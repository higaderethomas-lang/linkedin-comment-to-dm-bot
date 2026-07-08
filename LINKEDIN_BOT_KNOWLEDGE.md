# LinkedIn Bot — Knowledge Base
> Tout ce qu'on a appris en construisant ce bot. À lire avant de coder le prochain.

---

## 1. Architecture générale

- **Playwright** (pas Selenium) — indispensable pour les iframes et le Shadow DOM
- **Chromium headless** avec `--disable-blink-features=AutomationControlled` et `--no-sandbox`
- Toujours injecter : `Object.defineProperty(navigator, 'webdriver', { get: () => undefined })`
- **Pas de base de données** — cookies JSON, jobs JSON, logs fichier
- Un browser par cycle, fermé proprement dans un `finally`

### Système multi-jobs
Le bot gère une liste de jobs, chacun avec : `post_url`, `doc_url`, `trigger`, `label`, `id`.
Les jobs sont persistés dans `data/jobs.json`. À chaque cycle :
1. Les jobs vides depuis moins de 2h sont exclus (`JOB_COOLDOWN_SECONDS = 7200`)
2. Les jobs restants sont mélangés aléatoirement (`random.shuffle`)
3. Chaque job est traité séquentiellement avec 30s de pause entre eux
4. Si un job revient avec des cas à traiter → cooldown annulé automatiquement
5. Si tous les jobs sont en pause → attente 30 min avant de réessayer

### Interface web FastAPI
`server.py` expose :
- `GET/POST/DELETE /jobs` — CRUD des jobs (sauvegardés dans `data/jobs.json`)
- `POST /start` — démarre le bot avec tous les jobs, réinitialise les résultats
- `POST /stop` — annule la tâche asyncio du bot
- `GET /status` — retourne `{"running": bool}`
- `GET /logs` — SSE (Server-Sent Events) temps réel via `QueueHandler` → `log_queue`
- `GET /results` — liste des résultats de la session en cours
- `POST /login` — déclenche `login_and_save()` pour renouveler le cookie

### Callback `on_result`
`run_bot` accepte un paramètre `on_result=None` (callable).
`process_actions` l'appelle après chaque DM ou commentaire :
```python
on_result({"name": full_name, "action": "DM", "status": "ok"|"fail", "ts": "HH:MM"})
```
Le serveur accumule ces résultats dans `action_results` (liste vidée au `/start`).

---

## 2. Authentification — Cookies

### Comment ça marche
LinkedIn utilise un cookie `li_at` comme jeton de session. Durée de vie ~1 an.
On le sauvegarde après login manuel dans `data/linkedin_cookies.json` et on le recharge à chaque cycle.

```python
# Sauvegarder
cookies = await context.cookies()
Path("data/linkedin_cookies.json").write_text(json.dumps(cookies, indent=2))

# Recharger
cookies = json.loads(Path("data/linkedin_cookies.json").read_text())
await context.add_cookies(cookies)
```

### Signes que le cookie est expiré
- Le bot charge la session mais se bloque ensuite sans rien logger
- Redirect vers `/login` ou `/authwall` ou `/checkpoint`
- Toujours vérifier `page.url` après navigation

### Renouveler le cookie
```bash
python3 bot.py login
```
Ouvre Chrome, connexion manuelle, cookie sauvegardé automatiquement dès que LinkedIn redirige vers le feed.

---

## 3. Structure DOM de LinkedIn — Pièges majeurs

### Le bouton "Message" est HORS de `<main>`
`<main>` contient uniquement le feed d'activité (likes, reposts, etc.).
Le header du profil avec les boutons d'action est dans un autre conteneur.
**Ne jamais chercher le bouton Message dans `<main>`.**

### Le bouton Message est un `<a>`, pas un `<button>`
LinkedIn utilise `<a href>` pour le bouton Message sur les profils.
Il faut chercher `button, a[href]` ensemble, pas seulement `button`.

### CSS classes = instables
LinkedIn change ses class names constamment (ex: `.pvs-profile-actions`).
**Ne jamais cibler par class CSS.** Toujours cibler par texte, aria-label, ou data-testid.

### Stratégie robuste pour trouver le bouton Message
Trier tous les `button, a[href]` par position verticale (Y), prendre le premier qui matche "message" en excluant les overlays ouverts :

```javascript
const all = Array.from(document.querySelectorAll('button, a[href]'));
all.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
const btn = all.find(el => {
    const t = el.textContent.trim().toLowerCase();
    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
    return (t === 'message' || aria === 'message') &&
           !el.closest('.msg-overlay-conversation-bubble') &&
           !el.closest('[class*="typeahead"]') &&
           !el.disabled;
});
```

---

## 4. Messagerie LinkedIn — Le vrai fonctionnement

### `document.querySelectorAll()` ne voit PAS la messagerie
LinkedIn embarque toute sa messagerie dans un iframe `data-testid="interop-iframe"`.
`querySelectorAll()` depuis le document parent retourne toujours `[]` pour les éléments de messagerie.
**Confirmé en DevTools : `document.querySelectorAll('[contenteditable]')` → NodeList(0)**

### Il faut `page.frame_locator()` de Playwright
```python
iframe_loc = page.frame_locator('[data-testid="interop-iframe"]')
box = iframe_loc.locator("[contenteditable='true']")
```

### Les deux modes d'ouverture de conversation
Après un clic sur Message, LinkedIn ouvre soit :
- **Bulle flottante** (`.msg-overlay-conversation-bubble`) en bas à droite du profil
- **Iframe interop** (`data-testid="interop-iframe"`) intégré dans la page

Il faut gérer les deux. Tester la bulle en premier car elle est plus fréquente.

### NE PAS supprimer l'interop-iframe dans le cleanup
On a fait cette erreur : supprimer `[data-testid="interop-iframe"]` dans le cleanup cassait toute la messagerie. LinkedIn ne pouvait plus ouvrir de nouvelles conversations. **L'interop-iframe est l'infrastructure globale, pas juste une conversation.**

### Bouton Send introuvable → Meta+Enter
Le bouton Send est dans l'iframe et souvent introuvable via les locators.
Fallback fiable : `page.keyboard.press("Meta+Enter")`.
**Vérification que le message est parti : la textbox se vide après envoi.**

```python
after = await box.evaluate("el => el.textContent || el.innerText || ''")
if not after.strip():
    # Message envoyé ✅
```

---

## 5. Gestion des bulles de conversation — Bug critique

### Le problème du 2ème DM
Après le 1er DM, la bulle de conversation reste dans le DOM (minimisée).
Quand on ouvre le 2ème DM, `bubble_loc.last` peut attraper la mauvaise bulle.

### Solution : compter les bulles AVANT de cliquer Message
```python
bubbles_before = await page.locator(".msg-overlay-conversation-bubble").count()
# ... clic Message ...
# Attendre qu'une NOUVELLE bulle apparaisse
current_count = await all_bubbles.count()
if current_count > bubbles_before:
    new_bubble = all_bubbles.last  # C'est la bonne
```

### Cleanup avant chaque DM (sans supprimer l'interop-iframe)
```javascript
const selectors = [
    '.msg-overlay-conversation-bubble',
    '.msg-overlay-list-bubble',
    '[class*="msg-overlay"]',
    '._34a12934',
    // NE PAS METTRE [data-testid="interop-iframe"] ici
];
```
Suivi d'un `await asyncio.sleep(10)` pour laisser LinkedIn se stabiliser.

### Bulle "recyclée" — LinkedIn réutilise parfois une bulle existante
Si le nombre de bulles ne change pas après le clic, LinkedIn a peut-être réutilisé la dernière.
Vérifier que le prénom du destinataire est dans le texte de la bulle :
```python
bubble_text = await last_bubble.evaluate("el => el.innerText || ''")
if recip_first.lower() in bubble_text.lower():
    # Bonne bulle
```

### Taille de bulle suspecte
Une bulle de 156x60px est une bulle **minimisée**, pas ouverte. Une vraie bulle ouverte fait 400x100px minimum. Loguer les dimensions pour détecter des anomalies.

---

## 6. reCAPTCHA dans l'iframe — Faux positif

### Le bug
La stratégie `textarea` dans l'iframe peut attraper la textarea cachée du reCAPTCHA :
`<textarea name="g-recaptcha-response" id="g-recaptcha-response-100000">`

Le bot essaie d'écrire dedans → textbox vide → abandon.

### Fix
Exclure explicitement les textareas reCAPTCHA :
```python
"textarea:not([name*='recaptcha']):not([id*='recaptcha'])"
```

---

## 7. Extraction des commentateurs — Pièges

### Slugs avec accents URL-encodés
Les URLs LinkedIn encodent les accents : `timoth%C3%A9e-bondaz`.
Sans décodage, le prénom extrait du slug devient `Timoth%c3%a9e`.

```python
from urllib.parse import unquote
slug = unquote(href.split("/in/")[1].split("?")[0].rstrip("/"))
```

### Extraction du prénom depuis le slug vs inner_text
Le `inner_text` d'un lien peut contenir du texte parasite (nom du parent du thread).
La correction via slug ne s'applique **que si le slug contient des tirets**.

- `timothée-bondaz` → slug avec tirets → prénom corrigé via slug : `Timothée` ✅
- `brunobenattar` → slug sans tirets → on garde l'inner_text : `Bruno Benattar` ✅
- `sambagandega` → slug sans tirets → on garde l'inner_text : `Samba Gandega` ✅

```python
if "-" in slug:
    slug_first = slug.split("-")[0].capitalize()
    if slug_first and first_name.lower() != slug_first.lower():
        first_name = slug_first
        full_name = " ".join(p.capitalize() for p in slug.split("-"))
# Sinon : inner_text = source de vérité
```

**Pourquoi c'est important** : si `full_name = "Brunobenattar"` (slug sans tirets mal corrigé),
`reply_to_comment` cherche `aria-label="Reply to Brunobenattar's comment"` qui n'existe pas
sur LinkedIn → 50 tentatives → échec. Le vrai aria-label est `"Reply to Bruno Benattar's comment"`.

### MY_SLUG à filtrer
Toujours exclure ton propre profil (`thomas-higadere`) des commentateurs détectés.
Aussi exclure les slugs qui contiennent `ACoA` (URLs de profils alternatifs LinkedIn).

### Degré de connexion
Détecter dans le `inner_text` : `• 1st`, `• 2nd`, `• 3rd` (anglais) ou `• 1er`, `• 2e`, `• 3e` (français).
Le degré détermine l'action : 1st → DM possible, 2nd/3rd → demander connexion d'abord.

---

## 8. Répondre à un commentaire

### Le bouton Reply a un aria-label précis
```javascript
const btns = Array.from(document.querySelectorAll('button[aria-label*="Reply to"]'));
const btn = btns.find(b => b.getAttribute('aria-label').includes(full_name));
```
Utiliser le **full_name** (prénom + nom) pour être précis. Juste le prénom peut matcher plusieurs personnes.

### LinkedIn injecte un @mention automatique
Après avoir cliqué Reply, LinkedIn pré-remplit la zone avec `@Prénom Nom`.
Il faut l'effacer avant d'écrire :
```python
await page.keyboard.press("Meta+a")
await page.keyboard.press("Backspace")
```

### Zone de réponse = `div.ql-editor[contenteditable='true']`
Quill editor, pas une textarea. Toujours prendre le **dernier** (index -1) car il peut y en avoir plusieurs si d'autres zones de réponse sont ouvertes.

---

## 9. Anti-détection et limites

### Limites à respecter
- 50 DMs/jour max
- 30 commentaires/jour max
- Délais entre actions : 45-180 secondes (aléatoire)
- Pause anti-détection toutes les 3 actions : 5-10 minutes
- Inactif entre 1h et 7h du matin

### Frappe humaine
Ne jamais utiliser `fill()` ou `type()` en bloc. Taper caractère par caractère avec délai aléatoire :
```python
for char in message:
    await page.keyboard.type(char)
    await asyncio.sleep(random.uniform(0.04, 0.12))
```

### User-Agent et viewport
```python
user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
viewport = {"width": 1366, "height": 768}
locale = "fr-FR"
timezone_id = "Europe/Paris"
```

### Détecter un CAPTCHA ou redirect de session
```python
if "checkpoint" in page.url or "login" in page.url or "authwall" in page.url:
    log.error("🚫 CAPTCHA ou session expirée")
    return False
```

### Détecter le bot detection LinkedIn (PerimeterX)
LinkedIn injecte des iframes de vérification quand il suspecte une automatisation :
- `li.protechts.net` → PerimeterX fingerprinting
- `google.com/recaptcha/enterprise` → reCAPTCHA Enterprise actif
- `merchantpool1.linkedin.com` → autre couche de protection

Ces frames apparaissent **dans** la page après le clic Message. Sans détection précoce,
le bot attend 30 × 0.8s = 24 secondes avant de loger "Zone de saisie introuvable".

**Fix — détecter avant la boucle de recherche de textbox** :
```python
BOT_DETECTION_DOMAINS = ["li.protechts.net", "recaptcha/enterprise", "merchantpool1.linkedin.com"]
for frame in page.frames:
    if any(d in frame.url for d in BOT_DETECTION_DOMAINS):
        log.error(f"🚨 Bot detection (PerimeterX/reCAPTCHA) — abandon DM pour {recipient_name}")
        log.error(f"   Frame suspect : {frame.url[:120]}")
        return False
```

Quand ça arrive : renouveler le cookie (`python3 bot.py login`), attendre quelques heures,
réduire la fréquence des actions. C'est LinkedIn qui bloque activement la session.

---

## 10. Logs — Ce qu'il faut loguer

Loguer systématiquement :
- L'URL et le titre de la page après chaque navigation
- Le résultat de chaque clic (élément trouvé, tag, aria-label)
- Le nombre de bulles avant/après ouverture d'une conversation
- La taille (width x height) de la textbox trouvée
- Le contenu de la textbox avant envoi (pour vérifier que le texte a bien été capté)
- Le contenu de la textbox après envoi (0 chars = message envoyé)
- Les frames disponibles quand on ne trouve pas de textbox (aide au diagnostic)

---

## 11. Inexactitudes connues / Dette technique

- **Slug sans tirets** : `slug.split("-")[0].capitalize()` donne un prénom faux pour les slugs collés (`sambagandega` → `Sambagandega`). Pas encore corrigé proprement — nécessite une heuristique plus avancée ou un appel à l'API LinkedIn.
- **Détection de taille de bulle** : Le code loguait les dimensions mais ne les utilisait pas comme critère de sélection. La vraie sécurité vient du comptage `bubbles_before`.
- **Timeout textbox** : 30 tentatives × 0.8s = 24 secondes max pour trouver la zone de saisie. Insuffisant si LinkedIn est lent — augmenter à 40-50 tentatives sur les prochains bots.
- **`unquote()` non appliqué partout** : Dans `reply_to_comment`, le slug reçu est déjà décodé en amont. Vérifier la cohérence si on refactore.

---

## 12. Checklist avant de lancer un nouveau bot LinkedIn

- [ ] `--disable-blink-features=AutomationControlled` dans les args Chromium
- [ ] `navigator.webdriver = undefined` injecté
- [ ] Cookies chargés ET vérifiés (pas de redirect login après navigation)
- [ ] Bouton Message cherché dans TOUT le DOM (pas juste `<main>`)
- [ ] `frame_locator()` utilisé pour la messagerie (jamais `querySelectorAll` seul)
- [ ] Cleanup des bulles sans toucher à `[data-testid="interop-iframe"]`
- [ ] `unquote()` sur les slugs extraits des URLs
- [ ] Frappe caractère par caractère avec délais aléatoires
- [ ] Vérification post-envoi : textbox vidée = message envoyé
- [ ] Limites journalières et plages horaires respectées
- [ ] Logs suffisamment détaillés pour diagnostiquer sans voir le navigateur
- [ ] reCAPTCHA textarea exclue dans la recherche de textbox iframe
- [ ] Système de cooldown par job (2h si vide)
- [ ] Ordre d'exécution des jobs aléatoire à chaque cycle
