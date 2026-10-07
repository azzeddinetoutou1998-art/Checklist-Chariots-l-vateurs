# Checklist de prise de poste – Caristes

Application web autonome. Elle n'utilise ni Google, ni Drive, ni Microsoft, et aucun compte n'est demandé aux caristes.

| Qui | Adresse | Accès |
|---|---|---|
| Caristes | `http://VOTRE-SERVEUR:8080/` | Libre, via le QR-code |
| Responsables | `http://VOTRE-SERVEUR:8080/tableau` | Mot de passe |
| Horamètres | `http://VOTRE-SERVEUR:8080/horametres` | Mot de passe |
| Impression des QR-codes | `http://VOTRE-SERVEUR:8080/qr` | Mot de passe |

## Ce que fait l'application

**Côté cariste (téléphone)**
- Il scanne le QR-code et remplit la checklist, sans compte et sans application à installer.
- Le numéro du chariot est pré-rempli s'il scanne l'étiquette collée sur le chariot.
- Le téléphone retient le nom, le site, le fournisseur et l'activité pour la fois suivante.
- Le commentaire est obligatoire dès qu'un point est non conforme ou que le chariot ne peut pas rouler.
- Il peut joindre jusqu'à 4 photos de la panne, prises directement avec le téléphone et compressées automatiquement.
- Sans réseau, la checklist est gardée sur le téléphone et envoyée automatiquement au retour du réseau.

**Côté responsable (tableau de bord, actualisé toutes les 5 secondes)**
- Indicateurs : nombre de checklists, chariots contrôlés, chariots à l'arrêt, anomalies à traiter, taux de conformité.
- Une alerte s'affiche à l'écran quand un cariste déclare un chariot « NON roulant ».
- Liste « À traiter » : vous passez chaque anomalie en « Pris en charge » puis « Résolu », avec une note.
- Fil des commentaires et des photos des caristes (cliquez sur une photo pour l'agrandir). Les photos ne sont visibles qu'avec le mot de passe.
- Graphique des checklists par jour et classement des points les plus souvent non conformes.
- Filtres par période, site, fournisseur et numéro de chariot.
- Export Excel (CSV) avec les mêmes colonnes que votre fiche actuelle.

**Horamètres (page /horametres)**
- Les heures sont calculées entre deux relevés successifs d'un même chariot. Exemple : 8 555 h le 07/10 puis 8 560 h le 08/10 donnent 5 h sur 1 jour.
- Moyenne quotidienne par chariot : total des heures divisé par les jours écoulés entre ses relevés.
- Moyenne quotidienne par activité : heures par jour et par chariot. Les heures sont comptées dans l'activité déclarée au premier des deux relevés.
- Deux bases au choix : jours calendaires (week-ends inclus) ou jours ouvrés (lundi à vendredi).
- Courbe du compteur et barres d'heures par jour pour chaque chariot.
- Relevés incohérents (compteur qui baisse, plus de 24 h par jour) : signalés et exclus des moyennes.
- Dans le formulaire, le cariste voit le dernier relevé du chariot. Une saisie incohérente lui demande de confirmer.
- Export Excel détaillé : chaque intervalle entre deux relevés, plus les synthèses par chariot et par activité.

## Installation

Python 3.9 ou plus récent suffit, sans aucune autre installation. Les données sont dans le dossier `data/` (base `checklists.db` et dossier `photos/`) : sauvegardez ce dossier entier régulièrement.

### Option A – Un PC ou serveur du site (gratuit)
1. Installez Python depuis python.org (cochez « Add Python to PATH »).
2. Ouvrez `demarrer-windows.bat` avec le Bloc-notes et **changez le mot de passe**.
3. Double-cliquez sur `demarrer-windows.bat`. Le PC doit rester allumé.
4. Les téléphones doivent être sur le **Wi-Fi du site** pour joindre ce PC. Demandez au service informatique l'adresse IP fixe du PC et d'autoriser le port 8080 dans le pare-feu.

Limite : avec cette option, les caristes en 4G hors du Wi-Fi ne peuvent pas envoyer. Leurs checklists restent en attente sur le téléphone jusqu'au retour sur le Wi-Fi.

### Option B – Hébergement en ligne (recommandé, environ 5 € par mois)
Prenez un petit serveur VPS (OVH, Scaleway, Hostinger…) ou un hébergeur Docker (Railway, Render, Fly.io…).
```
docker build -t checklist-caristes .
docker run -d --restart=always -p 8080:8080 -e ADMIN_PASSWORD="VotreMotDePasse" -v checklist-data:/data checklist-caristes
```
Placez-le derrière un nom de domaine en **HTTPS** (par exemple Caddy ou le HTTPS fourni par l'hébergeur). Le mot de passe du tableau de bord ne doit jamais circuler sans HTTPS sur internet.

## Personnalisation : `config.json`
- `sites` et `activites` : remplissez les listes pour afficher des menus déroulants à la place du texte libre (`"sites": ["La Pallice", "Chef de Baie"]`).
- `points` : ajoutez, retirez ou renommez des points de contrôle.
- `nom_entreprise` : affiché en haut du formulaire.

Redémarrez l'application après chaque modification.

## Utilisation avec les guns (terminaux de scan Zebra, Honeywell…)
1. Sur chaque gun, ouvrez le navigateur (Chrome ou Enterprise Browser) à l'adresse du formulaire, puis ajoutez-la à l'écran d'accueil ou en page de démarrage.
2. Le cariste ouvre le formulaire et scanne l'étiquette du chariot. Le numéro se remplit tout seul, que le gun lise le QR-code ou le code-barres.
3. La touche Entrée envoyée par le gun après le scan passe au champ suivant ; elle n'envoie jamais le formulaire par erreur.
4. Réglage du gun (DataWedge chez Zebra) : sortie « clavier » (keystroke), suffixe Entrée conseillé, symbologies QR Code et Code 128 activées.

Les photos fonctionnent si le gun a un appareil photo. Les très anciens terminaux sous Windows CE ont un navigateur trop ancien : il faut des terminaux Android.

## QR-codes
Ouvrez `/qr`, vérifiez l'adresse publique, puis imprimez :
- l'affiche générale (poste de charge, local caristes) ;
- des étiquettes par chariot, avec le numéro pré-rempli, à coller dans chaque cabine.
