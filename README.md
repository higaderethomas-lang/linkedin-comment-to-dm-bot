# 🤖 LinkedIn Comment-to-DM Bot

Bot d'automation LinkedIn pour convertir automatiquement les commentateurs de tes posts en leads via DM.

## 🎯 Ce que ça fait

```
Tu postes sur LinkedIn
        ↓
Quelqu'un commente
        ↓
1ère connexion ?  →  DM direct + commentaire "Bien reçu en DM 👌"
2ème/3ème ?       →  Commentaire public "connecte-toi à moi"
                           ↓ (vérifié toutes les 24h)
                  A accepté ?  →  DM envoyé + commentaire de confirmation
```

## 🛠 Installation (5 minutes)

### 1. Prérequis

- Python 3.10+ installé sur ton Mac
- Homebrew (optionnel mais recommandé)

### 2. Installe les dépendances

```bash
cd linkedin-bot
pip3 install -r requirements.txt
playwright install chromium
```

### 3. Connexion LinkedIn (une seule fois)

```bash
python3 bot.py login
```

→ Un navigateur s'ouvre, entre tes identifiants LinkedIn
→ La session est sauvegardée dans `data/linkedin_cookies.json`
→ Tu n'auras plus jamais à te reconnecter

## 🚀 Utilisation

### Lancer le bot sur un post

```bash
python3 bot.py run \
  --post "https://www.linkedin.com/posts/thomas-xx_XXXX" \
  --doc  "https://drive.google.com/file/d/TON_FICHIER/view"
```

Le bot tourne en arrière-plan et vérifie les commentaires toutes les 30 minutes.

### Voir les statistiques

```bash
python3 bot.py stats
```

### Mode debug (voir le navigateur)

```bash
python3 bot.py run --post "..." --doc "..." --visible
```

### Arrêter le bot

```
Ctrl + C
```

## ⚙️ Personnalisation des messages

Ouvre `core/bot.py` et modifie les templates en haut du fichier :

```python
DM_TEMPLATE_1ST = """Salut {first_name},
...ton message personnalisé...
"""

COMMENT_CONFIRM_TEMPLATE = "Bien reçu en DM {first_name} 👌"

COMMENT_INVITE_TEMPLATE = "Salut {first_name} — connectons-nous d'abord 🤝"

DM_AFTER_CONNECT_TEMPLATE = """Salut {first_name},
...message après acceptation de connexion...
"""
```

## 🛡️ Limites de sécurité (anti-ban)

Configurées dans `core/bot.py` :

| Paramètre | Valeur | Pourquoi |
|-----------|--------|----------|
| MAX_DM_PER_DAY | 50 | Limite LinkedIn safe |
| MAX_COMMENTS_PER_DAY | 30 | Évite le spam détecté |
| MIN_DELAY | 45s | Comportement humain |
| MAX_DELAY | 180s | Comportement humain |
| Heures actives | 8h–22h | Simulation humaine |

## 📁 Structure des fichiers

```
linkedin-bot/
├── bot.py              ← Point d'entrée (lance tout depuis ici)
├── requirements.txt
├── core/
│   └── bot.py          ← Logique principale
├── data/
│   ├── contacts.db     ← Base SQLite (créée automatiquement)
│   └── linkedin_cookies.json  ← Session (créée après login)
└── logs/
    └── bot.log         ← Logs détaillés
```

## ⚠️ Important

- **Ne jamais partager** `data/linkedin_cookies.json` (c'est ta session)
- Le bot respecte les heures humaines (8h–22h)
- Si LinkedIn restreint ton compte, arrête 48h et reprends doucement
- Pour plusieurs posts : relance avec un nouveau `--post` URL

## 🔧 Problèmes courants

**"Session expirée"** → Relance `python3 bot.py login`

**"Bouton Message non trouvé"** → LinkedIn a changé son HTML. Lance avec `--visible` pour débugger

**"Limite atteinte"** → Normal. Le bot attend le lendemain automatiquement
