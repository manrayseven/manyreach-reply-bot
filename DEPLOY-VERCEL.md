# Déploiement Vercel — guide pas à pas

> Objectif : le bot tourne 24/7 dans le cloud (PC éteint OK) + un dashboard web
> sur ton URL Vercel (suivi des actions, réglages, bouton stop).
>
> On le fait ensemble, étape par étape. Chaque étape est vérifiable.

## Architecture

```
Vercel Cron (toutes 15 min) ──▶ /api/cron ──▶ fait tourner le bot (réutilise tout le code)
                                                   │
Toi ──▶ https://ton-projet.vercel.app ──▶ /api/index ──▶ Dashboard (status, actions, stop)
                                                   │
                              Vercel KV (base cloud) ◀── état : on/off, journal, réglages
```

## Pré-requis : le code est déjà sur GitHub ✅
Repo : https://github.com/manrayseven/manyreach-reply-bot

---

## Étape 1 — Importer le projet sur Vercel (3 min)
1. Va sur https://vercel.com/new
2. **Import Git Repository** → choisis `manyreach-reply-bot`
3. Framework Preset : **Other** (Vercel détecte Python via `api/` + `requirements.txt`)
4. **Ne déploie pas encore** — on ajoute d'abord les secrets et la base (étapes 2-3).
   (Si Vercel déploie automatiquement, pas grave, on re-déploiera après.)

## Étape 2 — Connecter une base Vercel KV (3 min)
1. Dans ton projet Vercel → onglet **Storage** → **Create Database** → **KV** (Upstash Redis)
2. Nomme-la `manyreach-bot-kv` → **Create**
3. **Connect to Project** → sélectionne ton projet → ça injecte automatiquement
   `KV_REST_API_URL` et `KV_REST_API_TOKEN` dans les variables d'env. ✅

## Étape 3 — Ajouter les secrets (Environment Variables) (5 min)
Projet Vercel → **Settings** → **Environment Variables**. Ajoute :

| Nom | Valeur |
|-----|--------|
| `MANYREACH_API_KEY` | ta clé ManyReach |
| `ANTHROPIC_API_KEY` | ta clé Anthropic |
| `GOOGLE_SERVICE_ACCOUNT_INFO` | le **contenu entier** du fichier `google-service-account.json` (copie-colle tout le JSON) |
| `GOOGLE_CALENDAR_ID` | `manray7@gmail.com` |
| `CRON_SECRET` | une chaîne aléatoire (ex. générée, 32 caractères) — sécurise le cron |
| `DASHBOARD_KEY` | une chaîne aléatoire — protège ton dashboard (tu mettras `?key=...` dans l'URL) |
| `LOG_DIR` | `/tmp/mr-logs` |

> ⚠️ Ces secrets vivent dans Vercel, **jamais dans le repo GitHub**. C'est la bonne pratique.

## Étape 4 — Déployer
- Onglet **Deployments** → **Redeploy** (ou push un commit).
- Attends le build (~1-2 min).

## Étape 5 — Vérifier
1. **Dashboard** : ouvre `https://ton-projet.vercel.app/?key=TON_DASHBOARD_KEY`
   → tu dois voir le statut, les réglages, le bouton stop, le tableau des actions.
2. **Cron** : onglet **Cron Jobs** dans Vercel → tu dois voir `/api/cron` programmé.
   Tu peux le déclencher manuellement pour tester (« Run »).

## ⚠️ Limite importante — fréquence du cron
- **Vercel Hobby (gratuit)** : les crons ne tournent qu'**une fois par jour**.
- Pour du **toutes les 15 min**, soit :
  - tu passes en **Vercel Pro** (cron illimité), soit
  - tu utilises un **cron externe gratuit** (https://cron-job.org) qui appelle
    `https://ton-projet.vercel.app/api/cron` avec l'en-tête
    `Authorization: Bearer TON_CRON_SECRET` toutes les 15 min. ← recommandé si tu restes en Hobby.

## ⚠️ Autre point — taille des dépendances
`google-api-python-client` est volumineux. Si le build Vercel dépasse la limite
de taille (250 Mo), on allègera (retirer les libs Google de la fonction cron et
gérer le Calendar autrement). On verra au déploiement.

---

## Une fois déployé
- Le bot tourne dans le cloud, PC éteint OK.
- Tu pilotes depuis le dashboard (URL + ?key=...).
- Tu peux **désactiver la tâche planifiée Windows locale** (`ARRETER-LE-BOT.bat`)
  pour éviter que le bot tourne en double (local + cloud).

## En cas de souci au déploiement
Copie-moi le message d'erreur du build Vercel ou de la fonction (onglet
**Logs** / **Functions**) et je débugge avec toi.
