# 🤝 Guide de passation — LinkedIn Comment‑to‑DM Bot

Ce document permet à une nouvelle personne de **comprendre, installer et faire tourner** le bot de A à Z. À lire en entier avant de toucher au code.

---

## 1. Ce que fait le bot (en une phrase)

Sur un post LinkedIn, des gens commentent un **mot‑clé déclencheur** (ex. « Newsletter », « PROPALE »). Le bot **scanne** ces commentaires, envoie le **document promis en DM** à chaque personne, puis **répond à son commentaire** (« Envoyé en DM ✅ »). Le tout en imitant un humain (frappe lente, pauses) pour ne pas se faire bannir.

Un **« job »** = un post LinkedIn + son mot‑clé déclencheur + le lien du document à envoyer. On peut avoir plusieurs jobs en parallèle.

---

## 2. Architecture (vue d'ensemble)

```
                    ┌──────────────────────────────────────────┐
   Interface web    │  server.py (FastAPI)  →  http://localhost:8001
   (static/         │   - gère les jobs, lance/arrête le bot
    index.html)     │   - flux de logs en direct (SSE /logs)
                    └───────────────┬──────────────────────────┘
                                    │ lance run_bot()
                    ┌───────────────▼──────────────────────────┐
                    │  core/bot.py  →  run_bot() = boucle 30 min │
                    └───────┬───────────────────┬───────────────┘
                            │ 1) SCAN            │ 2) EXÉCUTION
                ┌───────────▼─────────┐ ┌────────▼──────────────────┐
                │ core/scanner.py     │ │ core/executor.py          │
                │ daily_scan()        │ │ process_pending_actions() │
                │ - charge tous les   │ │ - pour chaque contact     │
                │   commentaires      │ │   "à traiter" : DM puis   │
                │ - extrait les gens  │ │   commentaire de confirm. │
                │ - analyse (IA) →    │ │ - anti-ban (pauses)       │
                │   statut en base    │ └───────────────────────────┘
                └─────────┬───────────┘
                          │ lit/écrit
                ┌─────────▼─────────────────────────────────────┐
                │ core/database.py  →  SQLite (data/contacts.db) │
                │ = SOURCE DE VÉRITÉ : qui en est où             │
                └────────────────────────────────────────────────┘
```

**Point clé** : la base SQLite (`data/contacts.db`) est la **source de vérité**. Chaque commentateur y a un **statut** qui dicte ce que le bot fait de lui. Le scan remplit/met à jour la base ; l'exécuteur agit selon les statuts.

---

## 3. Les statuts d'un contact (le cœur de la logique)

| Statut | Signification | Action du bot |
|---|---|---|
| `pending_scan` | Vient d'être trouvé, pas encore analysé | → analyse IA au scan |
| `a_traiter` | 1ʳᵉ relation + a le trigger, pas encore contacté | **Envoyer le DM** puis commenter |
| `a_connecter` | 2e/3e relation intéressée | Commenter « connectez‑vous d'abord » → `en_attente` |
| `a_convertir` | Était en attente, est devenu 1ʳᵉ relation | **Envoyer le DM** |
| `en_attente` | 2e/3e degré, a reçu le « connectez‑vous » | Attend qu'il accepte la connexion |
| `traite` | DM envoyé **et** commentaire posté | Terminé ✅ |
| `ignore` | Commentaire sans le trigger | Rien |
| `exclu` | Dans la liste d'exclusion manuelle | Jamais contacté |

Seuls `a_traiter`, `a_connecter`, `a_convertir` sont « actionnables » à chaque cycle.

---

## 4. Carte des fichiers

```
linkedin-bot/
├── bot.py                  # Entrée CLI : `python3 bot.py login` (connexion LinkedIn)
├── server.py               # Serveur web FastAPI + API (jobs, start/stop, logs SSE)
├── requirements.txt        # Dépendances Python
├── core/
│   ├── bot.py              # run_bot() + _send_dm() + reply_inline() + frappe humaine
│   ├── scanner.py          # daily_scan() : scanne le post, extrait les commentateurs
│   ├── executor.py         # process_pending_actions() : traite la file (DM + commentaires)
│   ├── database.py         # SQLite : statuts, dédup, helpers
│   ├── ai_analyzer.py      # Analyse IA (Gemini) du commentaire → statut
│   └── whatsapp.py         # (optionnel) notifs/pilotage WhatsApp via OpenWA — DÉSACTIVÉ
├── static/
│   └── index.html          # L'interface web (UI + logs en direct)
├── data/
│   ├── jobs.json           # Les "jobs" (posts + triggers + templates)
│   ├── exclusion_list.json # Personnes à ne JAMAIS contacter
│   ├── whatsapp_config.json# Config WhatsApp (désactivée par défaut)
│   ├── contacts.db         # (généré) la base SQLite — NON inclus dans le zip
│   └── linkedin_cookies.json # (généré au login) session LinkedIn — NON inclus (secret)
├── README.md / SETUP_MATHIS.md / LINKEDIN_BOT_KNOWLEDGE.md / CLOAKBROWSER.md  # docs
└── HANDOFF.md              # ce fichier
```

---

## 5. Installation (≈ 10 min)

```bash
# 1. Dépendances Python
cd linkedin-bot
python3 -m venv venv && source venv/bin/activate     # (optionnel mais recommandé)
pip install -r requirements.txt
pip install cloakbrowser                              # navigateur stealth (cf. §8)

# 2. Lancer le serveur
python3 server.py
# → ouvre http://localhost:8001
```

Au **premier lancement**, le binaire CloakBrowser (~140 Mo) se télécharge automatiquement.

---

## 6. Configuration — LES 3 CHOSES À CHANGER ABSOLUMENT

### a) Ton slug LinkedIn (sinon le bot scanne le mauvais compte)
Le bot doit savoir **qui est « toi »** pour ignorer tes propres réponses. Remplace `thomas-higadere` par **ton** slug (la partie après `/in/` dans l'URL de ton profil) :
```bash
# dans core/scanner.py — variable MY_SLUG
grep -n "MY_SLUG" core/scanner.py
```

### b) La clé API Gemini (analyse IA des commentaires)
Dans `core/ai_analyzer.py`, ligne `GEMINI_API_KEY = "..."` → mets **ta** clé (gratuite sur https://aistudio.google.com/apikey). ⚠️ La clé présente dans le zip est un **placeholder**, à remplacer.

### c) Ta session LinkedIn (cookies)
Dans l'interface, clique **« Reconnecter LinkedIn »** (ou `python3 bot.py login`) : un navigateur s'ouvre, connecte‑toi **manuellement** (+ 2FA), puis ferme la fenêtre. Tes cookies sont sauvés dans `data/linkedin_cookies.json`.

---

## 7. Les "jobs" (data/jobs.json)

Chaque job décrit un post à traiter. Tu les crées/édites **depuis l'interface** (plus simple) ou directement dans le JSON :
```json
{
  "label": "Newsletter",
  "post_url": "https://www.linkedin.com/posts/...",
  "trigger": "newsletter",              // mot-clé à détecter (tolérant aux fautes)
  "doc_url": "https://.../newsletter",  // lien envoyé en DM
  "active": true,                        // le bot ne traite QUE les jobs actifs
  "dm_templates": ["Bonjour {first_name}, ..."],     // [] = templates par défaut
  "confirm_templates": [],               // commentaire après DM
  "connect_templates": [],               // commentaire "connectez-vous"
  "handle_degrees": ["1st","2nd","3rd"]
}
```

---

## 8. CloakBrowser (le navigateur stealth) — important

Le bot n'utilise **pas** Chrome normal mais **CloakBrowser** (`pip install cloakbrowser`) : un Chromium recompilé qui masque l'automation au niveau binaire → indispensable pour ne pas se faire détecter par LinkedIn. Détails dans `CLOAKBROWSER.md`.

- `humanize=True` : frappe/clics/scroll humains automatiques.
- Mode **headless** par défaut (toggle « Afficher le navigateur » décoché) → tourne en arrière‑plan.
- ⚠️ En headless, on ne peut pas résoudre un CAPTCHA à la main → si ça bloque, **coche le toggle** avant de démarrer.

---

## 9. Les garde-fous anti-doublon (à comprendre)

Le bot ne doit JAMAIS envoyer deux fois le même doc à quelqu'un. 4 couches, dans l'ordre :
1. **Liste d'exclusion** (`data/exclusion_list.json`) — par slug ou nom. 100 % fiable. Ajoute‑y toute personne contactée **à la main**.
2. **Base par document** (`dm_doc_url`) — si on a déjà envoyé **ce lien précis** à cette personne (sur n'importe quel post) → skip. Une même personne peut recevoir des docs **différents** sur des posts différents.
3. **Lecture de la conversation** — si le lien est déjà visible dans le DM ouvert → skip.
4. **Double‑vérif prénom + nom de famille** avant de poster un commentaire → jamais sous le mauvais commentaire.

---

## 10. Limites connues / pièges (lis‑les, ça évite des surprises)

- **DM manuel non tracé** : si tu as envoyé un DM **à la main** (hors bot), le bot ne le « voit » pas toujours (LinkedIn n'affiche pas l'historique de façon fiable en automation) → **ajoute la personne à la liste d'exclusion** pour être sûr.
- **2e/3e degré** : on ne peut pas leur envoyer de DM sans être connecté → ils restent en `en_attente` jusqu'à ce qu'ils acceptent.
- **Quotas anti‑ban** (dans `core/bot.py`) : `MAX_DM_PER_DAY=50`, pauses 5‑10 min entre DMs, actif 8h‑22h seulement. Ne les augmente pas brutalement.
- **Détection du degré / des noms** : repose sur le DOM de LinkedIn, qui change parfois → des logs `⚠️ … NON extraites` signalent les commentateurs ratés (rares).
- **WhatsApp** (`core/whatsapp.py`) : intégration optionnelle de notifs/pilotage, **désactivée** (`data/whatsapp_config.json` → `enabled: false`). S'appuie sur OpenWA (projet Node séparé, non inclus). Peu fiable, à ignorer pour démarrer.

---

## 11. Lancer / arrêter au quotidien

```bash
# Démarrer (détaché, survit à la fermeture du terminal) :
cd linkedin-bot && nohup python3 server.py > logs/server.out 2>&1 & disown
# → http://localhost:8001  →  bouton ▶ Démarrer

# Arrêter le serveur :
pkill -f server.py
```
Le port est configurable : `PORT=9000 python3 server.py`.

Dans l'interface : **▶ Démarrer** lance la boucle (scan + traitement toutes les 30 min), **■ Arrêter** la stoppe, et les **logs en direct** montrent l'avancement (`[3/20] …`, pauses avec compte à rebours).

---

## 12. À faire en recevant le projet (checklist)

- [ ] `pip install -r requirements.txt && pip install cloakbrowser`
- [ ] Remplacer `MY_SLUG` dans `core/scanner.py` par ton slug LinkedIn
- [ ] Mettre ta clé Gemini dans `core/ai_analyzer.py`
- [ ] `python3 server.py` → se connecter à LinkedIn (bouton Reconnecter / `bot.py login`)
- [ ] Créer un job de test (1 post, 1 trigger, 1 doc_url) et le passer `active`
- [ ] Démarrer, regarder les logs, vérifier qu'un DM part bien sur un commentaire test

Bonne reprise ! Toute la logique fine (statuts, dédup, anti‑ban) est dans `core/` — commence par `core/executor.py` (la boucle de traitement) puis `core/bot.py` (_send_dm / reply_inline).
