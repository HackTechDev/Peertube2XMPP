# peertube2xmpp

Annonce automatiquement les nouvelles vidéos d'un compte PeerTube dans un salon
de discussion XMPP (MUC).

Par défaut, il surveille le compte `administrateur` de
<https://peertube.hacktech.dev> et poste dans `hacktechdev@conference.tarentule.us`
un message de la forme :

```
📺 Nouvelle vidéo : Titre de la vidéo
https://peertube.hacktech.dev/w/cw5EuTUeyHhaKtZyc5S1R9
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
d'échec, elle sera reproposée au lancement suivant. L'envoi est limité à 30 s
(clé `timeout`) : un serveur XMPP injoignable ne bloque donc jamais la tâche
cron, et l'erreur est écrite dans le log.

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

La configuration se fait dans un fichier INI. Partez de l'exemple fourni :

```bash
cp peertube2xmpp.ini.example ~/.config/peertube2xmpp.ini
chmod 600 ~/.config/peertube2xmpp.ini   # il contient le mot de passe XMPP
```

```ini
[xmpp]
jid = peertubebot@tarentule.us
password = changeme
room = hacktechdev@conference.tarentule.us
nick = PeerTube

[peertube]
url = https://peertube.hacktech.dev
account = administrateur
# state_file = ~/.local/share/peertube2xmpp/seen.json
message = 📺 Nouvelle vidéo : {title}\n{url}
```

Le fichier est cherché, dans cet ordre :

1. le chemin passé avec `-c` / `--config` (seul ce fichier est alors lu) ;
2. le chemin indiqué par la variable `PEERTUBE2XMPP_CONFIG` ;
3. `~/.config/peertube2xmpp.ini` ;
4. `peertube2xmpp.ini` à côté du script (ignoré par git).

| Clé          | Section      | Obligatoire | Valeur par défaut                      |
|--------------|--------------|-------------|----------------------------------------|
| `jid`        | `[xmpp]`     | oui         |                                        |
| `password`   | `[xmpp]`     | oui         |                                        |
| `room`       | `[xmpp]`     | non         | `hacktechdev@conference.tarentule.us`  |
| `nick`       | `[xmpp]`     | non         | `PeerTube`                             |
| `timeout`    | `[xmpp]`     | non         | `30` (secondes)                        |
| `url`        | `[peertube]` | non         | `https://peertube.hacktech.dev`        |
| `account`    | `[peertube]` | non         | `administrateur`                       |
| `state_file` | `[peertube]` | non         | `seen.json` à côté du script           |
| `message`    | `[peertube]` | non         | `📺 Nouvelle vidéo : {title}\n{url}`   |

Le salon peut aussi s'écrire sous forme d'URI (`xmpp:salon@serveur?join`).
Dans `message`, `\n` produit un saut de ligne, et les variables disponibles sont
`{title}`, `{url}`, `{channel}` (nom de la chaîne) et `{duration}` (ex. `1:30`).

Chaque clé peut aussi être fournie par une variable d'environnement
`PEERTUBE2XMPP_<CLÉ>` (ex. `PEERTUBE2XMPP_PASSWORD`), qui l'emporte toujours sur
le fichier.

## Utilisation

Premier lancement (initialisation, rien n'est posté) :

```bash
.venv/bin/python peertube2xmpp.py
```

Afficher les messages qui seraient envoyés, sans se connecter à XMPP :

```bash
.venv/bin/python peertube2xmpp.py --dry-run
```

Annoncer une vidéo précise, même déjà vue (id court, UUID ou URL complète ;
combinable avec `--dry-run`) :

```bash
.venv/bin/python peertube2xmpp.py --video cw5EuTUeyHhaKtZyc5S1R9
.venv/bin/python peertube2xmpp.py --video https://peertube.hacktech.dev/w/cw5EuTUeyHhaKtZyc5S1R9
```

Utiliser un autre fichier de configuration :

```bash
.venv/bin/python peertube2xmpp.py -c /chemin/vers/config.ini
```

Lancement automatique toutes les 10 minutes avec cron (`crontab -e`) :

```
*/10 * * * * /chemin/vers/peertube2xmpp/.venv/bin/python /chemin/vers/peertube2xmpp/peertube2xmpp.py >> /chemin/vers/peertube2xmpp/peertube2xmpp.log 2>&1
```

Le script doit tourner sur une machine allumée en permanence, idéalement le
serveur PeerTube lui-même.
