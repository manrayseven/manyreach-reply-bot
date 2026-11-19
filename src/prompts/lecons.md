# CAS TRANCHÉS PAR RUDY — ils font autorité

Ces exemples viennent de VRAIES réponses où le bot s'est trompé et où Rudy a
corrigé. Ils priment sur ton raisonnement : si une réponse ressemble à l'un
d'eux, classe-la pareil.

Rudy ne veut que trois issues, et il les a formulées ainsi :

- **INTÉRÊT** → un intent d'alerte. Il traite à la main.
- **PAS D'INTÉRÊT** → `not_interested_polite`. Réponse type automatique, et
  SURTOUT pas d'alerte : son tableau de bord doit rester propre.
- **OBJECTION** (un « non » qui n'est pas définitif, qui se discute) → un intent
  d'alerte. C'est une vente à faire, pas un refus.

La ligne de partage n'est pas la politesse du message ni sa longueur : c'est
**est-ce que la porte est fermée ou entrouverte**.

---

## Ce sont des REFUS → `not_interested_polite` (réponse auto, aucune alerte)

Un refus reste un refus même sans le mot « non », même très poli, même quand il
donne une raison. Ne le classe pas « demande d'infos » sous prétexte qu'aucune
formule de refus toute faite n'apparaît.

> « Je vous remercie pour votre sollicitation mais nous ne ressentons pas pour
> l'instant le besoin de faire appel à vos services. »
> — Éliane Zimmer, 12/11. Classé « demande d'infos » À TORT.

> « Merci pour ce message, mais je suis déjà accompagnée pour mon site et ma
> visibilité. Belle continuation. »
> — Linda Kortylak, 12/11. Classé « demande d'infos » À TORT. Déjà équipé et
> sans réserve = refus, pas une objection à travailler.

> « Ce n'est pas d'actualité pour l'instant. Cordialement. »
> — Jean Duboc, 12/11. Classé « demande d'infos » À TORT. « Pas d'actualité »,
> « pas pour le moment » sans demande de recontact = refus.

> « Merci pour votre proposition. Ma fille qui est directrice artistique
> graphisme gère mes media. »
> — Yannick Guerton, 12/11. Quelqu'un d'autre s'en occupe déjà, sans ouverture
> = refus. Le fait qu'il nomme la personne n'en fait pas une piste.

## C'est une OBJECTION → intent d'alerte (jamais de réponse auto)

Le prospect explique pourquoi ça ne colle pas, mais rien n'est fermé : il
raisonne, il nuance, il décrit sa contrainte. Il y a un argument à lui opposer.

> « J'apprécie votre message, mais cela ne convient pas car je dois filtrer les
> clients, beaucoup recherchant malheureusement des services sexuels. Par
> conséquent, je dois prendre contact avec eux pour comprendre leurs
> véritables intentions. »
> — Kalli Tantra, 12/11. Classé « signal de RDV » À TORT (mauvais libellé), mais
> l'alerte était la bonne issue : c'est une objection métier qui se répond.

> « Qui tombe à pic, car je suis à la recherche d'une personne avec vos
> compétences. […] Mon souci comme beaucoup est le manque de trésorerie pour
> honorer vos honoraires. »
> — Karine-Dita Madrid, 06/11. Clôturé À TORT comme une objection de prix. Un
> besoin exprimé n'est JAMAIS un refus, même avec une réserve de budget.

## Ce n'est NI l'un NI l'autre : une annonce d'adresse → `bounce_or_auto`

Le message ne parle pas de l'offre. Il ne dit ni oui ni non : il indique où
écrire désormais, ou que la personne est partie. Ni alerte, ni réponse type —
`bounce_or_auto`, silence. Lead Finder réinscrit la nouvelle adresse dans la
campagne et désinscrit l'ancienne ; une alerte ferait perdre les deux.

> « Notre adresse email (CONTACT) a changée ! Merci d'envoyer vos mails à :
> contact@<nouveau-domaine>.fr / et de supprimer : contact.<ancien>@gmail.com »
> — Farid Mehdi, 16/11. Remonté en alerte À TORT.

Le repère sûr : **le message cite l'adresse d'où il vous écrit**. On ne rappelle
son adresse actuelle que pour dire de ne plus s'en servir. Même famille : « j'ai
quitté la société », « cette boîte n'est plus consultée », « votre nouvel
interlocuteur est X ».

Attention : une personne qui dit « ce n'est pas moi, voyez avec M. X » PARLE de
l'offre et la décline — c'est `wrong_person_redirect`, pas une annonce d'adresse.

## Le test, quand tu hésites

Demande-toi : **si Rudy répondait lui-même, aurait-il quelque chose à dire qui
puisse changer l'issue ?**

- Non, la personne a tranché → refus → réponse type, pas d'alerte.
- Oui, il y a un argument à opposer, une précision à apporter, un besoin à
  creuser → alerte.

Et dans le doute sur un message LONG ou confus, alerte : une alerte de trop se
ferme d'un clic, un lead clôturé est perdu.
