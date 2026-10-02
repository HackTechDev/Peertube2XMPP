# peertube2xmpp

Annonce automatiquement les nouvelles vidéos d'un compte PeerTube dans un salon
de discussion XMPP (MUC).

Par défaut, il surveille le compte `administrateur` de
<https://peertube.hacktech.dev> et poste dans `hacktechdev@conference.tarentule.us`
un message de la forme :

```
📺 Nouvelle vidéo : Titre de la vidéo
https://peertube.hacktech.dev/videos/watch/…
```

## Fonctionnement

PeerTube n'envoie pas de notification sortante quand une vidéo est publiée. Le
script est donc conçu pour être lancé périodiquement (cron, timer systemd) :

1. il récupère les 20 dernières vidéos publiques du compte via l'API PeerTube
   (les vidéos privées, non listées ou encore en transcodage sont ignorées) ;
2. il les compare à celles déjà vues, enregistrées dans `seen.json` ;
3. s'il y a des nouveautés, il se connecte en XMPP, rejoint le salon, poste un
   message par vidéo puis se déconnecte.

Une vidéo n'est marquée comme annoncée qu'une fois le message envoyé : en cas
d'échec, elle sera reproposée au lancement suivant.

Au **premier lancement**, les vidéos déjà en ligne sont simplement mémorisées,
sans être annoncées, pour ne pas inonder le salon.

## Installation

Prérequis : Python 3.8 ou plus récent, et un compte XMPP pour le bot (par exemple
`peertube-bot@tarentule.us`). Si le salon est réservé aux membres, ajoutez ce
compte comme membre.

```bash
git clone <url-du-depot> peertube2xmpp
cd peertube2xmpp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configuration

Toute la configuration passe par des variables d'environnement :

| Variable           | Obligatoire | Valeur par défaut                      |
|--------------------|-------------|----------------------------------------|
| `XMPP_JID`         | oui         |                                        |
| `XMPP_PASSWORD`    | oui         |                                        |
| `XMPP_ROOM`        | non         | `hacktechdev@conference.tarentule.us`  |
| `XMPP_NICK`        | non         | `PeerTube`                             |
| `PEERTUBE_URL`     | non         | `https://peertube.hacktech.dev`        |
| `PEERTUBE_ACCOUNT` | non         | `administrateur`                       |
| `STATE_FILE`       | non         | `seen.json` à côté du script           |

Pour ne pas écrire le mot de passe dans la crontab, placez les variables dans un
fichier `.env` lisible par vous seul (il est ignoré par git) :

```bash
cat > .env <<'EOF'
export XMPP_JID=peertube-bot@tarentule.us
export XMPP_PASSWORD=motdepasse
EOF
chmod 600 .env
```

## Utilisation

Premier lancement (initialisation, rien n'est posté) :

```bash
. ./.env && .venv/bin/python peertube2xmpp.py
```

Afficher les messages qui seraient envoyés, sans se connecter à XMPP :

```bash
.venv/bin/python peertube2xmpp.py --dry-run
```

Lancement automatique toutes les 10 minutes avec cron (`crontab -e`) :

```
*/10 * * * * cd /chemin/vers/peertube2xmpp && . ./.env && .venv/bin/python peertube2xmpp.py >> log.txt 2>&1
```

Le script doit tourner sur une machine allumée en permanence, idéalement le
serveur PeerTube lui-même.
