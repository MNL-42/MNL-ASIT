"""Connecteurs : Email (IMAP/SMTP) et WhatsApp (Cloud API officielle de Meta)."""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import hmac
import html
import imaplib
import json
import re
import smtplib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage


class ConnectorError(Exception):
    pass


@dataclass
class Incoming:
    channel: str                 # "email" | "whatsapp"
    sender: str                  # adresse email ou numéro (chiffres)
    text: str = ""
    name: str = ""
    subject: str = ""
    msg_id: str = ""
    automated: bool = False      # newsletter, bot, auto-réponse...
    meta: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        ident = self.msg_id or hashlib.sha1(f"{self.sender}|{self.text}|{int(self.ts // 60)}".encode()).hexdigest()[:16]
        return f"{self.channel}:{ident}"


# --------------------------------------------------------------------------- #
# Santé des connecteurs (affichée dans le dashboard)
# --------------------------------------------------------------------------- #
def record_ok(store, name: str) -> None:
    health = dict(store.get_state(f"health.{name}", {}) or {})
    health["ok_ts"] = time.time()
    health["err"] = None
    store.set_state(f"health.{name}", health)


def record_error(store, name: str, error: str) -> None:
    health = dict(store.get_state(f"health.{name}", {}) or {})
    health["err"] = (error or "")[:200]
    health["err_ts"] = time.time()
    store.set_state(f"health.{name}", health)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
_AUTOMATED_LOCALPARTS = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
                         "postmaster", "notification", "newsletter", "bounce", "alerts")


def _is_automated(msg, sender: str) -> bool:
    if (msg.get("Auto-Submitted") or "no").strip().lower() != "no":
        return True
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk", "auto_reply"):
        return True
    if any(msg.get(h) for h in ("List-Id", "List-Unsubscribe", "X-Autoreply", "X-Auto-Response-Suppress")):
        return True
    local = sender.split("@")[0].lower()
    return any(local.startswith(p) for p in _AUTOMATED_LOCALPARTS)


def _body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:
        return ""
    if part.get_content_type() == "text/html":
        content = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", content)
        content = re.sub(r"(?is)<br\s*/?>|</p>|</div>", "\n", content)
        content = html.unescape(re.sub(r"(?s)<[^>]+>", " ", content))
    content = re.split(r"\n(?:Le .{0,120}a écrit\s*:|On .{0,120}wrote\s*:|-{2,} ?(?:Original|Message d'origine))",
                       content)[0]
    lines = [ln for ln in content.splitlines() if not ln.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:4000]


def parse_email_message(raw: bytes, own_address: str = "") -> Incoming | None:
    """Transforme un email brut en Incoming (None si c'est nous-mêmes)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = email.utils.parseaddr(str(msg.get("Reply-To") or msg.get("From") or ""))
    from_name, _ = email.utils.parseaddr(str(msg.get("From") or ""))
    addr = addr.strip().lower()
    if not addr or "@" not in addr or addr == own_address.strip().lower():
        return None
    subject = re.sub(r"[\r\n]+", " ", str(msg.get("Subject") or "")).strip()[:200]
    message_id = str(msg.get("Message-ID") or "").strip()
    return Incoming(
        channel="email", sender=addr, name=from_name or name or addr, subject=subject,
        text=_body_text(msg), msg_id=message_id, automated=_is_automated(msg, addr),
        meta={"reply_to": addr, "subject": subject, "message_id": message_id,
              "references": str(msg.get("References") or "").strip()[:900]},
    )


class EmailConnector:
    name = "email"

    def __init__(self, env: dict):
        self.addr = (env.get("EMAIL_ADDRESS") or "").strip()
        password = (env.get("EMAIL_PASSWORD") or "").strip()
        if re.fullmatch(r"[a-z]{4}( [a-z]{4}){3}", password):   # mot de passe d'application Google
            password = password.replace(" ", "")
        self.password = password
        self.imap_host = (env.get("IMAP_HOST") or "imap.gmail.com").strip()
        self.smtp_host = (env.get("SMTP_HOST") or "smtp.gmail.com").strip()
        try:
            self.smtp_port = int(env.get("SMTP_PORT") or 465)
        except ValueError:
            self.smtp_port = 465

    @property
    def configured(self) -> bool:
        return bool(self.addr and self.password)

    def fetch_new(self, limit: int = 20) -> list[Incoming]:
        """Lit les mails non lus SANS les marquer comme lus (lecture seule)."""
        out: list[Incoming] = []
        with imaplib.IMAP4_SSL(self.imap_host, 993, timeout=25) as imap:
            imap.login(self.addr, self.password)
            imap.select("INBOX", readonly=True)
            status, data = imap.uid("SEARCH", None, "UNSEEN")
            if status != "OK":
                raise ConnectorError("Recherche IMAP refusée")
            for uid in data[0].split()[-limit:]:
                status, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                incoming = parse_email_message(parts[0][1], self.addr)
                if incoming:
                    if not incoming.msg_id:
                        incoming.msg_id = f"uid-{uid.decode()}"
                    out.append(incoming)
        return out

    def _smtp_send(self, msg: EmailMessage) -> None:
        if self.smtp_port == 587:
            with smtplib.SMTP(self.smtp_host, 587, timeout=25) as smtp:
                smtp.starttls()
                smtp.login(self.addr, self.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=25) as smtp:
                smtp.login(self.addr, self.password)
                smtp.send_message(msg)

    def send(self, meta: dict, text: str) -> None:
        to = (meta.get("reply_to") or "").strip()
        if "@" not in to:
            raise ConnectorError("Adresse du destinataire introuvable")
        subject = re.sub(r"[\r\n]+", " ", meta.get("subject") or "(sans objet)")
        msg = EmailMessage()
        msg["From"] = self.addr
        msg["To"] = to
        msg["Subject"] = subject if re.match(r"(?i)^re\s*:", subject) else f"Re: {subject}"
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid()
        msg["Auto-Submitted"] = "auto-replied"          # RFC 3834 : évite les boucles de réponses
        if meta.get("message_id"):
            msg["In-Reply-To"] = meta["message_id"]
            msg["References"] = f"{meta.get('references', '')} {meta['message_id']}".strip()
        msg.set_content(text)
        try:
            self._smtp_send(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise ConnectorError(f"Envoi email impossible: {exc}") from None

    def send_plain(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"] = self.addr, to
        msg["Subject"] = re.sub(r"[\r\n]+", " ", subject)
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(body)
        try:
            self._smtp_send(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise ConnectorError(f"Envoi email impossible: {exc}") from None


# --------------------------------------------------------------------------- #
# WhatsApp (Cloud API officielle)
# --------------------------------------------------------------------------- #
class WhatsAppConnector:
    name = "whatsapp"

    def __init__(self, env: dict):
        self.token = (env.get("WHATSAPP_TOKEN") or "").strip()
        self.phone_id = (env.get("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
        self.verify_token = (env.get("WHATSAPP_VERIFY_TOKEN") or "").strip()
        self.app_secret = (env.get("WHATSAPP_APP_SECRET") or "").strip()
        self.version = (env.get("GRAPH_API_VERSION") or "v26.0").strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.phone_id)

    # -- webhook ------------------------------------------------------------
    def verify_challenge(self, query: dict) -> str | None:
        """GET de vérification de Meta : renvoie le challenge si le token correspond."""
        mode = (query.get("hub.mode") or [""])[0]
        given = (query.get("hub.verify_token") or [""])[0]
        challenge = (query.get("hub.challenge") or [""])[0]
        if mode == "subscribe" and self.verify_token and hmac.compare_digest(given, self.verify_token):
            return challenge
        return None

    def valid_signature(self, raw: bytes, header: str | None) -> bool:
        """Vérifie X-Hub-Signature-256. Sans WHATSAPP_APP_SECRET, tout est refusé (sécurité)."""
        if not self.app_secret or not header:
            return False
        expected = "sha256=" + hmac.new(self.app_secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header.strip())

    def parse(self, payload: dict) -> list[Incoming]:
        out: list[Incoming] = []
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                         for c in value.get("contacts", []) or []}
                for m in value.get("messages", []) or []:
                    sender = re.sub(r"\D", "", str(m.get("from", "")))
                    if not sender:
                        continue
                    try:
                        sent_at = float(m.get("timestamp") or time.time())
                    except (TypeError, ValueError):
                        sent_at = time.time()
                    if time.time() - sent_at > 24 * 3600:      # trop ancien, fenêtre de 24 h dépassée
                        continue
                    kind = m.get("type", "text")
                    text = (m.get("text") or {}).get("body", "") if kind == "text" else f"[{kind}]"
                    out.append(Incoming(
                        channel="whatsapp", sender=sender, text=text[:2000],
                        name=names.get(sender) or names.get(m.get("from")) or sender,
                        msg_id=str(m.get("id", "")), ts=sent_at,
                        meta={"wa_id": str(m.get("id", "")), "kind": kind}))
        return out

    # -- envoi ----------------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"https://graph.facebook.com/{self.version}{path}",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "User-Agent": "assistant-ia/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                message = json.loads(body)["error"]["message"]
            except Exception:
                message = body[:200]
            raise ConnectorError(f"WhatsApp HTTP {exc.code}: {message}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectorError(f"WhatsApp réseau: {getattr(exc, 'reason', exc)}") from None

    def send_text(self, to: str, text: str, reply_to_msg_id: str | None = None) -> None:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual",
                   "to": re.sub(r"\D", "", to), "type": "text",
                   "text": {"preview_url": False, "body": text[:4000]}}
        if reply_to_msg_id:
            payload["context"] = {"message_id": reply_to_msg_id}
        self._post(f"/{self.phone_id}/messages", payload)

    def mark_read(self, msg_id: str) -> None:
        try:
            self._post(f"/{self.phone_id}/messages",
                       {"messaging_product": "whatsapp", "status": "read", "message_id": msg_id})
        except ConnectorError:
            pass    # purement cosmétique
