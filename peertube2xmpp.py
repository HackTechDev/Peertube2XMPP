#!/usr/bin/env python3
"""Annonce les nouvelles vidéos d'un compte PeerTube dans un salon XMPP (MUC).

À lancer périodiquement (cron, timer systemd). Au premier lancement, les vidéos
existantes sont mémorisées sans être annoncées.

Exemples :
    peertube2xmpp.py                  # annonce les nouvelles vidéos
    peertube2xmpp.py -c config.ini    # utilise un fichier de configuration précis
    peertube2xmpp.py --dry-run        # affiche les messages sans les envoyer
    peertube2xmpp.py --video cw5EuTUeyHhaKtZyc5S1R9   # annonce une vidéo précise
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Dossier du script : sert de repli pour le fichier INI et le fichier d'état,
# quel que soit le dossier courant (cron lance souvent depuis $HOME).
SCRIPT_DIR = Path(__file__).resolve().parent

# Emplacements où chercher le fichier de configuration, par ordre de priorité.
# Les chemins inexistants sont simplement ignorés par ConfigParser.read().
DEFAULT_CONFIG_PATHS = [
    os.environ.get("PEERTUBE2XMPP_CONFIG", ""),
    os.path.expanduser("~/.config/peertube2xmpp.ini"),
    str(SCRIPT_DIR / "peertube2xmpp.ini"),
]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    """Paramètres du script, lus depuis le fichier INI et l'environnement."""

    # Compte XMPP du bot (obligatoires, sauf en --dry-run)
    jid: str
    password: str
    # Salon cible et pseudo utilisé pour le rejoindre
    room: str = "hacktechdev@conference.tarentule.us"
    nick: str = "PeerTube"
    # Instance et compte PeerTube surveillés
    url: str = "https://peertube.hacktech.dev"
    account: str = "administrateur"
    # Fichier mémorisant les vidéos déjà annoncées
    state_file: str = str(SCRIPT_DIR / "seen.json")
    # Modèle du message ; variables : {title} {url} {channel} {duration}
    message: str = "📺 Nouvelle vidéo : {title}\n{url}"

    @classmethod
    def load(cls, path: str | None) -> "Config":
        """Construit la configuration.

        Si `path` est fourni, seul ce fichier est lu ; sinon on essaie
        DEFAULT_CONFIG_PATHS. Une variable d'environnement PEERTUBE2XMPP_*
        l'emporte toujours sur la valeur du fichier.
        """
        # interpolation=None : un « % » dans le mot de passe ne doit pas être
        # interprété comme une référence à une autre clé.
        parser = configparser.ConfigParser(interpolation=None)
        paths = [path] if path else [p for p in DEFAULT_CONFIG_PATHS if p]
        # read() renvoie la liste des fichiers effectivement lus. Sans fichier,
        # on continue avec les valeurs par défaut, sauf si -c désignait un
        # fichier précis : il s'agit alors sûrement d'une faute de frappe.
        if not parser.read(paths, encoding="utf-8") and path:
            sys.exit(f"Fichier de configuration introuvable : {path}")
        # Une section absente est remplacée par un dict vide, pour que get()
        # retombe sur les valeurs par défaut.
        xmpp = parser["xmpp"] if parser.has_section("xmpp") else {}
        peertube = parser["peertube"] if parser.has_section("peertube") else {}

        def get(section, key, default=""):
            """Valeur de `key` : variable d'environnement, puis fichier, puis défaut."""
            # Ex. la clé « password » correspond à PEERTUBE2XMPP_PASSWORD.
            return os.environ.get(f"PEERTUBE2XMPP_{key.upper()}") or section.get(key, default)

        # Accepte aussi la forme URI « xmpp:salon@serveur?join ».
        room = get(xmpp, "room", cls.room).removeprefix("xmpp:").split("?")[0]
        return cls(
            jid=get(xmpp, "jid"),
            password=get(xmpp, "password"),
            room=room,
            nick=get(xmpp, "nick", cls.nick),
            # Le « / » final est retiré pour éviter un « // » dans les URL.
            url=get(peertube, "url", cls.url).rstrip("/"),
            account=get(peertube, "account", cls.account),
            # « ~ » est développé pour accepter ~/.local/share/… dans le fichier INI.
            state_file=os.path.expanduser(get(peertube, "state_file", cls.state_file)),
            # Dans un fichier INI, le saut de ligne s'écrit littéralement « \n ».
            message=get(peertube, "message", cls.message).replace("\\n", "\n"),
        )


def log(msg: str, file=sys.stdout) -> None:
    """Affiche `msg` précédé de la date et de l'heure (utile dans le log de cron)."""
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=file)


# --------------------------------------------------------------------------- #
# PeerTube
# --------------------------------------------------------------------------- #
# Chaque vidéo renvoyée par l'API est un dict JSON. Champs utilisés ici :
#   uuid       identifiant long et stable, mémorisé dans le fichier d'état
#   shortUUID  identifiant court, utilisé dans les liens /w/<id>
#   name       titre de la vidéo
#   duration   durée en secondes
#   channel    chaîne de publication (dict, dont « displayName »)
# Les appels se font sans authentification : seules les vidéos publiques (et
# non listées, pour --video) sont visibles.


def fetch_videos(cfg: Config):
    """Dernières vidéos publiques du compte, de la plus ancienne à la plus récente."""
    # L'API trie de la plus récente à la plus ancienne (sort=-publishedAt).
    # 20 vidéos suffisent largement entre deux lancements de cron.
    url = f"{cfg.url}/api/v1/accounts/{cfg.account}/videos?sort=-publishedAt&count=20"
    with urllib.request.urlopen(url, timeout=30) as resp:
        # Ordre inversé : si plusieurs vidéos sont annoncées d'un coup, elles
        # apparaissent dans le salon dans leur ordre de publication.
        return list(reversed(json.load(resp)["data"]))


def fetch_video(cfg: Config, video_id: str):
    """Vidéo désignée par son id (ex. cw5EuTUeyHhaKtZyc5S1R9) ou par son URL."""
    # « https://…/w/cw5EuTUeyHhaKtZyc5S1R9 » → « cw5EuTUeyHhaKtZyc5S1R9 »
    # (paramètres « ?start=… » et « / » final retirés, puis dernier segment).
    video_id = video_id.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    # Cette route accepte indifféremment l'id court, l'UUID ou l'id numérique.
    # Une vidéo inconnue lève urllib.error.HTTPError, gérée dans main().
    url = f"{cfg.url}/api/v1/videos/{video_id}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def format_message(cfg: Config, video) -> str:
    """Texte à poster dans le salon pour `video`, d'après le modèle `message`."""
    # Durée affichée en « minutes:secondes », ex. 90 s → « 1:30 »
    minutes, seconds = divmod(video["duration"], 60)
    return cfg.message.format(
        title=video["name"],
        # Lien court : https://instance/w/cw5EuTUeyHhaKtZyc5S1R9
        url=f"{cfg.url}/w/{video['shortUUID']}",
        channel=video["channel"]["displayName"],
        duration=f"{minutes}:{seconds:02d}",
    )


# --------------------------------------------------------------------------- #
# XMPP
# --------------------------------------------------------------------------- #
def send_to_muc(cfg: Config, messages: list[str]) -> bool:
    """Rejoint le salon, envoie `messages` puis se déconnecte. Renvoie True si envoyé."""
    # Import tardif : --dry-run fonctionne même sans slixmpp installé.
    import slixmpp

    class Notifier(slixmpp.ClientXMPP):
        """Client XMPP éphémère : une connexion pour un seul envoi."""

        def __init__(self):
            super().__init__(cfg.jid, cfg.password)
            # Passe à True une fois les messages envoyés : c'est ce que
            # renvoie send_to_muc().
            self.sent = False
            self.register_plugin("xep_0045")  # MUC (salons de discussion)
            self.add_event_handler("session_start", self.start)
            # Mauvais JID ou mot de passe : on se déconnecte, sent reste False.
            self.add_event_handler("failed_auth", lambda _: self.disconnect())

        async def start(self, _):
            """Session ouverte : rejoint le salon et envoie les messages."""
            # Attend la confirmation du salon ; maxstanzas=0 évite de
            # recevoir l'historique des messages déjà postés.
            await self.plugin["xep_0045"].join_muc_wait(cfg.room, cfg.nick, maxstanzas=0)
            for body in messages:
                # « groupchat » : message adressé à tout le salon
                self.send_message(mto=cfg.room, mbody=body, mtype="groupchat")
            self.sent = True
            self.disconnect()

    xmpp = Notifier()
    xmpp.connect()
    # Bloque jusqu'à la déconnexion, qu'elle suive l'envoi ou un échec.
    xmpp.loop.run_until_complete(xmpp.disconnected)
    return xmpp.sent


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    """Point d'entrée : renvoie le code de sortie du script (0 ou 1)."""
    # --- Arguments de la ligne de commande
    # La description reprend le premier paragraphe de la docstring du module.
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="fichier de configuration INI")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="affiche les messages sans les envoyer")
    ap.add_argument("--video", metavar="ID",
                    help="annonce cette vidéo (id ou URL), même si elle a déjà été vue")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    # Fichier d'état : liste JSON des UUID des vidéos déjà vues ou annoncées.
    state_file = Path(cfg.state_file)

    # --- Vidéos à annoncer
    if args.video:
        # Vidéo demandée explicitement : annoncée même si elle a déjà été vue.
        try:
            new = [fetch_video(cfg, args.video)]
        except urllib.error.HTTPError as exc:
            log(f"Vidéo {args.video} introuvable sur {cfg.url} (HTTP {exc.code}).",
                file=sys.stderr)
            return 1
        seen = set(json.loads(state_file.read_text())) if state_file.exists() else set()
    else:
        videos = fetch_videos(cfg)
        # Premier lancement : on mémorise l'existant sans rien annoncer, pour
        # ne pas inonder le salon avec des vidéos anciennes.
        if not state_file.exists():
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps([v["uuid"] for v in videos]))
            log(f"Initialisation : {len(videos)} vidéos existantes mémorisées, rien annoncé.")
            return 0

        # Nouvelles vidéos = celles dont l'UUID n'est pas encore dans le fichier
        seen = set(json.loads(state_file.read_text()))
        new = [v for v in videos if v["uuid"] not in seen]
        if not new:
            # Toujours écrire une ligne, pour voir dans le log que la tâche a tourné.
            log(f"Aucune nouvelle vidéo ({len(seen)} déjà vues).")
            return 0

    # --- Préparation des messages (un par vidéo)
    messages = [format_message(cfg, v) for v in new]
    if args.dry_run:
        # Affichage sans date : c'est le texte exact qui serait posté.
        # Le fichier d'état n'est pas modifié.
        print("\n\n".join(messages))
        return 0

    # --- Envoi
    # Les identifiants ne sont vérifiés qu'ici : --dry-run n'en a pas besoin.
    if not cfg.jid or not cfg.password:
        log("JID/mot de passe XMPP manquants (voir peertube2xmpp.ini.example).",
            file=sys.stderr)
        return 1
    if not send_to_muc(cfg, messages):
        # Le fichier d'état n'est pas mis à jour : ces vidéos seront de
        # nouveau proposées au prochain lancement.
        log("Échec de l'envoi XMPP, vidéos non marquées comme annoncées.",
            file=sys.stderr)
        return 1

    # --- Mise à jour du fichier d'état
    # Pas de création du fichier ici : avec --video avant l'initialisation, il ne
    # contiendrait qu'une vidéo et le lancement suivant annoncerait toutes les autres.
    if state_file.exists():
        # Trié, pour un fichier stable et lisible
        state_file.write_text(json.dumps(sorted(seen | {v["uuid"] for v in new})))
    log(f"{len(new)} vidéo(s) annoncée(s) dans {cfg.room}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
