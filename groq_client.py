"""Client minimal pour l'API Groq (compatible OpenAI). Aucune dépendance."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

API_BASE = "https://api.groq.com/openai/v1"
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class GroqError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503, 504) or self.status is None

    @property
    def model_problem(self) -> bool:
        text = str(self).lower()
        return self.status in (400, 404) and ("model" in text or "decommission" in text)


class GroqClient:
    def __init__(self, api_key: str, model: str, fallback_models: list[str] | None = None,
                 timeout: float = 30, retries: int = 2):
        self.api_key = (api_key or "").strip()
        self.model = model
        self.fallback_models = list(fallback_models or [])
        self.timeout = timeout
        self.retries = retries
        self.last_model = model

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            API_BASE + path, data=data, method="POST" if payload is not None else "GET",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Sans User-Agent explicite, Cloudflare (devant Groq) peut refuser urllib.
                "User-Agent": "assistant-ia/1.0 (+python-urllib)",
            })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:600]
            try:
                message = json.loads(body)["error"]["message"]
            except Exception:
                message = body or str(exc.reason)
            retry_after = None
            try:
                retry_after = float(exc.headers.get("retry-after"))
            except (TypeError, ValueError):
                pass
            raise GroqError(f"HTTP {exc.code}: {message}", exc.code, retry_after) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GroqError(f"Réseau: {getattr(exc, 'reason', exc)}") from None
        except json.JSONDecodeError:
            raise GroqError("Réponse Groq illisible", 502) from None

    def list_models(self) -> list[str]:
        data = self._request("/models")
        return sorted(m["id"] for m in data.get("data", []))

    def chat(self, messages: list[dict], temperature: float = 0.3, max_tokens: int = 900) -> str:
        if not self.configured:
            raise GroqError("GROQ_API_KEY manquante (voir .env)")
        last_error: GroqError | None = None
        for model in [self.model] + [m for m in self.fallback_models if m != self.model]:
            payload = {"model": model, "messages": messages, "temperature": temperature,
                       "max_completion_tokens": max_tokens}
            if model.startswith("openai/gpt-oss"):
                payload["reasoning_effort"] = "low"
            for attempt in range(self.retries + 1):
                try:
                    data = self._request("/chat/completions", payload)
                    text = (data["choices"][0]["message"].get("content") or "").strip()
                    self.last_model = model
                    return _THINK_RE.sub("", text).strip()
                except GroqError as exc:
                    last_error = exc
                    if exc.status in (401, 403):
                        raise                      # clé invalide : inutile d'essayer un autre modèle
                    if exc.model_problem:
                        break                      # modèle retiré : passer au suivant
                    if exc.retryable and attempt < self.retries:
                        time.sleep(min(exc.retry_after or 2 ** attempt, 8))
                        continue
                    if exc.retryable:
                        break                      # trop de 429/5xx : essayer le modèle suivant
                    raise
                except (KeyError, IndexError, TypeError):
                    last_error = GroqError("Réponse Groq inattendue", 502)
                    break
        raise last_error or GroqError("Aucun modèle disponible")
