"""API locale + fichiers du tableau de bord.

Sécurité : écoute sur 127.0.0.1 par défaut, jeton obligatoire (en-tête X-Admin-Token),
contrôle du Host (anti DNS-rebinding), CSP stricte, aucune ressource externe.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from connectors import ConnectorError, Incoming

_HTML = ("dashboard.html", "text/html; charset=utf-8")
STATIC = {
    "/": _HTML, "/index.html": _HTML,
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "application/javascript; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
MAX_BODY = 64 * 1024
HINTS = {
    "email": "Ajoute EMAIL_ADDRESS et EMAIL_PASSWORD (mot de passe d'application) dans .env, puis relance.",
    "whatsapp": "Ajoute WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN et WHATSAPP_APP_SECRET dans .env, puis relance.",
}


def mask_email(address: str) -> str:
    if "@" not in address:
        return address
    local, domain = address.split("@", 1)
    return f"{local[:1]}•••@{domain}"


def mask_phone(number: str) -> str:
    return f"+{number[:3]}••••{number[-2:]}" if len(number) > 6 else number


def get_or_create_token(store, env_token: str = "") -> str:
    if env_token.strip():
        return env_token.strip()
    token = store.get_state("dash_token")
    if not token:
        token = secrets.token_urlsafe(18)
        store.set_state("dash_token", token)
    return token


class Context:
    def __init__(self, cfg, store, dispatcher, agent, groq, connectors, token, base_dir, check_host=True):
        self.cfg, self.store, self.dispatcher, self.agent = cfg, store, dispatcher, agent
        self.groq, self.connectors, self.token = groq, connectors, token
        self.base_dir, self.check_host = base_dir, check_host
        self.allowed_hosts: set[str] = set()
        self.failures: dict[str, deque] = defaultdict(deque)


def build_state(ctx: Context) -> dict:
    disp, store, cfg = ctx.dispatcher, ctx.store, ctx.cfg
    now = disp.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = store.all_events_since(midnight)

    connectors = []
    for cid, label in (("email", "Email"), ("whatsapp", "WhatsApp")):
        connector = ctx.connectors.get(cid)
        health = store.get_state(f"health.{cid}", {}) or {}
        configured = bool(connector and connector.configured)
        enabled = bool(cfg["channels"].get(cid, True))
        if cid == "email":
            detail = mask_email(connector.addr) if configured else ""
        else:
            detail = f"ID {connector.phone_id[-4:].rjust(4, '•')}" if configured else ""
        connectors.append({
            "id": cid, "label": label, "configured": configured, "enabled": enabled, "detail": detail,
            "ok_ts": health.get("ok_ts"), "err": health.get("err"), "err_ts": health.get("err_ts"),
            "hint": "" if configured else HINTS[cid],
        })

    away = cfg["away"]
    drafts = [{
        "id": d["id"], "channel": d["channel"], "name": d.get("name") or d["sender"],
        "sender": mask_email(d["sender"]) if "@" in d["sender"] else mask_phone(d["sender"]),
        "subject": d.get("subject", ""), "incoming": d.get("incoming", ""), "reply": d.get("reply", ""),
        "created": d["created"], "note": d.get("note", ""),
    } for d in store.list_drafts()]

    return {
        "now": time.time(), "model": ctx.groq.last_model or ctx.groq.model,
        "groq_configured": ctx.groq.configured, "owner": cfg["owner_name"],
        "away": {"override": disp.away_override(), "active": disp.is_away(),
                 "window": f"{away.get('start', '20:00')} – {away.get('end', '07:00')}",
                 "days_off": away.get("days_off", [])},
        "reply_mode": disp.reply_mode(),
        "connectors": connectors, "drafts": drafts, "events": store.events(60),
        "stats": {
            "replied": sum(1 for e in today if e["kind"] in ("replied", "sent")),
            "pending": len(drafts),
            "escalated": sum(1 for e in today if e["kind"] == "escalated"),
        },
    }


def make_handler(ctx: Context):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AssistantDash/1.0"
        sys_version = ""

        def log_message(self, *args):           # silence : pas de contenu privé dans les logs
            pass

        # -- réponses ---------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, code: int, message: str) -> None:
            self._json(code, {"error": message})

        # -- contrôles -----------------------------------------------------------
        def _host_ok(self) -> bool:
            if not ctx.check_host:
                return True
            return (self.headers.get("Host") or "").strip().lower() in ctx.allowed_hosts

        def _auth_ok(self) -> bool:
            ip = self.client_address[0]
            window = ctx.failures[ip]
            now = time.time()
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= 10:
                return False
            given = self.headers.get("X-Admin-Token") or ""
            if hmac.compare_digest(given.encode(), ctx.token.encode()):
                return True
            window.append(now)
            return False

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length < 0 or length > MAX_BODY:
                return None
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            return data if isinstance(data, dict) else None

        # -- GET --------------------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._error(421, "Hôte non autorisé")
            path = self.path.split("?", 1)[0]
            if path in STATIC:
                name, ctype = STATIC[path]
                try:
                    with open(os.path.join(ctx.base_dir, name), "rb") as f:
                        return self._send(200, f.read(), ctype)
                except OSError:
                    return self._error(404, "Fichier du dashboard introuvable")
            if path == "/api/state":
                if not self._auth_ok():
                    return self._error(401, "Jeton invalide")
                return self._json(200, build_state(ctx))
            return self._error(404, "Introuvable")

        # -- POST -----------------------------------------------------------------------
        def do_POST(self):
            if not self._host_ok():
                return self._error(421, "Hôte non autorisé")
            if not self._auth_ok():
                return self._error(401, "Jeton invalide")
            body = self._read_json()
            if body is None:
                return self._error(400, "Requête invalide")
            path = self.path.split("?", 1)[0]
            store, disp = ctx.store, ctx.dispatcher

            if path == "/api/away":
                value = body.get("state")
                if value not in ("auto", "on", "off"):
                    return self._error(400, "Valeur invalide")
                store.set_state("away_override", value)
                store.log_event("info", "system", "Dashboard", f"Mode absent : {value}")
                return self._json(200, {"ok": True})

            if path == "/api/mode":
                value = body.get("mode")
                if value not in ("draft", "auto"):
                    return self._error(400, "Valeur invalide")
                store.set_state("reply_mode", value)
                store.log_event("info", "system", "Dashboard",
                                "Réponses automatiques activées" if value == "auto" else "Réponses sur validation")
                return self._json(200, {"ok": True})

            match = re.fullmatch(r"/api/drafts/([0-9a-f]{8})/(send|discard)", path)
            if match:
                draft_id, action = match.groups()
                try:
                    if action == "send":
                        text = body.get("text")
                        disp.send_draft(draft_id, text if isinstance(text, str) else None)
                    else:
                        disp.discard_draft(draft_id)
                except ConnectorError as exc:
                    return self._error(409 if "introuvable" in str(exc) else 502, str(exc)[:200])
                return self._json(200, {"ok": True})

            if path == "/api/test":
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._error(400, "Écris un message à tester")
                channel = body.get("channel") if body.get("channel") in ("email", "whatsapp") else "whatsapp"
                inc = Incoming(channel=channel, sender="000000000", name=str(body.get("name") or "Test")[:60],
                               subject="Test" if channel == "email" else "", text=text[:2000], msg_id="sim")
                try:
                    decision = ctx.agent.decide(inc, simulate=True, away_note=disp.away_note())
                except Exception as exc:
                    return self._error(502, f"IA indisponible : {str(exc)[:160]}")
                if decision.action != "reply":
                    outcome = "Rien ne serait envoyé."
                elif disp.reply_mode() == "auto" and not decision.force_draft:
                    outcome = "Serait envoyé automatiquement."
                else:
                    outcome = "Deviendrait un brouillon à valider."
                return self._json(200, {"action": decision.action, "reply": decision.reply,
                                        "reason": decision.reason, "outcome": outcome})

            return self._error(404, "Introuvable")

    return Handler


class DashboardServer:
    def __init__(self, host: str, port: int, ctx: Context):
        self.ctx = ctx
        self.httpd = ThreadingHTTPServer((host, port), make_handler(ctx))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        ctx.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="dashboard", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
