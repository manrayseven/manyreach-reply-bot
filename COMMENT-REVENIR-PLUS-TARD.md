# Comment retrouver et modifier ce bot plus tard

> Tu as construit ce bot avec Claude Code. Pour y revenir, ajuster les drafts,
> ajouter une feature, ou juste comprendre où ça en est — voici tout ce qu'il
> te faut.

---

## 📍 Où sont les choses

**Le code du bot** :
```
C:\Users\ManRa\Desktop\Rudy111\Claude\Manyreach Answers\
```

**Ce que Claude se rappelle de toi entre les sessions** :
```
C:\Users\ManRa\.claude\projects\C--Users-ManRa\memory\
```
(4 fichiers .md : ton rôle, tes outils, tes entités business, ton style de réponses)

---

## 🔁 Faire tourner le bot (sans Claude)

**Option 1 — Double-clic facile** :
- Double-clique sur `2-LANCER-BOT-DRY-RUN.bat` dans ce dossier
- Le bot tourne en mode test, aucun mail envoyé
- Tu vois les drafts à l'écran

**Option 2 — Voir les drafts récents** :
- Double-clique sur `3-VOIR-DERNIERS-DRAFTS.bat`
- Affiche uniquement les drafts "humains" (filtre bounces / auto-replies)

**Option 3 — Mode REEL (mails envoyés)** :
```powershell
py scripts\run_bot.py --limit 10 --no-dry-run
```
⚠️ Cette commande envoie réellement des replies. Ne l'utilise QUE quand tu as
validé la qualité en dry-run, et que tu as activé les bons intents dans
`src/actions.py` (par défaut, TOUT reste en review).

---

## 🤖 Reparler à Claude pour modifier le bot

**Option 1 — Le plus simple** :
- Double-clique sur `1-OUVRIR-CLAUDE.bat`
- Une fenêtre PowerShell s'ouvre dans le bon dossier
- Tape `claude` (entrée)
- Discute comme tu l'as fait pour construire le bot

**Option 2 — Manuel** :
- Ouvre PowerShell
- `cd "C:\Users\ManRa\Desktop\Rudy111\Claude\Manyreach Answers"`
- `claude`

Claude relit automatiquement ses fichiers de mémoire et se souvient de
toute notre conversation précédente (ton rôle, tes entités, tes préférences).
Tu n'as PAS à tout ré-expliquer.

---

## 🛠️ Exemples de demandes typiques à faire à Claude

Quand tu reviens, voici le genre de phrases qui marchent :

### Améliorer la qualité des drafts
- *"le bot a mal classifié ce reply [colle le reply], il aurait dû dire X"*
- *"sur les replies en allemand, le ton est trop formel, allège-le"*
- *"ajoute un nouvel intent `request_pricing` pour les prospects qui demandent juste un devis"*

### Modifier le comportement
- *"active l'auto-send sur wrong_person_redirect, j'ai validé la qualité"*
- *"je veux que le bot soit silencieux sur not_interested_polite finalement"*
- *"baisse le seuil d'auto-send à 0.85 pour objection_timing"*

### Étendre le bot
- *"branche Google Calendar pour proposer 3 vrais créneaux dans les drafts interested_warm"*
- *"ajoute un mode 'bumps' qui relance les leads `Interested` sans booking après 3 jours"*
- *"déploie le bot sur Vercel pour qu'il tourne en webhook au lieu de polling manuel"*

### Comprendre / debug
- *"montre-moi les 5 derniers drafts générés"*
- *"pourquoi le bot a classé ce reply comme X au lieu de Y ?"*
- *"combien j'ai dépensé en API Claude ce mois ?"*

---

## 📝 Modifier toi-même (sans Claude) les réglages courants

### Changer ta voice ou tes exemples
Édite : `training_examples.md`
Le bot prend ces changements à chaque relance (pas besoin de redémarrer un service).

### Changer les seuils / le comportement
Édite : `config/settings.yaml`
- `min_autosend_confidence` : seuil de confiance pour auto-send (défaut 0.92)
- `silent_on_not_interested` : true/false
- `limit_per_run` : combien de replies traiter par run

### Activer/désactiver l'auto-send par intent
Édite : `src/actions.py`, ligne `AUTOSEND_ELIGIBLE = frozenset({...})`
Dé-commente l'intent que tu veux activer.

### Changer le prompt de classification ou de draft
- `src/prompts/classify.md` — pour le classifier
- `src/prompts/draft.md` — pour le drafter

---

## 🔐 Tes secrets

Ils sont dans `.env` (pas commit dans git, jamais partagé). Si tu veux le voir :
```powershell
notepad .env
```

Contient :
- `MANYREACH_API_KEY` — ta clé ManyReach
- `ANTHROPIC_API_KEY` — ta clé Claude

Si tu changes de clé ManyReach, c'est ici qu'il faut la mettre à jour.

---

## 📊 Logs d'audit

Chaque run crée un fichier `logs/run_YYYYMMDD-HHMMSS.jsonl` avec une ligne JSON
par reply traité. Tu peux :
- Voir les drafts récents : double-clic sur `3-VOIR-DERNIERS-DRAFTS.bat`
- Tout voir en brut : `notepad logs\run_XXX.jsonl`
- Compter les drafts par intent : (demande à Claude, il te fait le grep)

Les logs sont gitignored (ne partent pas sur GitHub).

---

## ❓ En cas de pépin

- **"`python` not recognized"** → utilise `py` à la place. Tous les `.bat` du
  projet utilisent déjà `py`, pas de souci.
- **"`pip` not recognized"** → utilise `py -m pip install ...`
- **API key invalide** → vérifie `.env`, regénère si besoin sur ManyReach ou
  console.anthropic.com.
- **Le bot ne trouve pas mon reply** → augmente la fenêtre : `--since-days 30`
  ou `--since-days 90`. Ou utilise `--reprocess` pour re-traiter les replies
  déjà vus.
- **Tout cassé** → ouvre Claude (`1-OUVRIR-CLAUDE.bat`) et explique le
  symptôme. Je débogue.
