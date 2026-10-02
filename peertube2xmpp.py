#!/usr/bin/env python3
"""Annonce les nouvelles vidéos d'un compte PeerTube dans un salon XMPP (MUC).

À lancer périodiquement (cron, timer systemd). Au premier lancement, les vidéos
existantes sont mémorisées sans être annoncées.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

import slixmpp

PEERTUBE_URL = os.environ.get("PEERTUBE_URL", "https://peertube.hacktech.dev")
PEERTUBE_ACCOUNT = os.environ.get("PEERTUBE_ACCOUNT", "administrateur")
XMPP_ROOM = os.environ.get("XMPP_ROOM", "hacktechdev@conference.tarentule.us")
XMPP_NICK = os.environ.get("XMPP_NICK", "PeerTube")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("seen.json")))


def fetch_videos():
    """Dernières vidéos publiques du compte, de la plus ancienne à la plus récente."""
    url = f"{PEERTUBE_URL}/api/v1/accounts/{PEERTUBE_ACCOUNT}/videos?sort=-publishedAt&count=20"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return list(reversed(json.load(resp)["data"]))


def format_message(video):
    return f"📺 Nouvelle vidéo : {video['name']}\n{video['url']}"


class Notifier(slixmpp.ClientXMPP):
    def __init__(self, jid, password, messages):
        super().__init__(jid, password)
        self.messages = messages
        self.sent = False
        self.register_plugin("xep_0045")
        self.add_event_handler("session_start", self.start)
        self.add_event_handler("failed_auth", lambda _: self.disconnect())

    async def start(self, _):
        await self.plugin["xep_0045"].join_muc_wait(XMPP_ROOM, XMPP_NICK, maxstanzas=0)
        for body in self.messages:
            self.send_message(mto=XMPP_ROOM, mbody=body, mtype="groupchat")
        self.sent = True
        self.disconnect()


def main():
    dry_run = "--dry-run" in sys.argv
    videos = fetch_videos()

    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps([v["uuid"] for v in videos]))
        print(f"Initialisation : {len(videos)} vidéos existantes mémorisées, rien annoncé.")
        return

    seen = set(json.loads(STATE_FILE.read_text()))
    new = [v for v in videos if v["uuid"] not in seen]
    if not new:
        return

    messages = [format_message(v) for v in new]
    if dry_run:
        print("\n\n".join(messages))
        return

    xmpp = Notifier(os.environ["XMPP_JID"], os.environ["XMPP_PASSWORD"], messages)
    xmpp.connect()
    xmpp.loop.run_until_complete(xmpp.disconnected)
    if not xmpp.sent:
        sys.exit("Échec de l'envoi XMPP, vidéos non marquées comme annoncées.")

    STATE_FILE.write_text(json.dumps(sorted(seen | {v["uuid"] for v in new})))


if __name__ == "__main__":
    main()
