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

SCRIPT_DIR = Path(__file__).resolve().parent

# Emplacements où chercher le fichier de configuration, par ordre de priorité.
# Les chemins inexistants sont simplement ignorés par ConfigParser.read().
DEFAULT_CONFIG_PATHS = [
    os.environ.get("PEERTUBE2XMPP_CONFIG", ""),
    os.path.expanduser("~/.config/peertube2xmpp.ini"),
    str(SCRIPT_DIR / "peertube2xmpp.ini"),
]


@dataclass
class Config:
    """Paramètres du script, lus depuis le fichier INI et l'environnement."""

    # Compte XMPP du bot (obligatoires, sauf en --dry-run)
    jid: str
    password: str
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
        if not parser.read(paths, encoding="utf-8") and path:
            sys.exit(f"Fichier de configuration introuvable : {path}")
        xmpp = parser["xmpp"] if parser.has_section("xmpp") else {}
        peertube = parser["peertube"] if parser.has_section("peertube") else {}

        def get(section, key, default=""):
            """Valeur de `key` : variable d'environnement, puis fichier, puis défaut."""
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
            state_file=os.path.expanduser(get(peertube, "state_file", cls.state_file)),
            # Dans un fichier INI, le saut de ligne s'écrit littéralement « \n ».
            message=get(peertube, "message", cls.message).replace("\\n", "\n"),
        )


def log(msg: str, file=sys.stdout) -> None:
    """Affiche `msg` précédé de la date et de l'heure (utile dans le log de cron)."""
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=file)


def fetch_videos(cfg: Config):
    """Dernières vidéos publiques du compte, de la plus ancienne à la plus récente."""
    url = f"{cfg.url}/api/v1/accounts/{cfg.account}/videos?sort=-publishedAt&count=20"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return list(reversed(json.load(resp)["data"]))


def fetch_video(cfg: Config, video_id: str):
    """Vidéo désignée par son id (ex. cw5EuTUeyHhaKtZyc5S1R9) ou par son URL."""
    # « https://…/w/cw5EuTUeyHhaKtZyc5S1R9 » → « cw5EuTUeyHhaKtZyc5S1R9 »
    video_id = video_id.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    url = f"{cfg.url}/api/v1/videos/{video_id}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def format_message(cfg: Config, video) -> str:
    minutes, seconds = divmod(video["duration"], 60)
    return cfg.message.format(
        title=video["name"],
        url=video["url"],
        channel=video["channel"]["displayName"],
        duration=f"{minutes}:{seconds:02d}",
    )


def send_to_muc(cfg: Config, messages: list[str]) -> bool:
    """Rejoint le salon, envoie `messages` puis se déconnecte. Renvoie True si envoyé."""
    import slixmpp

    class Notifier(slixmpp.ClientXMPP):
        def __init__(self):
            super().__init__(cfg.jid, cfg.password)
            self.sent = False
            self.register_plugin("xep_0045")
            self.add_event_handler("session_start", self.start)
            self.add_event_handler("failed_auth", lambda _: self.disconnect())

        async def start(self, _):
            await self.plugin["xep_0045"].join_muc_wait(cfg.room, cfg.nick, maxstanzas=0)
            for body in messages:
                self.send_message(mto=cfg.room, mbody=body, mtype="groupchat")
            self.sent = True
            self.disconnect()

    xmpp = Notifier()
    xmpp.connect()
    xmpp.loop.run_until_complete(xmpp.disconnected)
    return xmpp.sent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="fichier de configuration INI")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="affiche les messages sans les envoyer")
    ap.add_argument("--video", metavar="ID",
                    help="annonce cette vidéo (id ou URL), même si elle a déjà été vue")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    state_file = Path(cfg.state_file)

    if args.video:
        try:
            new = [fetch_video(cfg, args.video)]
        except urllib.error.HTTPError as exc:
            log(f"Vidéo {args.video} introuvable sur {cfg.url} (HTTP {exc.code}).",
                file=sys.stderr)
            return 1
        seen = set(json.loads(state_file.read_text())) if state_file.exists() else set()
    else:
        videos = fetch_videos(cfg)
        if not state_file.exists():
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps([v["uuid"] for v in videos]))
            log(f"Initialisation : {len(videos)} vidéos existantes mémorisées, rien annoncé.")
            return 0

        seen = set(json.loads(state_file.read_text()))
        new = [v for v in videos if v["uuid"] not in seen]
        if not new:
            # Toujours écrire une ligne, pour voir dans le log que la tâche a tourné.
            log(f"Aucune nouvelle vidéo ({len(seen)} déjà vues).")
            return 0

    messages = [format_message(cfg, v) for v in new]
    if args.dry_run:
        print("\n\n".join(messages))
        return 0

    # Les identifiants ne sont vérifiés qu'ici : --dry-run n'en a pas besoin.
    if not cfg.jid or not cfg.password:
        log("JID/mot de passe XMPP manquants (voir peertube2xmpp.ini.example).",
            file=sys.stderr)
        return 1
    if not send_to_muc(cfg, messages):
        log("Échec de l'envoi XMPP, vidéos non marquées comme annoncées.",
            file=sys.stderr)
        return 1

    # Pas de création du fichier ici : avec --video avant l'initialisation, il ne
    # contiendrait qu'une vidéo et le lancement suivant annoncerait toutes les autres.
    if state_file.exists():
        state_file.write_text(json.dumps(sorted(seen | {v["uuid"] for v in new})))
    log(f"{len(new)} vidéo(s) annoncée(s) dans {cfg.room}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
