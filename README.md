# Assistant IA — répond à tes messages quand tu es absent

IA propulsée par **Groq** (ta clé API), branchée sur ton **email** et **WhatsApp**, pilotée depuis un
**dashboard en verre 3D** (Liquid Glass). Zéro dépendance : Python 3.9+ suffit.

## Démarrage en 4 étapes

```bash
cp env.example .env          # puis remplis GROQ_API_KEY (et les connecteurs que tu veux)
python main.py check         # vérifie la clé, le modèle, l'email, WhatsApp
python main.py demo          # discute avec l'assistant dans le terminal (rien n'est envoyé)
python main.py run           # démarre tout
```

Au démarrage, le terminal affiche un lien du type `http://127.0.0.1:8787/#t=…` : ouvre-le une fois,
le navigateur retient le jeton.

## Le dashboard

- **Accueil** : mode absent (Auto / Toujours / Jamais), type de réponse (« Je valide » ou « Automatique »),
  compteurs du jour, état des connecteurs.
- **Brouillons** : tu relis, modifies, puis envoies ou ignores.
- **Test** : simule un message entrant pour voir ce que ferait l'assistant (rien n'est envoyé).
- **Activité** : journal de tout ce que l'assistant a fait.

Il n'écoute que sur ta machine (`127.0.0.1`) et exige un jeton. Pour l'ouvrir depuis ton téléphone,
utilise un tunnel privé (Tailscale par exemple) plutôt que d'ouvrir le port.

## Les connecteurs

### Email (le plus simple)
1. Active la validation en 2 étapes de ton compte, puis crée un **mot de passe d'application**.
2. Mets `EMAIL_ADDRESS` et `EMAIL_PASSWORD` dans `.env` (Gmail par défaut ; change `IMAP_HOST` / `SMTP_HOST` sinon).
3. Au premier lancement, l'assistant **ignore ton ancien courrier** : il ne répond qu'aux nouveaux mails.
   Il ne marque jamais tes mails comme lus et ne répond pas aux newsletters ni aux messages automatiques.

### WhatsApp (API officielle Meta)
1. Crée une app sur Meta for Developers, ajoute le produit **WhatsApp** et récupère le `WHATSAPP_TOKEN`
   (jeton permanent via un utilisateur système) et le `WHATSAPP_PHONE_NUMBER_ID`.
2. Mets aussi `WHATSAPP_APP_SECRET` (Paramètres > Général) et invente un `WHATSAPP_VERIFY_TOKEN`.
3. Expose **uniquement** le webhook (port 8080) en HTTPS, par exemple :
   `cloudflared tunnel --url http://localhost:8080`
4. Chez Meta, configure le webhook sur `https://TON-TUNNEL/webhook/whatsapp`, avec ton verify token,
   et abonne-toi au champ `messages`.

À savoir : l'API officielle est liée à un **numéro WhatsApp Business**, pas à ton WhatsApp personnel classique.
Les bibliothèques non officielles qui se « connectent » à un WhatsApp perso risquent de faire **bannir** ton numéro :
je ne les ai volontairement pas utilisées. Hors fenêtre de 24 h après le dernier message de la personne,
WhatsApp n'autorise plus la réponse libre.

## Sécurité intégrée

- Par défaut, **tu valides chaque réponse** (« Je valide »). L'envoi automatique est à activer toi-même.
- Même en automatique, une réponse contenant un **lien ou un numéro** passe en brouillon.
- Mots sensibles (urgent, virement, mot de passe, police…) : l'IA ne répond pas, elle te **prévient**
  (`notify` dans `config.json` ; l'email est plus fiable que WhatsApp pour ça).
- Le message reçu est traité comme une donnée non fiable (anti « prompt injection »). L'IA n'a **aucun outil** :
  elle ne peut qu'écrire un texte.
- Limite de 3 réponses/heure par expéditeur (anti-boucle), en-tête `Auto-Submitted` sur les emails.
- Webhook WhatsApp : signature `X-Hub-Signature-256` vérifiée (sans `WHATSAPP_APP_SECRET`, tout est refusé).
- Dossier `data/` : contient tes messages ; ne le partage pas.

## Personnaliser (`config.json`)

| Clé | Rôle |
| --- | --- |
| `owner_name`, `style`, `facts` | Ton nom, le ton voulu, les infos que l'assistant a le droit de donner |
| `away` | Plage horaire (`start`/`end`), `days_off` (0 = lundi), `utc_offset_hours` (Kinshasa = 1) |
| `reply_mode` | `draft` (tu valides) ou `auto` — modifiable aussi depuis le dashboard |
| `allowlist` / `blocklist` | Qui peut / ne peut jamais recevoir de réponse |
| `model`, `fallback_models` | Modèle Groq et modèles de secours |
| `escalate_keywords` | Mots qui te réveillent au lieu d'une réponse automatique |

## Mises à jour vérifiées (nov. 2026)

- Modèles Groq en production : `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b`.
  Si un modèle est retiré, le suivant de `fallback_models` prend le relais automatiquement.
  `python main.py check` liste les modèles réellement disponibles sur ton compte.
- Graph API Meta : `v26.0` (modifiable via `GRAPH_API_VERSION`).

## Tests

```bash
python -m unittest -v        # 22 tests hors-ligne (agent, signatures, horaires, brouillons, API du dashboard)
```

## Fichiers

`main.py` (démarrage) · `agent.py` (décision IA) · `dispatcher.py` (horaires, brouillons, envoi) ·
`connectors.py` (email, WhatsApp) · `groq_client.py` · `store.py` · `config.py` ·
`dashboard.py` + `dashboard.html/.css/.js` (interface)
