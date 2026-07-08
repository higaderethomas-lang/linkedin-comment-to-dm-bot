# LinkedIn Bot — Setup local

Bot qui scanne les commentaires sous un post LinkedIn, classifie les commentateurs avec une IA (Gemini), puis envoie un DM + laisse un commentaire de confirmation.

---

## 1. Pré-requis

- **Python 3.10+** (vérifie avec `python3 --version`)
- **macOS, Linux ou Windows** (testé sur macOS)
- **Compte LinkedIn actif** (idéalement avec du réseau existant, sinon risque de ban)
- **Clé API Gemini** (gratuit jusqu'à un certain quota) : crée-la sur [Google AI Studio](https://aistudio.google.com/apikey)

---

## 2. Installation (5 minutes)

```bash
# 1. Va dans le dossier du projet
cd linkedin-bot

# 2. Crée un environnement virtuel Python (optionnel mais recommandé)
python3 -m venv venv
source venv/bin/activate    # macOS/Linux
# venv\Scripts\activate     # Windows

# 3. Installe les dépendances Python
pip install -r requirements.txt

# 4. Installe le navigateur Playwright (Chromium)
playwright install chromium
```

---

## 3. Configuration

### a) Ta clé Gemini

Ouvre `core/ai_analyzer.py` et remplace la clé existante par la tienne :

```python
GEMINI_API_KEY = "TA_CLE_GEMINI_ICI"
```

### b) Connexion à ton LinkedIn

Lance le serveur :

```bash
python3 server.py
```

Ouvre [http://localhost:8000](http://localhost:8000) dans ton navigateur.

Clique sur le bouton **"Login LinkedIn"** : un Chromium s'ouvre, connecte-toi manuellement à ton compte LinkedIn, fais le 2FA si nécessaire, puis ferme la fenêtre. Tes cookies sont sauvegardés dans `data/linkedin_cookies.json` et ne seront plus demandés.

### c) Crée tes jobs

Depuis l'UI, ajoute un job par post LinkedIn que tu veux automatiser :

- **Post URL** : URL du post (ex: `https://www.linkedin.com/posts/tonpseudo_activity-...`)
- **Doc URL** : URL du document à envoyer en DM
- **Trigger** : mots-clés qui déclenchent l'envoi (ex: `PROPALE,PROPAL`). Laisse vide pour traiter tous les commentaires.
- **DM templates** : variantes du message DM. Utilise `{first_name}` et `{doc_url}` comme placeholders.
- **Confirm templates** : commentaire posté après envoi du DM (`{first_name}` dispo)
- **Connect templates** : commentaire posté pour demander la connexion aux 2nd/3rd degrés

> 💡 **Mets toujours plusieurs variantes** dans chaque template — LinkedIn hash les messages identiques et flag les bots qui spamment toujours le même texte.

### d) Important : remplace MY_SLUG

Dans `core/scanner.py` et `core/bot.py`, remplace `thomas-higadere` par TON propre slug LinkedIn (la partie après `/in/` dans ton URL profil) :

```bash
# Trouve les occurrences
grep -rn "thomas-higadere" core/

# Remplace partout (sur macOS)
sed -i '' 's/thomas-higadere/ton-slug-linkedin/g' core/scanner.py core/bot.py
```

---

## 4. Utilisation

### Lance le bot

Depuis l'UI ([http://localhost:8000](http://localhost:8000)) :

1. **Scanner** : analyse tous les commentaires du post, classifie chaque commentateur via Gemini, stocke en base SQLite
2. **Démarrer** : exécute les actions (DM + commentaires) sur les contacts classifiés `a_traiter` ou `a_connecter`

### Onglet "Base de données"

Affiche tous les contacts avec leur statut :
- `pending_scan` — pas encore analysé
- `a_traiter` — Thomas doit envoyer le DM (degré 1st)
- `a_connecter` — demander la connexion (degré 2nd/3rd)
- `en_attente` — connexion demandée, attend qu'il accepte
- `a_convertir` — il vient d'accepter, envoyer le DM maintenant
- `traite` — DM envoyé + commentaire posté ✅
- `ignore` — commentaire ne contient pas le trigger

Tu peux retraiter un contact manuellement avec le bouton **"Retraiter"**.

---

## 5. Limites anti-ban (déjà configurées)

Le bot respecte ces limites pour minimiser le risque de strike LinkedIn :

| | Limite |
|---|---|
| DMs par jour | **50** |
| Commentaires par jour | **20** (très scrutés par LinkedIn) |
| DMs par semaine | **250** |
| Commentaires par semaine | **80** |
| Délai entre DMs | **5–10 min** randomisé |
| Délai entre commentaires | **2–5 min** randomisé |
| Working hours | **8h–22h** uniquement |
| Pause nocturne | Bot inactif entre 22h et 8h |

À adapter dans `core/bot.py` en haut du fichier si besoin (cf. `MAX_DM_PER_DAY`, `DM_DELAY_MIN`, etc.).

---

## 6. Structure du projet

```
linkedin-bot/
├── server.py              # Serveur FastAPI (UI + API)
├── requirements.txt       # Dépendances Python
├── core/
│   ├── bot.py             # Logique DM + commentaires (humanize, anti-ban)
│   ├── scanner.py         # Scan d'un post → extraction commentateurs
│   ├── executor.py        # Boucle d'exécution (process actions)
│   ├── ai_analyzer.py     # Classification IA Gemini + pré-classification locale
│   └── database.py        # SQLite (contacts, daily_counts, scan_history)
├── static/
│   └── index.html         # UI web
└── data/                  # Créé automatiquement
    ├── contacts.db        # Base SQLite
    ├── linkedin_cookies.json
    └── jobs.json          # Tes jobs configurés via l'UI
```

---

## 7. Troubleshooting

- **`address already in use`** : un autre process tourne sur le port 8000. Tue-le : `lsof -ti :8000 | xargs kill -9`
- **Le scan trouve 0 commentaires** : le slug LinkedIn dans `scanner.py`/`bot.py` n'a pas été changé (cf. étape 3.d)
- **Tous les contacts sont en `a_traiter` mais Thomas a déjà répondu** : ton slug LinkedIn n'est pas le bon → Passe 1 ne reconnaît pas tes réponses comme étant les tiennes
- **`reCAPTCHA détecté`** : le bot fait 3 min de pause automatique. Si ça arrive souvent, augmente les délais dans `core/bot.py`
- **DM tab s'ouvre mais le bot ne tape rien** : LinkedIn a probablement changé son DOM. Regarde les logs `Tentative X` pour voir laquelle a été utilisée.

---

## 8. Bonnes pratiques anti-ban (lis ABSOLUMENT)

1. **N'utilise jamais le bot sur un compte LinkedIn neuf** : risque de ban en 48h. Compte minimum 30 jours d'usage manuel + 500 connexions.
2. **Reste dans les working hours 8h–22h** : LinkedIn détecte les comportements nocturnes.
3. **Ne change pas les délais à la baisse** : 45s minimum entre actions, jamais le même délai deux fois.
4. **Diversifie tes templates** : 4+ variantes par type de message, sinon LinkedIn hash et flag.
5. **Si tu te fais flag** ("unusual activity"), **arrête immédiatement** le bot pendant 48–72h et reprends avec des délais x2.

---

Bon courage, et envoie-moi un message si tu galères 🚀
