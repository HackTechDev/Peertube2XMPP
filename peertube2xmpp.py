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
import asyncio
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
    # Durée maximale (secondes) de la connexion et de l'envoi XMPP
    timeout: float = 30
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

        # Les erreurs de configuration arrêtent le script avec un message
        # clair, plutôt qu'avec une trace Python au milieu du traitement.
        raw_timeout = get(xmpp, "timeout", cls.timeout)
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = 0
        # « not > 0 » rejette aussi nan
        if not timeout > 0:
            sys.exit(f"timeout invalide : {raw_timeout!r} (nombre de secondes positif attendu)")

        message = get(peertube, "message", cls.message).replace("\\n", "\n")
        # Essai du modèle avec des valeurs fictives : une variable inconnue
        # (ex. {titre}) ou une accolade isolée est signalée dès le lancement,
        # même s'il n'y a aucune vidéo à annoncer.
        try:
            message.format(title="", url="", channel="", duration="")
        except (KeyError, IndexError, ValueError, AttributeError) as exc:
            sys.exit(f"message invalide : {message!r} ({type(exc).__name__} : {exc}). "
                     "Variables disponibles : {title} {url} {channel} {duration} ; "
                     "doubler les accolades littérales ({{ et }}).")

        return cls(
            jid=get(xmpp, "jid"),
            password=get(xmpp, "password"),
            room=room,
            nick=get(xmpp, "nick", cls.nick),
            timeout=timeout,
            # Le « / » final est retiré pour éviter un « // » dans les URL.
            url=get(peertube, "url", cls.url).rstrip("/"),
            account=get(peertube, "account", cls.account),
            # « ~ » est développé pour accepter ~/.local/share/… dans le fichier INI.
            state_file=os.path.expanduser(get(peertube, "state_file", cls.state_file)),
            # Dans un fichier INI, le saut de ligne s'écrit littéralement « \n ».
            message=message,
        )


def log(msg: str, file=sys.stdout) -> None:
    """Affiche `msg` précédé de la date et de l'heure (utile dans le log de cron)."""
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=file)


def describe(exc) -> str:
    """Texte d'une erreur, ou son type si le texte est vide (ex. TimeoutError())."""
    # « exc or … » ne suffirait pas : une exception est toujours « vraie »,
    # même quand son texte est vide.
    return str(exc) or type(exc).__name__


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


def peertube_error(cfg: Config, exc: Exception) -> int:
    """Journalise une erreur d'accès à PeerTube et renvoie le code de sortie 1.

    OSError couvre l'instance injoignable (DNS, connexion refusée, délai
    dépassé…) et ValueError une réponse qui n'est pas du JSON valide.
    """
    # URLError range la cause réelle dans .reason
    reason = getattr(exc, "reason", None) or exc
    log(f"PeerTube injoignable ({cfg.url}) : {describe(reason)}", file=sys.stderr)
    return 1


def load_seen(state_file: Path) -> set[str]:
    """UUID mémorisés dans le fichier d'état (ensemble vide s'il n'existe pas).

    Lève OSError si le fichier ne peut pas être lu, ValueError si son contenu
    n'est pas une liste JSON de chaînes (fichier tronqué, modifié à la main…).
    """
    if not state_file.exists():
        return set()
    data = json.loads(state_file.read_text())
    if not isinstance(data, list) or not all(isinstance(u, str) for u in data):
        raise ValueError("liste JSON d'UUID attendue")
    return set(data)


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
def send_to_muc(cfg: Config, messages: list[str]) -> None:
    """Rejoint le salon, envoie `messages` puis se déconnecte.

    Lève une exception en cas d'échec (authentification, salon, réseau) ou si
    l'ensemble dépasse cfg.timeout secondes.
    """
    # Import tardif : --dry-run fonctionne même sans slixmpp installé.
    import slixmpp

    class Notifier(slixmpp.ClientXMPP):
        """Client XMPP éphémère : une connexion pour un seul envoi."""

        def __init__(self):
            super().__init__(cfg.jid, cfg.password)
            # Future résolue quand l'envoi est terminé (ou a échoué) : c'est
            # elle qu'attend send_to_muc(). Chaque gestionnaire vérifie done()
            # car une future ne peut être résolue qu'une seule fois.
            self.done = self.loop.create_future()
            # Dernière erreur de connexion : slixmpp réessaie de se connecter
            # en boucle, on ne la signale donc qu'en cas de délai dépassé.
            self.last_error = None
            self.register_plugin("xep_0045")  # MUC (salons de discussion)
            self.add_event_handler("session_start", self.start)
            self.add_event_handler("failed_auth", self.on_failed_auth)
            self.add_event_handler("connection_failed", self.on_connection_failed)
            # Connexion établie : une erreur d'une tentative précédente
            # (ex. IPv6 avant IPv4) n'est plus pertinente.
            self.add_event_handler("connected", lambda _: setattr(self, "last_error", None))
            self.add_event_handler("disconnected", self.on_disconnected)

        def fail(self, exc: Exception) -> None:
            """Marque l'envoi comme échoué (sans effet s'il est déjà terminé)."""
            if not self.done.done():
                self.done.set_exception(exc)

        async def start(self, _):
            """Session ouverte : rejoint le salon et envoie les messages."""
            try:
                # Attend la confirmation du salon ; maxstanzas=0 évite de
                # recevoir l'historique des messages déjà postés. Lève une
                # exception si le salon refuse l'entrée.
                await self.plugin["xep_0045"].join_muc_wait(
                    cfg.room, cfg.nick, maxstanzas=0, timeout=cfg.timeout
                )
                for body in messages:
                    # « groupchat » : message adressé à tout le salon
                    self.send_message(mto=cfg.room, mbody=body, mtype="groupchat")
                if not self.done.done():
                    self.done.set_result(None)
            except Exception as exc:
                self.fail(RuntimeError(f"impossible de rejoindre {cfg.room} : "
                                       f"{describe(exc)}"))

        def on_failed_auth(self, _):
            """Le serveur a refusé le JID ou le mot de passe."""
            self.fail(RuntimeError("authentification XMPP refusée"))

        def on_connection_failed(self, error):
            """Échec d'une tentative de connexion (DNS, port fermé…)."""
            self.last_error = error

        def on_disconnected(self, _):
            """Déconnexion avant la fin de l'envoi : considérée comme un échec.

            Après un envoi réussi, `done` est déjà résolue et rien ne change.
            """
            self.fail(RuntimeError("déconnecté du serveur XMPP"))

    xmpp = Notifier()
    xmpp.connect()
    try:
        # Le délai global couvre la connexion, l'authentification et l'envoi.
        xmpp.loop.run_until_complete(asyncio.wait_for(xmpp.done, cfg.timeout))
    except asyncio.TimeoutError:
        detail = f", dernière erreur : {xmpp.last_error}" if xmpp.last_error else ""
        raise RuntimeError(f"délai de {cfg.timeout:g} s dépassé{detail}") from None
    finally:
        # Dans tous les cas : arrête les tentatives de reconnexion, puis ferme
        # la connexion après l'envoi des messages en attente (2 s au plus).
        xmpp.cancel_connection_attempt()
        xmpp.loop.run_until_complete(xmpp.disconnect())
        # Annule les tâches internes de slixmpp encore actives, pour éviter
        # l'avertissement « Task was destroyed but it is pending » en sortie.
        pending = asyncio.all_tasks(xmpp.loop)
        for task in pending:
            task.cancel()
        xmpp.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


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
    try:
        seen = load_seen(state_file)
    except (OSError, ValueError) as exc:
        # On ne le réinitialise pas d'office : c'est à l'utilisateur de décider
        # (le supprimer revient à refaire un premier lancement).
        log(f"Fichier d'état illisible ({state_file}) : {describe(exc)}. "
            "Corrigez-le, ou supprimez-le pour réinitialiser.", file=sys.stderr)
        return 1

    # --- Vidéos à annoncer
    if args.video:
        # Vidéo demandée explicitement : annoncée même si elle a déjà été vue.
        try:
            new = [fetch_video(cfg, args.video)]
        except urllib.error.HTTPError as exc:
            log(f"Vidéo {args.video} introuvable sur {cfg.url} (HTTP {exc.code}).",
                file=sys.stderr)
            return 1
        except (OSError, ValueError) as exc:
            return peertube_error(cfg, exc)
    else:
        try:
            videos = fetch_videos(cfg)
        except urllib.error.HTTPError as exc:
            log(f"Compte {cfg.account} introuvable sur {cfg.url} (HTTP {exc.code}).",
                file=sys.stderr)
            return 1
        except (OSError, ValueError) as exc:
            return peertube_error(cfg, exc)
        # Premier lancement : on mémorise l'existant sans rien annoncer, pour
        # ne pas inonder le salon avec des vidéos anciennes.
        if not state_file.exists():
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps([v["uuid"] for v in videos]))
            log(f"Initialisation : {len(videos)} vidéos existantes mémorisées, rien annoncé.")
            return 0

        # Nouvelles vidéos = celles dont l'UUID n'est pas encore dans le fichier
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
    try:
        send_to_muc(cfg, messages)
    except Exception as exc:
        # Le fichier d'état n'est pas mis à jour : ces vidéos seront de
        # nouveau proposées au prochain lancement.
        log(f"Échec de l'envoi XMPP : {describe(exc)}. "
            "Vidéos non marquées comme annoncées.", file=sys.stderr)
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
