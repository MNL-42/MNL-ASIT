#!/usr/bin/env python3
"""Assistant IA (Groq) qui répond à tes messages quand tu es absent.

Commandes :
    python main.py run     démarre tout (email, WhatsApp, dashboard)
    python main.py check   vérifie la clé Groq, le modèle et les connecteurs
    python main.py demo    discute avec l'assistant dans le terminal (rien n'est envoyé)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import signal
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from agent import Agent
from config import load_config, load_env
from connectors import (ConnectorError, EmailConnector, Incoming, WhatsAppConnector,
                        record_error, record_ok)
from dashboard import Context, DashboardServer, get_or_create_token
from dispatcher import Dispatcher
from groq_client import GroqClient, GroqError
from store import Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("assistant")


def build(env=os.environ):
    cfg = load_config(os.path.join(BASE_DIR, "config.json"))
    store = Store(os.path.join(BASE_DIR, "data"))
    groq = GroqClient(env.get("GROQ_API_KEY", ""), cfg["model"], cfg["fallback_models"])
    agent = Agent(cfg, groq, store)
    connectors = {"email": EmailConnector(env), "whatsapp": WhatsAppConnector(env)}
    dispatcher = Dispatcher(cfg, store, agent, connectors)
    return cfg, store, groq, agent, connectors, dispatcher


# --------------------------------------------------------------------------- #
# Webhook WhatsApp (à exposer via un tunnel HTTPS ; le dashboard, lui, reste local)
# --------------------------------------------------------------------------- #
def make_webhook_handler(wa: WhatsAppConnector, store: Store, work: queue.Queue):
    class WebhookHandler(BaseHTTPRequestHandler):
        server_version = "Assistant"
        sys_version = ""

        def log_message(self, *args):
            pass

        def _reply(self, code: int, body: str = "") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._reply(200, "ok")
            if url.path == "/webhook/whatsapp":
                challenge = wa.verify_challenge(parse_qs(url.query))
                return self._reply(200, challenge) if challenge is not None else self._reply(403, "forbidden")
            self._reply(404, "not found")

        def do_POST(self):
            if urlparse(self.path).path != "/webhook/whatsapp":
                return self._reply(404, "not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._reply(400, "bad length")
            if length <= 0 or length > 1_000_000:
                return self._reply(413, "too large")
            raw = self.rfile.read(length)
            if not wa.valid_signature(raw, self.headers.get("X-Hub-Signature-256")):
                log.warning("Webhook refusé : signature invalide (WHATSAPP_APP_SECRET correct ?)")
                return self._reply(403, "bad signature")
            try:
                items = wa.parse(json.loads(raw))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._reply(400, "bad json")
            for item in items:
                try:
                    work.put_nowait(item)
                except queue.Full:
                    return self._reply(503, "busy")      # Meta réessaiera plus tard
            record_ok(store, "whatsapp")
            self._reply(200, "ok")

    return WebhookHandler


# --------------------------------------------------------------------------- #
# Boucles de fond
# --------------------------------------------------------------------------- #
def worker_loop(work: queue.Queue, dispatcher: Dispatcher, store: Store, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            incoming = work.get(timeout=1)
        except queue.Empty:
            continue
        try:
            dispatcher.handle(incoming)
        except Exception as exc:                 # un message problématique ne doit pas tuer le worker
            log.exception("Erreur de traitement")
            store.log_event("error", incoming.channel, incoming.name or incoming.sender, f"Erreur interne : {exc}")


def email_loop(mail: EmailConnector, work: queue.Queue, store: Store, cfg: dict, stop: threading.Event) -> None:
    interval = max(10, int(cfg.get("email_poll_seconds", 30)))
    delay = interval
    baseline_done = bool(store.get_state("email_baseline"))
    while not stop.is_set():
        try:
            items = mail.fetch_new()
            if not baseline_done:
                for item in items:                # première synchro : on ne répond PAS à l'ancien courrier
                    store.mark_seen(item.key)
                store.set_state("email_baseline", True)
                baseline_done = True
                store.log_event("info", "email", "Synchro", f"Boîte synchronisée : {len(items)} ancien(s) mail(s) ignoré(s)")
            else:
                for item in items:
                    if not store.seen(item.key):
                        work.put(item, timeout=5)
            record_ok(store, "email")
            delay = interval
        except Exception as exc:                  # noqa: BLE001 : le poll doit survivre à toute erreur
            record_error(store, "email", str(exc))
            log.warning("Email : %s", exc)
            delay = min(delay * 2, 300)
        stop.wait(delay)


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #
def cmd_run() -> int:
    load_env(os.path.join(BASE_DIR, ".env"))
    cfg, store, groq, agent, connectors, dispatcher = build()
    stop = threading.Event()
    work: queue.Queue = queue.Queue(maxsize=500)
    threads = [threading.Thread(target=worker_loop, args=(work, dispatcher, store, stop), daemon=True)]

    if not groq.configured:
        log.warning("GROQ_API_KEY absente : l'assistant ne pourra pas répondre (voir .env)")

    mail, wa = connectors["email"], connectors["whatsapp"]
    if cfg["channels"].get("email", True) and mail.configured:
        threads.append(threading.Thread(target=email_loop, args=(mail, work, store, cfg, stop), daemon=True))
        log.info("Email connecté : %s", mail.addr)
    webhook = None
    if cfg["channels"].get("whatsapp", True) and wa.configured:
        port = int(os.environ.get("WEBHOOK_PORT", "8080"))
        webhook = ThreadingHTTPServer(("0.0.0.0", port), make_webhook_handler(wa, store, work))
        webhook.daemon_threads = True
        threads.append(threading.Thread(target=webhook.serve_forever, daemon=True))
        log.info("Webhook WhatsApp : http://0.0.0.0:%d/webhook/whatsapp (à exposer en HTTPS via un tunnel)", port)
        if not wa.app_secret or not wa.verify_token:
            log.warning("WHATSAPP_APP_SECRET / WHATSAPP_VERIFY_TOKEN manquants : le webhook refusera les requêtes")

    host = os.environ.get("DASH_HOST", "127.0.0.1")
    token = get_or_create_token(store, os.environ.get("DASHBOARD_TOKEN", ""))
    ctx = Context(cfg, store, dispatcher, agent, groq, connectors, token, BASE_DIR,
                  check_host=host in ("127.0.0.1", "localhost", "::1"))
    dash = DashboardServer(host, int(os.environ.get("DASH_PORT", "8787")), ctx)
    dash.start()
    if not ctx.check_host:
        log.warning("Dashboard exposé sur %s en HTTP clair : utilise plutôt un tunnel privé (ex. Tailscale).", host)

    for t in threads:
        t.start()
    print(f"\n  ✦ Dashboard : http://127.0.0.1:{dash.port}/#t={token}\n    (ouvre ce lien une fois, le jeton est retenu par le navigateur)\n")

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            stop.wait(1)
    except KeyboardInterrupt:
        pass
    stop.set()
    dash.stop()
    if webhook:
        webhook.shutdown()
    return 0


def cmd_check() -> int:
    load_env(os.path.join(BASE_DIR, ".env"))
    cfg, store, groq, agent, connectors, dispatcher = build()
    ok = True

    def line(good: bool | None, text: str) -> None:
        print(("  ✓ " if good else "  ✗ " if good is False else "  • ") + text)

    print("\nGroq")
    if not groq.configured:
        line(False, "GROQ_API_KEY absente dans .env")
        ok = False
    else:
        try:
            models = groq.list_models()
            line(True, f"clé valide ({len(models)} modèles disponibles)")
            for name in [cfg["model"]] + cfg["fallback_models"]:
                line(name in models, f"modèle {name}" + ("" if name in models else " — introuvable, change-le dans config.json"))
            reply = groq.chat([{"role": "user", "content": "Réponds juste: OK"}], max_tokens=20)
            line(True, f"test de réponse : « {reply[:40]} » (modèle {groq.last_model})")
        except GroqError as exc:
            line(False, f"Groq : {exc}")
            ok = False

    print("\nEmail")
    mail = connectors["email"]
    if not mail.configured:
        line(None, "non configuré (EMAIL_ADDRESS / EMAIL_PASSWORD)")
    else:
        try:
            import imaplib
            with imaplib.IMAP4_SSL(mail.imap_host, 993, timeout=20) as imap:
                imap.login(mail.addr, mail.password)
            line(True, f"connexion IMAP OK ({mail.addr})")
        except Exception as exc:
            line(False, f"IMAP : {exc}")
            ok = False

    print("\nWhatsApp")
    wa = connectors["whatsapp"]
    if not wa.configured:
        line(None, "non configuré (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID)")
    else:
        try:
            req = urllib.request.Request(
                f"https://graph.facebook.com/{wa.version}/{wa.phone_id}?fields=display_phone_number,verified_name",
                headers={"Authorization": f"Bearer {wa.token}", "User-Agent": "assistant-ia/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                info = json.loads(resp.read().decode())
            line(True, f"jeton valide, numéro {info.get('display_phone_number', '?')} (API {wa.version})")
        except urllib.error.HTTPError as exc:
            line(False, f"Meta a refusé le jeton (HTTP {exc.code}) : régénère-le")
            ok = False
        except OSError as exc:
            line(False, f"Réseau : {exc}")
            ok = False
        line(bool(wa.app_secret), "WHATSAPP_APP_SECRET " + ("présent" if wa.app_secret else "manquant (obligatoire pour le webhook)"))
        line(bool(wa.verify_token), "WHATSAPP_VERIFY_TOKEN " + ("présent" if wa.verify_token else "manquant"))
    print()
    return 0 if ok else 1


def cmd_demo() -> int:
    load_env(os.path.join(BASE_DIR, ".env"))
    cfg, store, groq, agent, connectors, dispatcher = build()
    print("Mode démo : écris un message comme si on t'écrivait (Ctrl+C pour quitter). Rien n'est envoyé.\n")
    try:
        while True:
            text = input("Message reçu > ").strip()
            if not text:
                continue
            try:
                d = agent.decide(Incoming(channel="whatsapp", sender="demo", name="Démo", text=text, msg_id="demo"),
                                 simulate=True, away_note=dispatcher.away_note())
            except GroqError as exc:
                print(f"  ✗ {exc}\n")
                continue
            print(f"  → {d.action.upper()} ({d.reason})")
            if d.reply:
                print("  " + d.reply.replace("\n", "\n  ") + "\n")
    except (KeyboardInterrupt, EOFError):
        print()
        return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    commands = {"run": cmd_run, "check": cmd_check, "demo": cmd_demo}
    if command not in commands:
        print(__doc__)
        return 2
    return commands[command]()


if __name__ == "__main__":
    sys.exit(main())
