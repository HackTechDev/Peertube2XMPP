# Idées d'amélioration

Classées de la plus utile à la plus accessoire. Les cases cochées sont faites.

## 1. Fiabilité

- [x] **Délai maximal sur l'envoi XMPP.** Si la connexion au salon échouait
  (salon réservé aux membres, pseudo déjà pris, serveur qui ne répond pas), le
  bot ne se déconnectait jamais et le script restait bloqué indéfiniment ; avec
  cron, ces processus s'accumulaient. L'envoi est maintenant limité à 30 s
  (clé `timeout` de la section `[xmpp]`).
- [x] **Erreurs réseau côté PeerTube.** Instance en panne ou injoignable,
  compte inexistant, réponse invalide : le script écrit une ligne d'erreur
  claire dans le log au lieu d'une trace d'erreur Python brute.
- [ ] **Écriture du fichier d'état sûre.** Si le script est interrompu pendant
  l'écriture de `seen.json`, le fichier peut être corrompu et tous les
  lancements suivants plantent. Écrire dans un fichier temporaire, puis le
  renommer.
- [ ] **Empêcher deux lancements simultanés.** Si un lancement est lent, le
  suivant peut démarrer avant sa fin, et la même vidéo est alors annoncée deux
  fois. Un verrou de fichier (`flock`) l'empêche.
- [ ] **Tester un vrai envoi XMPP**, avec le compte du bot.

## 2. Fonctionnalités

- [ ] **Les directs.** Distinguer un direct d'une vidéo, avec un message du type
  `🔴 En direct : {title}`. L'API l'indique avec le champ `isLive`.
- [ ] **Plus de variables dans le message** : `{description}` (un extrait),
  `{tags}`, `{category}`…
- [ ] **Filtrer par chaîne.** Ne surveiller qu'une chaîne précise (ex.
  `hacktechdev`) si le compte en a plusieurs.
- [ ] **Aperçu de la miniature.** Avec XEP-0066, l'extension XMPP qui joint un
  lien vers un fichier, des clients comme Conversations ou Gajim pourraient
  afficher la miniature dans le salon. Le rendu dépend du client.
- [ ] **Plusieurs salons.** Annoncer dans plusieurs salons à la fois, avec une
  liste de salons dans le fichier `.ini`.

## 3. Qualité et exploitation

- [ ] **Tests automatiques** avec pytest :
  - lecture de l'id dans une URL ;
  - `format_message` ;
  - ordre de priorité entre fichier `.ini` et variables d'environnement ;
  - logique premier lancement / nouvelles vidéos, en simulant l'API.
- [ ] **Options `--verbose` et `--version`**, en passant au module `logging`
  comme dans Grav2XMPP.
- [ ] **Timer systemd** à la place de cron, avec un fichier `.service` et un
  fichier `.timer` d'exemple dans le dépôt : logs dans journald, et des
  lancements qui ne peuvent pas se chevaucher.

## 4. Pour aller plus loin

- [ ] **Notification instantanée** avec un plugin PeerTube qui réagit au moment
  de la publication, au lieu de vérifier toutes les 10 minutes. Plus réactif,
  mais il faut être administrateur de l'instance, et c'est plus lourd à
  maintenir.
