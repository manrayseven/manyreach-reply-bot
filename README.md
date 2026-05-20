# ManyReach Reply Bot

Bot qui classifie les replies aux cold emails ManyReach, tague le prospect, drafte (ou envoie) une réponse adaptée à l'intent, et gère unsubscribes/désinscriptions automatiquement.

## Statut

**Phase 1 — MVP CLI local** (en construction)

- [x] Reverse-engineering de l'API ManyReach v2 (spec complète dans `manyreach_openapi.json`)
- [x] Script d'exploration `explore_api.ps1`
- [x] `.gitignore` (protège `.env`)
- [ ] Client Python ManyReach (`src/manyreach.py`)
- [ ] Classifier + drafter
- [ ] Runner CLI avec `--dry-run`

**Phase 2 — Production**

- [ ] Déploiement Vercel (webhook event-driven au lieu de polling)
- [ ] Google Calendar pour proposer 3 créneaux concrets dans les réponses
- [ ] GitHub Actions cron 1x/jour pour bumps (`MaybeLater`, `Interested sans booking`, etc.)

## Architecture

### Phase 1 (local CLI)

```
PowerShell ─▶ python scripts/run_bot.py [--dry-run]
                 │
                 ├─ GET /api/v2/messages?type=Reply&since=...
                 │   └─ Filtre les bounces (mail-out.ovh.net, postmaster, etc.)
                 │
                 ├─ Pour chaque reply non traité :
                 │   ├─ Classifier (Claude API) → {intent, confidence, key_phrase}
                 │   ├─ Drafter (Claude API) → HTML reply
                 │   ├─ Map intent → actions (PATCH status, POST tag, POST blacklist, POST reply)
                 │   └─ Dry-run : print les actions / Live : execute
                 │
                 └─ Log dans logs/run_YYYY-MM-DD.jsonl (audit + idempotence)
```

### Phase 2 (production event-driven)

```
ManyReach ──webhook──▶ Vercel function (api/webhook.py)
                          ├─ Verify token
                          ├─ Same classify + draft + action pipeline
                          └─ Respond 200 OK

GitHub Actions cron ──1x/jour──▶ scripts/bump_leads.py
                          ├─ GET prospects?sendingStatus=Interested,MaybeLater
                          └─ Send appropriate bump per state machine rules
```

## API ManyReach — endpoints utilisés

| Méthode | Endpoint | Usage |
|---------|----------|-------|
| GET | `/api/v2/messages?type=Reply` | Liste les replies (filtrable par `campaignId`) |
| GET | `/api/v2/prospects/{id}` | Détail prospect (statut, custom fields) |
| GET | `/api/v2/prospects/{id}/messages` | Thread complet (Sent + Reply) |
| GET | `/api/v2/tags` | Liste tags existants (pour mapping intent → tagId) |
| POST | `/api/v2/messages/reply` | Envoie un reply dans le thread |
| POST | `/api/v2/prospects/{id}/tags` | Tag un prospect (body: `{"tagId": int}`) |
| POST | `/api/v2/blacklist/emails` | Désinscrit (body: `{"emails": [str]}`) |
| GET | `/api/v2/blacklist/emails/check` | Vérifie si email est blacklisté |
| PATCH | `/api/v2/prospects/{id}` | Update `sendingStatus`, `sendingActive`, etc. |

**Auth** : `X-API-Key: <key>` header  
**Base URL** : `https://api.manyreach.com/api/v2`

## Mapping intent → actions

| Intent | sendingStatus | sendingActive | Tags ajoutés | Blacklist | Reply envoyé |
|--------|---------------|---------------|--------------|-----------|--------------|
| `interested_warm` | `Interested` | true | `hot-lead` | non | oui + créneaux |
| `interested_lukewarm` | `Neutral` | true | `lukewarm` | non | oui + créneaux |
| `objection_price` | `MaybeLater` | true | `price-objection` | non | oui (reframe valeur) |
| `objection_timing` | `MaybeLater` | true | `timing-objection` | non | oui (recontact plus tard) |
| `objection_already_have_solution` | `NotInterested` | false | `has-solution` | non | oui (reframe ponctuel) |
| `wrong_person_redirect` | `NotInterested` | false | `wrong-person` | non | oui (demande contact réel) |
| `ask_more_info` | `Neutral` | true | `info-requested` | non | oui (réponse + call) |
| `not_interested_polite` | `NotInterested` | false | `not-interested` | non | optionnel (silence par défaut) |
| `unsubscribe` | `Unsub` | false | `unsub` | **oui** | non (silence) |
| `hostile` | `Unsub` | false | `hostile` | oui (safety) | non |
| `bounce` (auto) | `BounceHard` | false | — | non | non |

## Setup (Phase 1)

### Prérequis
- Python 3.10+
- Clé API ManyReach (Settings → API)
- Clé API Anthropic (console.anthropic.com)

### Installation
```powershell
cd "C:\Users\ManRa\Desktop\Rudy111\Claude\Manyreach Answers"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Éditer .env avec tes vraies clés
```

### Tester en dry-run (aucun mail envoyé)
```powershell
python scripts/run_bot.py --dry-run --limit 5
```

### Mode auto-send (uniquement pour intents safe : unsubscribe, hostile, not_interested_polite avec confidence > 0.95)
```powershell
python scripts/run_bot.py --auto-send
```

### Mode review (drafts générés, à envoyer manuellement via ManyReach UI)
```powershell
python scripts/run_bot.py --review-mode
```

## Fichiers

- `src/manyreach.py` — Client API ManyReach
- `src/classifier.py` — Classification d'intent via Claude
- `src/drafter.py` — Génération de réponse via Claude
- `src/actions.py` — Mapping intent → actions API
- `src/prompts/classify.md` — System prompt classification
- `src/prompts/draft.md` — System prompt drafting
- `scripts/run_bot.py` — Runner CLI
- `scripts/explore_api.ps1` — Probe API (déjà utilisé)
- `config/settings.yaml` — Calendrier, signature, seuils auto-send
- `config/training_examples.md` — Few-shot examples (à compléter par l'utilisateur)
- `manyreach_openapi.json` — Spec OpenAPI complète de ManyReach v2
- `logs/` — Logs JSONL d'exécution (gitignored)

## Sécurité

- `.env` est dans `.gitignore` — JAMAIS commit la clé API
- Token webhook (phase 2) sera généré fort (32+ chars random) et stocké en env var Vercel
- Blacklist immédiate sur intent `unsubscribe` — non négociable (RGPD)
- Mode `--dry-run` par défaut pour les premiers runs
