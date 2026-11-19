"""Stockage JSON local : atomique, thread-safe, fichiers privés (chmod 600)."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

MAX_EVENTS = 300
MAX_SEEN = 3000
MAX_HISTORY = 12
MAX_CONVERSATIONS = 200
MAX_DRAFTS = 500


class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict = {}

    # ---- bas niveau -------------------------------------------------------
    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name + ".json")

    def _load(self, name: str, default):
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            path = self._path(name)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = default
            except (json.JSONDecodeError, OSError):
                try:
                    os.replace(path, f"{path}.corrupt-{int(time.time())}")
                except OSError:
                    pass
                data = default
            self._cache[name] = data
            return data

    def _save(self, name: str) -> None:
        with self._lock:
            path = self._path(name)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache[name], f, ensure_ascii=False, indent=1)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)

    # ---- état clé/valeur ----------------------------------------------------
    def get_state(self, key: str, default=None):
        with self._lock:
            return self._load("state", {}).get(key, default)

    def set_state(self, key: str, value) -> None:
        with self._lock:
            self._load("state", {})[key] = value
            self._save("state")

    # ---- brouillons ---------------------------------------------------------
    def add_draft(self, **fields) -> dict:
        with self._lock:
            drafts = self._load("drafts", [])
            draft = {"id": uuid.uuid4().hex[:8], "created": time.time(), "status": "pending", **fields}
            drafts.append(draft)
            if len(drafts) > MAX_DRAFTS:
                keep = [d for d in drafts if d["status"] == "pending"]
                rest = [d for d in drafts if d["status"] != "pending"][-(MAX_DRAFTS // 2):]
                drafts[:] = sorted(keep + rest, key=lambda d: d["created"])
            self._save("drafts")
            return dict(draft)

    def list_drafts(self, status: str = "pending") -> list:
        with self._lock:
            items = [dict(d) for d in self._load("drafts", []) if d.get("status") == status]
        return sorted(items, key=lambda d: d["created"], reverse=True)

    def get_draft(self, draft_id: str):
        with self._lock:
            for d in self._load("drafts", []):
                if d["id"] == draft_id:
                    return dict(d)
        return None

    def update_draft(self, draft_id: str, **changes):
        with self._lock:
            for d in self._load("drafts", []):
                if d["id"] == draft_id:
                    d.update(changes)
                    self._save("drafts")
                    return dict(d)
        return None

    # ---- mémoire de conversation -------------------------------------------
    def history(self, key: str) -> list:
        with self._lock:
            return list(self._load("memory", {}).get(key, []))

    def add_history(self, key: str, role: str, content: str) -> None:
        with self._lock:
            memory = self._load("memory", {})
            turns = memory.pop(key, [])
            turns.append({"role": role, "content": content[:2500]})
            memory[key] = turns[-MAX_HISTORY:]
            while len(memory) > MAX_CONVERSATIONS:
                memory.pop(next(iter(memory)))
            self._save("memory")

    # ---- messages déjà traités ---------------------------------------------
    def seen(self, key: str) -> bool:
        with self._lock:
            return key in self._load("seen", [])

    def mark_seen(self, key: str) -> None:
        with self._lock:
            keys = self._load("seen", [])
            if key not in keys:
                keys.append(key)
                del keys[:-MAX_SEEN]
                self._save("seen")

    # ---- journal d'activité -------------------------------------------------
    def log_event(self, kind: str, channel: str, who: str, text: str, **extra) -> None:
        with self._lock:
            events = self._load("activity", [])
            events.append({"ts": time.time(), "kind": kind, "channel": channel,
                           "who": (who or "")[:80], "text": (text or "")[:160], **extra})
            del events[:-MAX_EVENTS]
            self._save("activity")

    def events(self, limit: int = 60) -> list:
        with self._lock:
            return [dict(e) for e in reversed(self._load("activity", [])[-limit:])]

    def all_events_since(self, ts: float) -> list:
        with self._lock:
            return [dict(e) for e in self._load("activity", []) if e["ts"] >= ts]
