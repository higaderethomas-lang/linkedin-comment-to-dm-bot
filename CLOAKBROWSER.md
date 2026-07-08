# CloakBrowser — Guide complet pour mes bots et scrapers

> Documentation de référence pour CloakBrowser dans le contexte de mes projets d'automation (LinkedIn, Lemlist, Charlie growth/prospection, et futurs scrapers).
>
> Dernière mise à jour : 2026-05-17 — version installée : `cloakbrowser` (Chromium 146 sur Linux/Windows, 145 sur macOS)

---

## 1. Qu'est-ce que CloakBrowser ?

CloakBrowser est un **Chromium custom**, recompilé depuis les sources avec ~50 patches C++ qui modifient les empreintes navigateur (fingerprints) au niveau binaire. Il s'intègre comme un **drop-in replacement de Playwright/Puppeteer** : on garde la même API, on change juste l'import.

### Pourquoi ça change tout

| Outil classique | Limite |
|---|---|
| `playwright-stealth` | Patches en JavaScript injectés au runtime → détectables, cassent à chaque update Chrome |
| `undetected-chromedriver` | Modifie des flags Chrome → les anti-bots modernes (Cloudflare, DataDome) le repèrent |
| `puppeteer-extra-stealth` | Idem JS injection, instable |
| **CloakBrowser** | Patches **compilés dans le binaire C++** → invisibles, survivent aux updates Chrome |

### Ce que ça résout concrètement

- `navigator.webdriver` masqué nativement (pas via `add_init_script`)
- `window.chrome` présent comme un vrai Chrome
- `navigator.plugins` peuplé avec une liste réaliste
- Canvas, WebGL, AudioContext, fonts, GPU, screen, WebRTC, timing réseau : tous spoofés au niveau C++
- Pas de leak "HeadlessChrome" dans le User-Agent
- TLS fingerprint (ja3n/ja4/akamai) identique à un vrai Chrome
- Pattern CDP (Chrome DevTools Protocol) modifié pour ne pas trahir l'automation

### Ce que ça ne résout PAS

- Les bans liés au **comportement** (trop de requêtes/min, patterns non humains) → utiliser `humanize=True` + rate limits
- Les bans liés à la **réputation IP** (datacenter IPs) → utiliser un proxy résidentiel
- Les bans liés au **compte** (trop jeune, signalements) → indépendant du browser
- Les CAPTCHAs déjà déclenchés (CloakBrowser les évite, ne les résout pas)

---

## 2. Spécifications techniques

### Plateformes supportées

| Plateforme | Chromium | Patches | Statut |
|---|---|---|---|
| macOS arm64 (Apple Silicon) | 145 | 26 | Stable (utilisé sur mon Mac M2 Pro) |
| macOS x86_64 (Intel) | 145 | 26 | Stable |
| Linux x86_64 | 146 | 57 | Latest |
| Linux arm64 | 146 | 57 | Latest |
| Windows x86_64 | 146 | 57 | Latest |

### Résultats de tests de détection (vs Playwright brut)

| Service de détection | Playwright brut | CloakBrowser |
|---|---|---|
| reCAPTCHA v3 score | 0.1 (bot) | **0.9 (humain)** |
| Cloudflare Turnstile | Échec | **Pass** |
| FingerprintJS | Détecté | **Pass** |
| BrowserScan | Détecté | **Normal (4/4)** |
| bot.sannysoft.com | Plusieurs fails | **All green** (testé sur mon Mac) |
| `navigator.webdriver` | `true` | `false` |
| User-Agent | `HeadlessChrome` | `Chrome/146.0.0.0` |

### API supportées

- **Python** : `cloakbrowser` (sync + async), basé sur Playwright
- **JavaScript/Node** : `cloakbrowser` (Playwright OU Puppeteer)
- **Docker** : `cloakhq/cloakbrowser` (image officielle, ~190 MB RAM idle)
- **CDP server mode** : `cloakserve` pour piloter à distance

### Stockage local

- Binaire téléchargé dans `~/.cloakbrowser/` (~200 MB par version)
- Auto-update activé par défaut (désactivable avec `CLOAKBROWSER_AUTO_UPDATE=false`)
- Vérification SHA-256 automatique

---

## 3. API essentielle (Python)

### `launch()` / `launch_async()` — usage basique

```python
from cloakbrowser import launch  # version sync
from cloakbrowser import launch_async  # version async

# Headless par défaut
browser = launch()

# Avec fenêtre visible
browser = launch(headless=False)

# Avec proxy HTTP
browser = launch(proxy="http://user:pass@proxy:8080")

# Avec proxy SOCKS5 (recommandé si possible — bypass les soucis HTTP CONNECT)
browser = launch(proxy="socks5://user:pass@proxy:1080")

# Avec humanize (clics, frappes, scroll humains)
browser = launch(humanize=True)

# Préréglage prudent (LinkedIn, sites sensibles)
browser = launch(humanize=True, human_preset="careful")

# Auto-detect timezone/locale depuis l'IP du proxy (nécessite pip install cloakbrowser[geoip])
browser = launch(proxy="http://proxy:8080", geoip=True)
```

Retourne un objet `Browser` Playwright standard — tout le reste du code Playwright marche tel quel.

### `launch_persistent_context()` — profil persistant

Crée un vrai profil Chrome avec cookies, localStorage, cache, IndexedDB qui survivent aux redémarrages. **Utile quand un site challenge les sessions vierges**.

```python
from cloakbrowser import launch_persistent_context

ctx = launch_persistent_context(
    "./data/my-profile",  # dossier où le profil est stocké
    headless=False,
    humanize=True,
    proxy="http://proxy:8080",
)
page = ctx.new_page()
page.goto("https://example.com")
ctx.close()  # cookies/cache sauvegardés
```

**Quand utiliser :**
- Sites qui penalisent les sessions incognito (BrowserScan flag `notPrivate`)
- Sites qui demandent une cohérence de fingerprint sur plusieurs sessions
- Quand on veut accumuler un historique de navigation naturel
- Pour charger des extensions Chrome

### `launch_context()` — raccourci browser + context

```python
from cloakbrowser import launch_context

context = launch_context(
    user_agent="Mozilla/5.0 ...",
    viewport={"width": 1920, "height": 1080},
    locale="fr-FR",
    timezone="Europe/Paris",
    storage_state="state.json",  # restaure une session
)
```

### Mode CDP serveur (`cloakserve`)

Lance un browser CloakBrowser en arrière-plan, on s'y connecte via CDP depuis plusieurs scripts.

```bash
# Démarrer le serveur (port 9222)
python -m cloakbrowser cloakserve

# Ou avec Docker
docker run -d --name cloak -p 127.0.0.1:9222:9222 cloakhq/cloakbrowser cloakserve
```

```python
from playwright.sync_api import sync_playwright

pw = sync_playwright().start()
browser = pw.chromium.connect_over_cdp("http://localhost:9222")

# Bonus : fingerprints différents par connexion
b1 = pw.chromium.connect_over_cdp("http://localhost:9222?fingerprint=11111")
b2 = pw.chromium.connect_over_cdp("http://localhost:9222?fingerprint=22222")
```

**Quand utiliser :** plusieurs scripts qui scrapent en parallèle avec des identités différentes.

---

## 4. La feature qui change tout : `humanize=True`

Quand activée, **toutes** les interactions navigateur deviennent humaines automatiquement, sans changer une ligne de code.

| Interaction | Sans humanize | Avec humanize |
|---|---|---|
| Mouvement souris | Téléportation instantanée | Courbe de Bézier avec accélération + léger overshoot |
| Click | Instantané | Aim point réaliste + durée de hold |
| `fill()` / `type()` | Valeur injectée en 0ms | Frappe caractère par caractère avec pauses |
| Scroll | Saut sec | Accélère → cruise → décélère par micro-pas |
| Entre actions | Rien | Micro-mouvements de souris (preset careful) |

### Presets disponibles

```python
# Vitesse normale
browser = launch(humanize=True, human_preset="default")

# Lent et prudent (LinkedIn, banques, sites sensibles)
browser = launch(humanize=True, human_preset="careful")

# Config custom
browser = launch(humanize=True, human_config={
    "mistype_chance": 0.05,              # 5% de typos auto-corrigés
    "typing_delay": 100,                 # 100ms entre chaque touche
    "idle_between_actions": True,        # micro-mouvements entre actions
    "idle_between_duration": [0.3, 0.8], # range de durée idle
})
```

### Coût en performance

- Chaque click : +200-500ms
- Chaque `fill()` : +50ms par caractère
- Chaque scroll : +500ms-2s

**Insignifiant** pour mes bots qui ont déjà des délais 90-180s ou 5-10 min entre actions. **Bloquant** si je fais 1000 clicks/min — mais dans ce cas, je suis déjà détectable par le volume.

---

## 5. Cas d'usage pour MES projets

### Mes projets actuels et futurs

| Projet | Utilise CloakBrowser ? | Pourquoi |
|---|---|---|
| **linkedin-bot** (actuel) | **OUI — déjà migré le 2026-05-17** | LinkedIn fait de l'analyse comportementale + fingerprint. `humanize=True` est critique pour ne pas se faire ban. |
| **lemlist-bot** (actuel) | **NON — pas pertinent** | Connexion CDP à un vrai Chrome local + compte payant Lemlist. Aucun anti-bot agressif. CloakBrowser n'apporterait rien. |
| **charlie-growth / charlie-prospection** (futurs scrapers ?) | **OUI si scraping de sites protégés** | Voir matrice ci-dessous |
| **Scraper Pappers** (si automatisé) | **OUI** | Pappers a un rate-limit + fingerprinting léger |
| **Scraper Société.com / Infogreffe** | **OUI** | Cloudflare actif |
| **Scraping de news financière** | **OUI si Cloudflare/DataDome** | Beaucoup de sites financiers utilisent Akamai |
| **Scraper Google / Bing résultats** | **OUI obligatoire** | Détection bot agressive |
| **APIs publiques (JSON, REST)** | **NON** | Pas besoin de browser, utiliser `httpx` ou `requests` |
| **Scrapers internes (intranet, dashboards)** | **NON** | Pas d'anti-bot |

### Matrice de décision rapide

**Utilise CloakBrowser si :**
- Le site protège ses pages avec Cloudflare, DataDome, Akamai, PerimeterX, ShieldSquare, Imperva
- Le site fait du fingerprinting (FingerprintJS, BrowserScan)
- Le site utilise reCAPTCHA v3 (scoring invisible)
- Le site impose un CAPTCHA Turnstile à la première visite
- Le site analyse le comportement (durée sur page, mouvements souris)
- Tu scrapes à grande échelle et veux éviter les blocs
- Tu fais de l'automation sur des **comptes personnels qu'il ne faut pas perdre** (LinkedIn, Instagram, etc.)

**N'utilise PAS CloakBrowser si :**
- Le site est une API publique JSON → utiliser `httpx` directement
- Le site est un outil SaaS payant où tu es authentifié (Lemlist, Notion, Linear) → ils n'ont pas intérêt à te bloquer
- Le site est un intranet ou un dashboard interne
- Tu fais juste un screenshot rapide d'une page non protégée → `playwright` brut suffit
- Tu as besoin d'extensions Chrome très spécifiques (vérifier compatibilité avant)

### Cas particulier : LinkedIn

LinkedIn ne se base **pas principalement sur le fingerprint navigateur** pour bannir. Il regarde :

1. **Volume d'actions** (DMs/jour, vues de profil, invitations) → mes rate limits règlent ça
2. **Patterns comportementaux** (clics instantanés, frappe à 1000 char/sec) → `humanize=True` règle ça
3. **Réputation du compte** (ancienneté, historique de signalements) → indépendant du tech
4. **Cohérence géographique** (IP fr + locale en-US = suspect) → `geoip=True` + locale fr-FR règle ça

→ Donc pour LinkedIn, `humanize=True` est plus important que le stealth fingerprint. Mais les deux sont activés dans le bot actuel.

### Cas particulier : sites avec Cloudflare Turnstile

Config recommandée pour passer Turnstile :

```python
browser = launch(
    headless=False,                         # Turnstile détecte souvent le headless
    proxy="http://residential-proxy:port",  # IP résidentielle obligatoire
    geoip=True,                             # timezone/locale matchent l'IP
    humanize=True,
    human_preset="careful",
)
```

---

## 6. Installation rapide

```bash
# Python (mon stack principal)
pip3 install cloakbrowser

# Avec auto-detect géo (timezone/locale via IP proxy)
pip3 install cloakbrowser[geoip]

# Pré-télécharger le binaire (sinon téléchargé au premier launch())
python3 -m cloakbrowser install

# Vérifier l'installation
python3 -m cloakbrowser info
```

### Sur macOS, première fois

macOS Gatekeeper bloque le binaire ad-hoc signé. Si erreur "App is damaged" :

```bash
xattr -cr ~/.cloakbrowser/chromium-*/Chromium.app
```

### Test de stealth immédiat

```python
from cloakbrowser import launch

browser = launch(headless=False)
page = browser.new_page()
page.goto("https://bot.sannysoft.com")
input("Vérifier les tests en vert puis appuyer sur Entrée...")
browser.close()
```

---

## 7. Patterns prêts à l'emploi pour mes futurs scrapers

### Pattern A — Scraper "fire & forget" (one-shot)

```python
from cloakbrowser import launch

def scrape_page(url):
    browser = launch(headless=True, humanize=True)
    try:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # ... extraction ...
        return page.content()
    finally:
        browser.close()
```

### Pattern B — Scraper avec proxy résidentiel + géo

```python
from cloakbrowser import launch

browser = launch(
    proxy="http://user:pass@my-residential-proxy:port",
    geoip=True,
    humanize=True,
)
```

### Pattern C — Scraper avec session persistante (sites SaaS authentifiés)

```python
from cloakbrowser import launch_persistent_context

ctx = launch_persistent_context(
    "./profiles/site-x",
    headless=False,  # première fois pour login manuel
    humanize=True,
)
# Login manuel la première fois
# Runs suivants : headless=True, cookies déjà sauvegardés
```

### Pattern D — Scraping parallèle avec identités différentes (CDP server)

```python
# Dans un terminal séparé
# python -m cloakbrowser cloakserve

from playwright.sync_api import sync_playwright

pw = sync_playwright().start()

# Chaque connexion = un fingerprint différent
identities = [11111, 22222, 33333]
for seed in identities:
    browser = pw.chromium.connect_over_cdp(
        f"http://localhost:9222?fingerprint={seed}&geoip=true"
    )
    # ... scraping en parallèle ...
```

### Pattern E — Bot LinkedIn-style (mon cas)

Voir [core/bot.py](core/bot.py) lignes 143-180 :

```python
async def _new_browser_context(p, headless):
    browser = await launch_async(
        headless=headless,
        humanize=True,
        human_preset="careful",
    )
    context = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ...",
        locale="fr-FR",
        timezone_id="Europe/Paris",
    )
    await load_session(context)  # cookies JSON
    return browser, context
```

---

## 8. Troubleshooting

### "App is damaged" sur macOS
```bash
xattr -cr ~/.cloakbrowser/chromium-*/Chromium.app
```

### Le binaire ne se télécharge pas
```bash
# Forcer le téléchargement
python3 -m cloakbrowser install

# Ou pointer vers un binaire local
export CLOAKBROWSER_BINARY_PATH=/path/to/your/chrome
```

### Toujours bloqué malgré CloakBrowser
Dans 90% des cas, c'est l'un de ces 3 problèmes :
1. **Datacenter IP** → utiliser proxy résidentiel
2. **Mode headless détecté** → passer `headless=False` + Xvfb sur Linux
3. **Mismatch timezone/locale/IP** → activer `geoip=True`

### `humanize=True` ralentit trop
Possible sur des scrapers haute fréquence. Solutions :
- Accéder à `page._original` pour bypasser humanize sur un appel spécifique
- Utiliser `human_preset="default"` au lieu de `"careful"`
- Désactiver humanize et gérer les délais manuellement

### Le bot crashe au démarrage (CloakBrowser)
Rollback rapide : commenter les blocs "NOUVEAU CODE" dans `core/bot.py` et décommenter les blocs "ANCIEN CODE" → retour à Playwright brut en 30 secondes.

### Cookies invalides / expirés
```bash
python3 bot.py login
```

---

## 9. Coûts et limites

### Coûts
- **Logiciel : 0€** — open source MIT
- **Binaire : 0€** — gratuit, pas de subscription
- **Pas de service externe** (contrairement à Browserless, ScrapingBee, Bright Data Browser)
- **Mais : proxies résidentiels** restent payants si nécessaires (~5-15€/GB chez les bons providers)

### Limites
- Pas de support officiel — issues GitHub uniquement
- Binaire ~200 MB par version (multiplie si plusieurs machines)
- Auto-update fait un ping pypi.org au démarrage (désactivable)
- Sur macOS : Chromium 145 (vs 146 sur Linux/Win), 26 patches au lieu de 57 → moins complet
- Ne résout PAS les CAPTCHAs visibles (Hcaptcha, image puzzles) — il les évite, ne les résout pas

### Alternative payante pour comparaison
- **Browserless.io** : ~50-200€/mois selon volume — bonne option si je veux 0 maintenance
- **Bright Data Scraping Browser** : ~10€/GB — cher mais ultra-stealth
- **Multilogin / GoLogin / AdsPower** : ~20-100€/mois — gestion de profils multiples (CloakBrowser Manager fait pareil gratuitement)

---

## 10. Décisions et conventions pour mes projets

### Convention de migration

Pour migrer un bot existant vers CloakBrowser :
1. Garder l'ancien code en commentaire juste au-dessus du nouveau (rollback en 30s)
2. Tester d'abord en `headless=False` pour voir ce qui se passe
3. Activer `humanize=True` par défaut sur tous mes bots
4. `human_preset="careful"` pour LinkedIn et sites sensibles
5. Mettre à jour `MEMORY.md` (Claude) pour tracer le passage à CloakBrowser

### Variables d'environnement utiles (à mettre dans `~/.zshrc`)

```bash
# Désactiver le ping pypi au démarrage
export CLOAKBROWSER_AUTO_UPDATE=false

# Customiser le cache (utile si disque principal saturé)
export CLOAKBROWSER_CACHE_DIR=/Volumes/External/cloakbrowser
```

### Fichier `linkedin-bot/.env` (recommandé, à créer)
```
CLOAKBROWSER_AUTO_UPDATE=false
ANTHROPIC_API_KEY=sk-ant-...
```

---

## 11. Ressources

- **GitHub** : https://github.com/CloakHQ/cloakbrowser
- **PyPI** : https://pypi.org/project/cloakbrowser
- **Site officiel** : https://cloakbrowser.dev
- **Docker Hub** : `cloakhq/cloakbrowser`
- **Tests de détection à monitorer** :
  - https://bot.sannysoft.com (basique)
  - https://browserscan.net (moderne)
  - https://demo.fingerprint.com (FingerprintJS officiel)
  - https://bot.incolumitas.com (détection avancée)

---

## TL;DR — Quand utiliser CloakBrowser ?

> **Règle simple :** dès qu'un site m'a bloqué une fois, ou dès que j'automatise un compte personnel que je ne veux pas perdre, j'utilise CloakBrowser avec `humanize=True`. Pour tout le reste (APIs, dashboards internes, scrapers one-shot sans protection), Playwright brut ou `httpx` suffisent.
