"""Chargement du fichier .env et de config.json (sans aucune dépendance)."""
from __future__ import annotations

import copy
import json
import os

DEFAULTS: dict = {
    "owner_name": "Moi",
    "language": "français",
    "style": "Court, poli et naturel. Tutoiement avec les proches, vouvoiement avec les inconnus.",
    # Infos que l'assistant a le droit de donner (horaires, disponibilités, FAQ...)
    "facts": [],
    # Ajoutée à chaque réponse automatique (transparence). Laisse vide pour la retirer.
    "signature": "— Assistant de {owner}",
    # "draft" = l'assistant prépare, TU valides depuis le dashboard. "auto" = envoi direct.
    "reply_mode": "draft",
    # Si true, l'assistant ne répond que pendant tes périodes d'absence.
    "only_when_away": True,
    "away": {
        "mode": "auto",          # auto | on | off
        "start": "20:00",
        "end": "07:00",
        "days_off": [],          # jours d'absence totale : 0=lundi ... 6=dimanche
        "utc_offset_hours": 1,   # Kinshasa = UTC+1
    },
    # Modèles Groq (production en nov. 2026). Le suivant prend le relais si le premier échoue.
    "model": "llama-3.3-70b-versatile",
    "fallback_models": ["openai/gpt-oss-120b", "llama-3.1-8b-instant"],
    "max_reply_chars": 600,
    "max_replies_per_sender_per_hour": 3,
    # Ces mots déclenchent TOUJOURS une escalade vers toi (jamais de réponse automatique).
    "escalate_keywords": [
        "urgent", "urgence", "accident", "hôpital", "hopital", "police", "décès", "deces",
        "incendie", "sos", "au secours", "virement", "paiement", "mot de passe",
        "code de vérification", "otp", "banque", "avocat", "tribunal", "menace",
    ],
    "allowlist": [],   # si non vide : seuls ces expéditeurs reçoivent une réponse
    "blocklist": [],   # jamais de réponse à ceux-là
    "notify": {"email": "", "whatsapp": ""},   # où te prévenir en cas d'escalade
    "email_poll_seconds": 30,
    "channels": {"email": True, "whatsapp": True},
}


def load_env(path: str = ".env") -> None:
    """Charge KEY=VALUE depuis .env sans écraser les variables déjà définies."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                os.environ.setdefault(key, value)


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str = "config.json") -> dict:
    user: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    cfg = _merge(DEFAULTS, user)
    if cfg["reply_mode"] not in ("draft", "auto"):
        cfg["reply_mode"] = "draft"
    if cfg["away"].get("mode") not in ("auto", "on", "off"):
        cfg["away"]["mode"] = "auto"
    return cfg
