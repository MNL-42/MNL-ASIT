"""Orchestration : horaires d'absence, brouillon ou envoi direct, escalades, journal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from connectors import ConnectorError, Incoming, record_error, record_ok


def _minutes(value: str, default: int) -> int:
    try:
        hours, minutes = str(value).split(":")
        return max(0, min(1439, int(hours) * 60 + int(minutes)))
    except (ValueError, TypeError):
        return default


class Dispatcher:
    def __init__(self, cfg: dict, store, agent, connectors: dict):
        self.cfg, self.store, self.agent, self.connectors = cfg, store, agent, connectors

    # ---- horaires ---------------------------------------------------------------
    def tz(self) -> timezone:
        return timezone(timedelta(hours=float(self.cfg["away"].get("utc_offset_hours", 0))))

    def now(self) -> datetime:
        return datetime.now(self.tz())

    def schedule_active(self, now: datetime | None = None) -> bool:
        now = now or self.now()
        away = self.cfg["away"]
        if now.weekday() in away.get("days_off", []):
            return True
        start, end = _minutes(away.get("start"), 20 * 60), _minutes(away.get("end"), 7 * 60)
        current = now.hour * 60 + now.minute
        if start == end:
            return True
        return start <= current < end if start < end else (current >= start or current < end)

    def away_override(self) -> str:
        value = self.store.get_state("away_override", self.cfg["away"].get("mode", "auto"))
        return value if value in ("auto", "on", "off") else "auto"

    def is_away(self, now: datetime | None = None) -> bool:
        override = self.away_override()
        if override == "on":
            return True
        if override == "off":
            return False
        return self.schedule_active(now)

    def reply_mode(self) -> str:
        value = self.store.get_state("reply_mode", self.cfg["reply_mode"])
        return value if value in ("draft", "auto") else "draft"

    def away_note(self) -> str:
        return f"{self.cfg['owner_name']} est actuellement absent·e."

    # ---- envoi --------------------------------------------------------------------
    def _deliver(self, channel: str, sender: str, meta: dict, text: str) -> None:
        connector = self.connectors.get(channel)
        if connector is None or not connector.configured:
            raise ConnectorError(f"Connecteur « {channel} » non configuré")
        if channel == "email":
            connector.send(meta, text)
        elif channel == "whatsapp":
            connector.send_text(sender, text, meta.get("wa_id"))
        else:
            raise ConnectorError(f"Canal inconnu : {channel}")
        record_ok(self.store, channel)

    def notify_owner(self, text: str) -> None:
        notify = self.cfg.get("notify", {})
        try:
            email_to = (notify.get("email") or "").strip()
            mail = self.connectors.get("email")
            if email_to and mail and mail.configured:
                mail.send_plain(email_to, "Assistant : un message demande ton attention", text)
            wa_to = (notify.get("whatsapp") or "").strip()
            wa = self.connectors.get("whatsapp")
            if wa_to and wa and wa.configured:
                wa.send_text(wa_to, text)      # fenêtre 24 h : n'arrive que si tu as écrit au numéro récemment
        except Exception as exc:               # une notification ratée ne doit jamais bloquer le reste
            self.store.log_event("error", "system", "notification", f"Notification impossible : {exc}")

    # ---- traitement d'un message entrant -----------------------------------------------
    def handle(self, inc: Incoming) -> None:
        if self.store.seen(inc.key):
            return
        self.store.mark_seen(inc.key)
        who = inc.name or inc.sender
        wa = self.connectors.get("whatsapp")
        if inc.channel == "whatsapp" and wa and inc.meta.get("wa_id"):
            wa.mark_read(inc.meta["wa_id"])

        if self.cfg.get("only_when_away", True) and not self.is_away():
            self.store.log_event("skipped", inc.channel, who, inc.text)
            return

        try:
            decision = self.agent.decide(inc, away_note=self.away_note())
        except Exception as exc:
            self.store.log_event("error", inc.channel, who, f"IA indisponible : {exc}")
            self.notify_owner(f"⚠️ {who} ({inc.channel}) : {inc.text[:200]}\n(L'assistant n'a pas pu répondre : {exc})")
            return

        if decision.action == "ignore":
            self.store.log_event("ignored", inc.channel, who, decision.reason or inc.text)
        elif decision.action == "escalate":
            self.store.log_event("escalated", inc.channel, who, f"{decision.reason} — {inc.text}")
            self.notify_owner(f"⚠️ {who} ({inc.channel}) : {inc.text[:300]}\nRaison : {decision.reason}")
        else:
            self._reply(inc, who, decision)

    def _reply(self, inc: Incoming, who: str, decision) -> None:
        if self.reply_mode() == "auto" and not decision.force_draft:
            try:
                self._deliver(inc.channel, inc.sender, inc.meta, decision.reply)
                self.store.log_event("replied", inc.channel, who, decision.reply)
                return
            except Exception as exc:
                record_error(self.store, inc.channel, str(exc))
                self._make_draft(inc, who, decision, note=f"Envoi automatique échoué : {exc}")
                return
        self._make_draft(inc, who, decision, note=decision.reason if decision.force_draft else "")

    def _make_draft(self, inc: Incoming, who: str, decision, note: str = "") -> None:
        self.store.add_draft(channel=inc.channel, sender=inc.sender, name=who, subject=inc.subject,
                             incoming=inc.text[:1500], reply=decision.reply, meta=inc.meta, note=note[:200])
        self.store.log_event("draft", inc.channel, who, decision.reply)

    # ---- actions depuis le dashboard ---------------------------------------------------
    def send_draft(self, draft_id: str, text: str | None = None) -> None:
        draft = self.store.get_draft(draft_id)
        if not draft or draft["status"] != "pending":
            raise ConnectorError("Brouillon introuvable ou déjà traité")
        body = (text if text is not None else draft["reply"]).strip()
        if not body:
            raise ConnectorError("La réponse est vide")
        try:
            self._deliver(draft["channel"], draft["sender"], draft.get("meta", {}), body[:4000])
        except ConnectorError as exc:
            record_error(self.store, draft["channel"], str(exc))
            raise
        self.store.update_draft(draft_id, status="sent", reply=body, sent=self.now().timestamp())
        self.store.log_event("sent", draft["channel"], draft["name"], body)

    def discard_draft(self, draft_id: str) -> None:
        draft = self.store.get_draft(draft_id)
        if not draft or draft["status"] != "pending":
            raise ConnectorError("Brouillon introuvable ou déjà traité")
        self.store.update_draft(draft_id, status="discarded")
        self.store.log_event("discarded", draft["channel"], draft["name"], draft["reply"])
