# Setup Google Calendar — guide pas à pas

> Objectif : permettre au bot de lire tes créneaux libres et de créer des
> événements dans ton Google Agenda. ~15 min, à faire une seule fois.
>
> Méthode : compte de service (pas d'écran de consentement, pas de token qui
> expire). Tu crées une "robot-identité" Google et tu partages ton agenda avec elle.

---

## Étape 1 — Créer un projet Google Cloud (2 min)

1. Va sur https://console.cloud.google.com/
2. En haut, clique sur le sélecteur de projet → **Nouveau projet**
3. Nom : `manyreach-bot` (ou ce que tu veux) → **Créer**
4. Attends quelques secondes, puis sélectionne ce projet (en haut)

## Étape 2 — Activer l'API Google Calendar (1 min)

1. Dans la barre de recherche en haut, tape **"Google Calendar API"**
2. Clique sur le résultat → bouton **Activer**

## Étape 3 — Créer le compte de service (3 min)

1. Menu (☰) → **IAM et administration** → **Comptes de service**
2. Bouton **+ Créer un compte de service**
3. Nom : `bot-calendar` → **Créer et continuer**
4. Rôle : tu peux laisser vide (on n'a pas besoin de rôle projet) → **Continuer** → **OK**
5. Tu reviens à la liste. **Copie l'adresse email** du compte de service
   (du type `bot-calendar@manyreach-bot.iam.gserviceaccount.com`).
   ⚠️ Garde-la, on en a besoin à l'étape 5.

## Étape 4 — Télécharger la clé JSON (2 min)

1. Clique sur le compte de service que tu viens de créer
2. Onglet **Clés** → **Ajouter une clé** → **Créer une clé**
3. Type : **JSON** → **Créer**
4. Un fichier `.json` se télécharge. **Renomme-le** `google-service-account.json`
5. **Déplace-le** dans le dossier du projet :
   `C:\Users\ManRa\Desktop\Rudy111\Claude\Manyreach Answers\`
   (il est déjà gitignored, il ne partira jamais sur GitHub)

## Étape 5 — Partager ton agenda avec le compte de service (3 min)

C'est l'étape clé qui donne accès à ton agenda.

1. Va sur https://calendar.google.com/ (avec le compte dont tu veux que le bot gère l'agenda)
2. À gauche, survole ton agenda principal (souvent ton nom/email) → clique les **⋮** → **Paramètres et partage**
3. Section **Partager avec des personnes ou des groupes** → **+ Ajouter des personnes**
4. Colle l'**email du compte de service** (copié à l'étape 3)
5. Permission : choisis **"Apporter des modifications aux événements"**
6. **Envoyer**

## Étape 6 — Remplir le .env (1 min)

Ouvre le fichier `.env` du projet (ou crée-le depuis `.env.example`) et remplis :

```
GOOGLE_SERVICE_ACCOUNT_JSON=google-service-account.json
GOOGLE_CALENDAR_ID=ton-email@gmail.com
```

`GOOGLE_CALENDAR_ID` = l'adresse email de l'agenda que tu as partagé à l'étape 5
(souvent ton adresse Google principale).

## Étape 7 — Installer les librairies + tester (2 min)

Dans PowerShell, dans le dossier du projet :

```powershell
py -m pip install -r requirements.txt
py scripts\check_calendar.py
```

Si tout est bon, tu verras :
```
✅ Connecté à l'agenda : [ton agenda]
📅 Tes 3 prochains créneaux libres :
  • mardi 12 mai à 14h00
  • mardi 12 mai à 15h00
  • mercredi 13 mai à 10h00
```

Si ça affiche une erreur, le message te dit quoi vérifier (souvent : l'agenda
n'a pas été partagé avec le bon email, ou le calendar_id est faux).

---

## Une fois que `check_calendar.py` marche

Dis-le moi (Claude) et je branche le Calendar dans le bot :
- Les drafts "intéressé" proposeront 3 vrais créneaux libres
- Quand un RDV est confirmé, l'event sera créé automatiquement dans ton agenda
  au format : `14.00 Call Cold Email avec [Entreprise]` + email/tél/site dans la description

## Régler tes horaires de travail

Tes créneaux proposés respectent tes horaires définis dans
`config/settings.yaml` (section `calendar` → `working_hours`). Édite-les si besoin
(par défaut : lun-jeu 10h-12h + 14h-18h, vendredi 10h-12h).
