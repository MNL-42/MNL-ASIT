# TON SAAS AI — MNL Control Center
PWA inspirée de la vidéo : dashboard sombre glassmorphism, solde, graphiques, transactions, journal de virements, globe 3D animé, projets GitHub et couleurs personnalisables.

## Configuration
1. Créer le projet Supabase et exécuter supabase/schema.sql dans SQL Editor.
2. Copier l'URL du projet et la clé publishable/anon dans config.js.
3. Ne jamais placer la clé service_role dans le navigateur.
4. Activer GitHub Pages : Settings → Pages → Deploy from branch → main → /(root).
5. Ouvrir l'URL HTTPS dans Chrome Android puis choisir Installer l'application.

Les transactions et virements sont des enregistrements de suivi uniquement : aucun transfert bancaire n'est exécuté. Aucun revenu fictif n'est préchargé. Les lignes financières sont privées via RLS. GitHub affiche les dépôts publics; les dépôts privés nécessitent OAuth côté serveur.