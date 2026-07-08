"""
LinkedIn Comment-to-DM Bot
Mode streaming : un seul navigateur, un onglet par post, onglet temporaire pour les DMs.
"""

import asyncio
import json
import random
import re
import logging
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from playwright.async_api import async_playwright
from cloakbrowser import launch_async  # CloakBrowser : Chromium stealth + humanize
from core.ai_analyzer import analyze_comment_thread
from core.database import init_db, get_contacts_to_process, get_last_scan_time, has_doc_been_sent_to_slug

BASE_DIR       = Path(__file__).parent.parent
LOG_PATH       = BASE_DIR / "logs" / "bot.log"
COOKIES_PATH   = BASE_DIR / "data" / "linkedin_cookies.json"
EXCLUSION_PATH = BASE_DIR / "data" / "exclusion_list.json"


def _norm_txt(s: str) -> str:
    """Minuscule + sans accents, pour comparer noms/slugs de façon tolérante."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def is_excluded(slug: str = "", name: str = "") -> bool:
    """True si la personne est dans la liste d'exclusion manuelle
    (data/exclusion_list.json). Relue à chaque appel → éditable sans redémarrer.
    Match par slug exact OU par nom (insensible casse/accents)."""
    try:
        data = json.loads(EXCLUSION_PATH.read_text())
    except Exception:
        return False
    slug_n = _norm_txt(slug)
    name_n = _norm_txt(name)
    if slug_n:
        for s in data.get("slugs", []):
            if _norm_txt(s) and _norm_txt(s) == slug_n:
                return True
    if name_n:
        for n in data.get("names", []):
            nn = _norm_txt(n)
            if nn and (nn == name_n or nn in name_n or name_n in nn):
                return True
    return False

# ── Templates par défaut ──────────────────────────────────────────────────────

DM_TEMPLATES = [
    "Bonjour {first_name},\n\nJ'ai vu votre commentaire (merci pour l'intérêt).\n\nVoici le document directement : {doc_url}\n\nThomas",
    "Bonjour {first_name},\n\nComme promis, voici le document 👇\n\n{doc_url}\n\nBelle journée,\nThomas",
    "Bonjour {first_name},\n\nVoici ce que je vous avais promis en commentaire :\n\n{doc_url}\n\nN'hésitez pas si vous avez des questions.\nThomas",
    "Bonjour {first_name},\n\nJe vous envoie le document directement ici :\n\n{doc_url}\n\nÀ bientôt,\nThomas",
]

COMMENT_CONFIRMS = [
    "C'est dans votre boîte {first_name} 📩",
    "Envoyé en DM {first_name} ✅",
    "Je vous ai envoyé ça en DM {first_name} 👍",
]

# Commentaire quand la personne a DÉJÀ reçu le DM (déjà contactée) — formulation
# distincte et honnête : on n'affirme pas un nouvel envoi.
COMMENT_ALREADY = [
    "Bien reçu en DM {first_name} 👍",
    "Déjà envoyé en DM {first_name} ✅",
]

COMMENT_CONNECTS = [
    "Bonjour {first_name}, connectez-vous à moi d'abord pour que je puisse vous envoyer la séquence 🤝",
    "Bonjour {first_name} : envoyez-moi une demande de connexion et je vous fais suivre ça directement 📩",
    "Bonjour {first_name} ! Connectez-vous à moi sur LinkedIn et je vous envoie le doc en DM 🙌",
    "Bonjour {first_name}, je pourrai vous envoyer ça en DM dès qu'on sera connectés 😊",
]


def pick_dm(first_name, doc_url, templates=None):
    pool = [t for t in templates if t.strip()] if templates else DM_TEMPLATES
    return random.choice(pool or DM_TEMPLATES).format(first_name=first_name, doc_url=doc_url)


def pick_confirm(first_name, templates=None):
    pool = [t for t in templates if t.strip()] if templates else COMMENT_CONFIRMS
    return random.choice(pool or COMMENT_CONFIRMS).format(first_name=first_name)


def pick_confirm_already(first_name):
    """Commentaire quand la personne a DÉJÀ reçu le DM (déjà contactée)."""
    return random.choice(COMMENT_ALREADY).format(first_name=first_name)


def pick_connect(first_name, templates=None):
    pool = [t for t in templates if t.strip()] if templates else COMMENT_CONNECTS
    return random.choice(pool or COMMENT_CONNECTS).format(first_name=first_name)


# ── Templates de relance (follow-up envoyé X jours après le DM initial) ───────

RELANCE_TEMPLATES = [
    "Bonjour {first_name},\n\nAvez-vous eu le temps de regarder le document que je vous ai envoyé ?\n\nDites-moi une problématique que vous rencontrez et je vous envoie une ressource gratuite adaptée 🙂\n\nThomas",
    "Bonjour {first_name},\n\nJe me permets un petit suivi : le document vous a-t-il été utile ?\n\nSi vous me partagez un défi que vous rencontrez en ce moment, je vous prépare une ressource gratuite sur le sujet.\n\nThomas",
]


def pick_relance(first_name, doc_url="", templates=None):
    pool = [t for t in templates if t.strip()] if templates else RELANCE_TEMPLATES
    tpl = random.choice(pool or RELANCE_TEMPLATES)
    try:
        return tpl.format(first_name=first_name, doc_url=doc_url)
    except (KeyError, IndexError):
        # Template avec des accolades parasites → on remplace juste {first_name}
        return tpl.replace("{first_name}", first_name).replace("{doc_url}", doc_url)


# ── Quotas et timings ─────────────────────────────────────────────────────────

# ── Limites alignées sur le kit anti-ban LinkedIn ────────────────────────────
# Source : docs/LINKEDIN_ANTI_BAN_PORTABLE_KIT.md (règle 3)
# Commentaires = vecteur primaire de spam pour LinkedIn → bien plus scrutés
MAX_DM_PER_DAY        = 25    # Réduit de 50 → 25 pour ralentir (anti-restriction)
MAX_COMMENTS_PER_DAY  = 15    # Réduit de 20 → 15 (cohérence)
MAX_DM_PER_WEEK       = 150   # Réduit de 250 → 150
MAX_COMMENTS_PER_WEEK = 60    # Réduit de 80 → 60

# Working hours (règle 4) : jamais la nuit
WORK_HOUR_START = 8   # ne démarre pas avant 8h
WORK_HOUR_END   = 22  # arrête après 22h

CHECK_INTERVAL_MINUTES = 15
JOB_COOLDOWN_SECONDS   = 20 * 60

# Délais DM — Augmentés pour paraître + humain et éviter détection bot
DM_DELAY_MIN   = 600      # 10 min entre chaque DM (était 5 min)
DM_DELAY_MAX   = 900      # 15 min max (était 10 min)
DM_PAUSE_EVERY = 2        # Pause + fréquente : toutes les 2 DMs (était 3)
DM_PAUSE_MIN   = 1500     # 25 min (était 15 min)
DM_PAUSE_MAX   = 2700     # 45 min (était 30 min)

# Délais commentaires — aussi augmentés pour cohérence
CONNECT_DELAY_MIN   = 240  # 4 min minimum (était 2 min)
CONNECT_DELAY_MAX   = 420  # 7 min max (était 5 min)
CONNECT_PAUSE_EVERY = 3    # Pause toutes les 3 (était 5)
CONNECT_PAUSE_MIN   = 900  # 15 min (était 10 min)
CONNECT_PAUSE_MAX   = 1800 # 30 min (était 20 min)

# ── Flag d'arrêt ─────────────────────────────────────────────────────────────
# Mis à True quand run_bot démarre, False quand il s'arrête.
# Permet aux fonctions internes de savoir si elles doivent s'arrêter.
_bot_running = False

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_PATH.parent.mkdir(exist_ok=True, parents=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

daily_counts = {"dms": 0, "comments": 0, "reset_day": datetime.now().day}

# ── Humanize typing (règle 5 anti-ban) ────────────────────────────────────────
# Source : docs/LINKEDIN_ANTI_BAN_PORTABLE_KIT.md
KEY_NEIGHBORS_AZERTY = {
    'a': 'zqs', 'z': 'aesq', 'e': 'zrds', 'r': 'etfd', 't': 'rygf', 'y': 'tuhg',
    'u': 'yijh', 'i': 'uokj', 'o': 'iplk', 'p': 'olm',
    'q': 'asw', 's': 'qdazx', 'd': 'sfez', 'f': 'dgrc', 'g': 'fhtv', 'h': 'gjyb',
    'j': 'hkun', 'k': 'jlim', 'l': 'kmop', 'm': 'lnpo',
    'w': 'qxs', 'x': 'wcs', 'c': 'xvfd', 'v': 'cbgf', 'b': 'vnhg', 'n': 'bmj',
}

async def human_type(page, text: str, *, thinking_pause_chance: float = 0.04,
                     thinking_pause_range: tuple = (400, 1200),
                     mistype_chance: float = 0.025):
    """Tape un texte de façon humaine : char-par-char avec délais variables,
    typos occasionnels (corrigés), et pauses "réfléchies" sur ponctuation.
    Utilise page.keyboard (version qui fonctionnait)."""
    for char in text:
        # Typo aléatoire : tape voisin AZERTY → backspace → retape
        lower = char.lower()
        if random.random() < mistype_chance and lower in KEY_NEIGHBORS_AZERTY:
            wrong = random.choice(KEY_NEIGHBORS_AZERTY[lower])
            if char.isupper():
                wrong = wrong.upper()
            await page.keyboard.type(wrong)
            await asyncio.sleep(random.uniform(0.12, 0.26))
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.08, 0.18))

        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(0.04, 0.12))

        # Pause "thinking" sur espace/ponctuation
        if char in " .,\n!?" and random.random() < thinking_pause_chance:
            await asyncio.sleep(random.uniform(*thinking_pause_range) / 1000)


def _reset_daily_counts_if_needed():
    today = datetime.now().day
    if daily_counts["reset_day"] != today:
        daily_counts["dms"] = 0
        daily_counts["comments"] = 0
        daily_counts["reset_day"] = today
        log.info("🔄 Compteurs journaliers remis à zéro")


# ── Session ───────────────────────────────────────────────────────────────────

async def load_session(context):
    if COOKIES_PATH.exists():
        cookies = json.loads(COOKIES_PATH.read_text())
        await context.add_cookies(cookies)
        log.info("🍪 Session chargée")
    else:
        log.warning("⚠️  Pas de cookies — lance: python3 bot.py login")


async def save_session(context):
    cookies = await context.cookies()
    COOKIES_PATH.write_text(json.dumps(cookies, indent=2))


async def login_and_save():
    browser = await launch_async(headless=False, humanize=True)
    try:
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="fr-FR", timezone_id="Europe/Paris"
        )
        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login")
        log.info("👉 Connecte-toi manuellement (jusqu'à 5 min, 2FA/email OK). "
                 "Sauvegarde AUTO dès que tu es connecté — pas besoin de fermer la fenêtre.")

        # Détection robuste : on attend le cookie d'authentification `li_at`
        # (présent dès qu'on est connecté), QUELLE QUE SOIT la page d'arrivée
        # (feed, checkpoint résolu, etc.). Bien plus fiable que wait_for_url('/feed/').
        saved = False
        for _ in range(150):  # 150 × 2s = 5 min
            await asyncio.sleep(2)
            try:
                cookies = await context.cookies()
            except Exception:
                continue
            if any(c.get("name") == "li_at" and c.get("value") for c in cookies):
                await asyncio.sleep(2)  # laisse la session se stabiliser
                await save_session(context)
                log.info("✅ Connecté — session LinkedIn sauvegardée. Tu peux fermer la fenêtre.")
                saved = True
                break
        if not saved:
            log.warning("⏱️ Connexion non détectée après 5 min — relance « Reconnecter LinkedIn ».")
        return saved
    finally:
        try:
            await browser.close()
        except Exception:
            pass


async def _new_browser_context(p, headless):
    """Crée un browser Chromium stealth (CloakBrowser) + contexte avec la session LinkedIn.

    Le paramètre `p` (async_playwright) est conservé pour compatibilité avec
    l'appelant `run_bot()`, mais n'est plus utilisé : CloakBrowser gère
    son propre cycle de vie Playwright via launch_async().
    """
    # humanize=True : clics/frappes/scroll humains automatiquement
    # human_preset="careful" : mouvements lents et prudents (LinkedIn-friendly)
    browser = await launch_async(
        headless=headless,
        humanize=True,
        human_preset="careful",
    )
    context = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="fr-FR", timezone_id="Europe/Paris"
    )
    await load_session(context)
    return browser, context


# ── Vérification : le lien est-il déjà dans la conversation ? ───────────────

def _extract_url(text: str) -> str:
    """Extrait la première URL http(s) d'un message."""
    m = re.search(r'https?://\S+', text or "")
    return m.group(0).rstrip('.,;)') if m else ""


# ── Envoi DM (dans un onglet temporaire) ──────────────────────────────────────

async def send_dm_tab(context, profile_url, message, recipient_name="", skip_dup_check=False,
                      skip_if_replied=False):
    """Ouvre un nouvel onglet, envoie le DM, ferme l'onglet.
    L'onglet du post principal n'est jamais touché.
    skip_dup_check=True : pour les relances (on VEUT écrire à qqn déjà contacté).
    skip_if_replied=True : pour les relances — annule l'envoi si la personne a
    déjà répondu (message entrant dans la conversation)."""
    dm_page = await context.new_page()
    try:
        return await _send_dm(dm_page, profile_url, message, recipient_name,
                              skip_dup_check, skip_if_replied)
    finally:
        try:
            await dm_page.close()
        except Exception:
            pass


async def _send_dm(page, profile_url, message, recipient_name="", skip_dup_check=False,
                   skip_if_replied=False):
    """Logique d'envoi DM sur une page donnée.
    skip_dup_check=True : saute la vérif anti-doublon (relances). Le garde-fou
    destinataire reste actif dans tous les cas."""
    try:
        if not _bot_running:
            return False
        log.info(f"{'='*50}")
        log.info(f"📤 DM → {recipient_name} | {profile_url}")

        slug = profile_url.rstrip("/").split("/in/")[-1].split("?")[0]

        # ── COUCHE 0 anti-doublon : liste d'exclusion manuelle (100% fiable) ──
        # Personnes déjà contactées à la main / clients → jamais de DM, même en relance.
        if is_excluded(slug, recipient_name):
            log.info(f"  🛑 {recipient_name} est dans la liste d'exclusion (slug={slug}) — skip")
            return "excluded"

        # ── COUCHE 1 anti-doublon PAR DOCUMENT : a-t-on déjà envoyé CE MÊME lien
        # à cette personne (sur n'importe quel post) ? On NE bloque QUE si c'est le
        # même doc_url — une personne peut recevoir des docs différents sur des posts
        # différents. (Sauté pour les relances.)
        _dm_doc_url = _extract_url(message)
        if not skip_dup_check and has_doc_been_sent_to_slug(slug, _dm_doc_url):
            log.info(f"  🛑 Ce lien a déjà été envoyé à {recipient_name} (slug={slug}) — skip")
            return "already_sent_db"

        # 1. Navigation vers le profil
        # URL percent-encodée → gère les slugs accentués (« félix-rivierre-… »)
        # qui, bruts, peuvent faire échouer/rediriger le chargement du profil.
        from urllib.parse import quote
        if "/in/" in profile_url:
            _base, _slug = profile_url.split("/in/", 1)
            profile_url_nav = _base + "/in/" + quote(_slug, safe="/-_~.")
        else:
            profile_url_nav = profile_url
        await page.goto(profile_url_nav, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(4)

        current_url = page.url
        if "checkpoint" in current_url or "login" in current_url or "authwall" in current_url:
            log.error(f"🚫 Redirect détecté — CAPTCHA ou session expirée : {current_url}")
            return False

        # ── GARDE-FOU CHARGEMENT PROFIL ───────────────────────────────────────
        # On DOIT être sur une page profil (/in/). Si LinkedIn a redirigé ailleurs
        # (feed, messaging, recherche, 404…), le seul bouton « Message » présent
        # serait celui de la messagerie persistante (= une AUTRE personne) → cause
        # racine des mauvais destinataires observés (Gaspard/Macaire/Joseph). On
        # ABANDONNE alors plutôt que de risquer un envoi à côté.
        if "/in/" not in current_url:
            log.warning(
                f"🛑 Profil NON chargé (URL={current_url}) pour {recipient_name} "
                f"— abandon (réessai au prochain cycle)."
            )
            return False

        # Supprime les overlays de messagerie résiduels + attente fixe (comme la
        # version qui marchait). On NE bloque PLUS sur un wait_for_selector("h1")
        # (il expirait en headless et cassait tout) : le retry de recherche du
        # bouton ci-dessous attend déjà naturellement que la barre d'action rende.
        await page.evaluate("""
            () => {
                ['.msg-overlay-conversation-bubble','.msg-overlay-list-bubble',
                 '[class*="msg-overlay"]','._34a12934'].forEach(sel =>
                    document.querySelectorAll(sel).forEach(el => el.remove())
                );
                window.scrollTo(0, 0);
            }
        """)
        await asyncio.sleep(8)  # LinkedIn a besoin de temps pour se stabiliser

        # Tokens du nom de la cible (sans accents, ≥ 3 lettres) → servent à NE PAS
        # cliquer le bouton « Message X » d'une AUTRE personne (bulle de messagerie
        # persistante, conversation restée ouverte, etc.). Bug réel observé :
        # profil de « Sébastien Gros » mais bouton « Message Gaspard de Monclin »
        # cliqué → DM parti chez Gaspard.
        def _accent_strip(s: str) -> str:
            s = unicodedata.normalize("NFKD", s or "")
            return "".join(c for c in s if not unicodedata.combining(c)).lower()

        _rcpt_tokens = [
            re.sub(r"[^a-z]", "", _accent_strip(t))
            for t in re.split(r"[\s\-]+", recipient_name or "")
        ]
        _rcpt_tokens = [t for t in _rcpt_tokens if len(t) >= 3]

        # 2. Clic sur le bouton "Message" du profil (barre d'action du profil)
        # On EXCLUT la colonne « Autres profils pour vous » (boutons d'autres gens)
        # et la messagerie persistante. Règle : on ne clique JAMAIS un bouton
        # « Message <autre nom> ». On préfère celui qui nomme la cible ; à défaut un
        # bouton « Message » générique (sans nom). On REFUSE un bouton tiers.
        _btn_js = """(tokens) => {
                const strip = s => (s||'').toLowerCase().normalize('NFD')
                                      .replace(/[\\u0300-\\u036f]/g, '');
                const nameFromLabel = lbl => {
                    const l = strip(lbl).trim();
                    return l.startsWith('message ') ? l.slice(8).trim() : '';
                };
                // null = bouton générique (pas de nom) ; true = nomme la cible ;
                // false = nomme une AUTRE personne.
                const matchTarget = el => {
                    const nm = nameFromLabel(el.getAttribute('aria-label') || '')
                            || nameFromLabel(el.textContent || '');
                    if (!nm) return null;
                    return tokens.some(tok => nm.includes(tok));
                };
                // Zones EXCLUES : la messagerie persistante ET la colonne de droite
                // « Autres profils pour vous / People also viewed / PYMK » — qui
                // affiche des boutons « Message <autre personne> » et causait des
                // mauvais destinataires (Yannick, Florian, Julien Borri…).
                const inExcludedZone = el =>
                       el.closest('.msg-overlay-conversation-bubble') ||
                       el.closest('.msg-overlay-list-bubble') ||
                       el.closest('[class*="msg-overlay"]') ||
                       el.closest('[class*="typeahead"]') ||
                       el.closest('.scaffold-layout__aside') ||
                       el.closest('aside') ||
                       el.closest('[class*="browsemap"]') ||
                       el.closest('[class*="pymk"]') ||
                       el.closest('[class*="people-also"]') ||
                       el.closest('[class*="similar"]') ||
                       el.closest('[class*="aside"]');
                const isMsg = el => {
                    // Accepte le bouton dont le TEXTE ou l'aria-label commence par
                    // « message » (LinkedIn rend le bouton principal en texte
                    // « Message <Prénom> », SANS aria-label → il faut lire le texte).
                    const t    = strip(el.textContent || '').trim();
                    const aria = strip(el.getAttribute('aria-label') || '');
                    const looks = (t === 'message' || t.startsWith('message ') ||
                                   aria === 'message' || aria.startsWith('message '));
                    return looks && !inExcludedZone(el) && !el.disabled;
                };
                const all = Array.from(document.querySelectorAll('button, a[href]'))
                    .filter(isMsg)
                    .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

                // 1) bouton qui nomme explicitement la cible
                let btn = all.find(b => matchTarget(b) === true);
                // 2) sinon bouton « Message » générique (sans nom)
                if (!btn) btn = all.find(b => matchTarget(b) === null);
                // 3) on REFUSE un bouton nommant une autre personne
                if (!btn) {
                    const foreign = all.find(b => matchTarget(b) === false);
                    return { label: null,
                             reason: foreign ? 'foreign_only' : 'not_found',
                             foreign: foreign ? (foreign.getAttribute('aria-label')
                                       || foreign.textContent.trim()).slice(0,40) : null };
                }
                btn.scrollIntoView({behavior:'instant', block:'center'});
                btn.click();
                return { label: (btn.getAttribute('aria-label') || btn.textContent.trim()).slice(0,40),
                         reason: 'ok', foreign: null };
            }"""

        # Recherche du bouton avec retry : la barre d'action du profil peut se
        # rendre quelques secondes après le nom (h1). Tant qu'on ne trouve QUE des
        # boutons tiers / rien, on attend et on réessaie (garde-fou toujours actif,
        # donc aucun risque d'envoi à côté pendant les essais).
        click_res = None
        for _btn_attempt in range(5):
            click_res = await page.evaluate(_btn_js, _rcpt_tokens)
            if click_res and click_res.get("label"):
                if _btn_attempt > 0:
                    log.info(f"  ⏳ Bouton Message trouvé à la tentative {_btn_attempt+1}")
                break
            # Pas (encore) de bon bouton : re-nettoie l'overlay réinjecté + attend.
            await page.evaluate("""() => {
                ['.msg-overlay-conversation-bubble','.msg-overlay-list-bubble',
                 '[class*="msg-overlay"]','._34a12934'].forEach(sel =>
                    document.querySelectorAll(sel).forEach(el => el.remove()));
            }""")
            await asyncio.sleep(2)

        clicked = (click_res or {}).get("label")
        reason  = (click_res or {}).get("reason")
        if not clicked:
            if reason == "foreign_only":
                log.warning(
                    f"🛑 GARDE-FOU DESTINATAIRE : sur le profil de «{recipient_name}», "
                    f"seul un bouton « {click_res.get('foreign')} » (autre personne) a été trouvé "
                    f"— abandon (AUCUN message envoyé). Réessai au prochain cycle."
                )
            else:
                log.warning(f"❌ Bouton Message introuvable sur {profile_url}")
            return False
        log.info(f"✅ Bouton Message cliqué : «{clicked}»")

        # Garde-fou redondant (ceinture + bretelles) : si le label du bouton cliqué
        # nomme quelqu'un, ce nom DOIT recouper la cible. Bloque même si la règle JS
        # est contournée par un variant de DOM inattendu.
        _clbl = _accent_strip(clicked)
        if _clbl.startswith("message ") and _rcpt_tokens:
            _named = _clbl[len("message "):].strip()
            if _named and not any(tok in _named for tok in _rcpt_tokens):
                log.warning(
                    f"🛑 GARDE-FOU : bouton «{clicked}» ne correspond pas à la cible "
                    f"«{recipient_name}» — abandon (AUCUN message envoyé)."
                )
                return False

        # Vérifie qu'on n'a pas ouvert un dialog "New message" générique
        # ET détecte si une conversation existe déjà (DM déjà envoyé manuellement)
        await asyncio.sleep(1.5)
        dialog_info = await page.evaluate("""
            () => {
                // Mauvais dialog = search input pour destinataire
                const searchEl = document.querySelector(
                    'input[placeholder*="name"], input[placeholder*="nom"], '
                    + '.msg-connections-typeahead__search-input'
                );
                if (searchEl) return { type: 'wrong_dialog', placeholder: searchEl.placeholder || 'found' };

                // Conversation existante = bulle avec des messages déjà envoyés
                const bubbles = document.querySelectorAll('.msg-overlay-conversation-bubble');
                for (const bubble of bubbles) {
                    const events = bubble.querySelectorAll(
                        '.msg-s-event-listitem, .msg-event-content, ' +
                        '[class*="msg-s-message-list__event"], [class*="msg-s-message-group"]'
                    );
                    if (events.length > 0) return { type: 'existing_conv', count: events.length };
                }
                return null;
            }
        """)
        if dialog_info:
            if dialog_info.get("type") == "wrong_dialog":
                log.warning(f"⚠️ Mauvais dialog 'New message' sans destinataire — fermeture et abandon")
                await page.keyboard.press("Escape")
                return False
            elif dialog_info.get("type") == "existing_conv":
                log.info(f"📩 Conversation déjà existante ({dialog_info.get('count')} msg) pour {recipient_name} — commentaire seulement")
                return "already_in_thread"
        # Note : le dialog "New message" avec destinataire pré-rempli (Stratégie D) est OK —
        # on le détecte via .msg-form__contenteditable dans la boucle de recherche de box.

        # 3. Attend la zone de saisie du message
        # 2 stratégies réelles sur le DOM LinkedIn actuel : bulle flottante (avec
        # historique) ou compositeur .msg-form (sans historique). Les anciennes
        # stratégies iframe interop / frames secondaires ne matchaient plus rien
        # (code mort) et ralentissaient chaque tentative.
        box = None
        matched_mode = None   # 'bubble' (historique visible) | 'compose' (vide)
        bubbles_before = await page.locator(".msg-overlay-conversation-bubble").count()

        for attempt in range(30):
            # Stratégie A : nouvelle bulle flottante
            all_bubbles = page.locator(".msg-overlay-conversation-bubble")
            if await all_bubbles.count() > bubbles_before:
                inner = all_bubbles.last.locator("[contenteditable='true']")
                if await inner.count() > 0:
                    log.info(f"  💬 Tentative {attempt+1} : bulle flottante")
                    box = inner.last
                    matched_mode = 'bubble'
                    break

            # Stratégie B : dialog "New message" / compositeur .msg-form
            # ATTENTION : l'historique n'est PAS affiché dans ce dialog → double-check nécessaire
            for sel in [
                ".msg-form__contenteditable[contenteditable='true']",
                "div.msg-form [contenteditable='true']",
                "div[role='textbox'][contenteditable='true']",
                "div[contenteditable='true'][aria-label*='message' i]",
                "div.msg-form__msg-content-container [contenteditable='true']",
            ]:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        log.info(f"  💬 Tentative {attempt+1} : compose dialog ({sel})")
                        box = loc.last
                        matched_mode = 'compose'
                        break
                except Exception:
                    pass
            if box:
                break

            await asyncio.sleep(0.8)

        if not box:
            # Vérifie si c'est un vrai blocage LinkedIn (reCAPTCHA)
            suspect = [f.url for f in page.frames if "recaptcha" in f.url or "checkpoint" in f.url]
            if suspect:
                log.error(f"🚨 reCAPTCHA détecté : {suspect}")
                log.info("🔄 Pause anti-reCAPTCHA : navigation vers le feed (3 min)...")
                await page.goto("https://www.linkedin.com/feed/", timeout=60000)
                await asyncio.sleep(180)
                log.info("🔄 Reprise après pause reCAPTCHA")
            else:
                log.warning("❌ Zone de saisie DM introuvable après 30 tentatives")
            return False

        # ── RELANCE : la personne a-t-elle déjà RÉPONDU ? ─────────────────────
        # Option A (prudente) : on ne relance que si on est SÛR qu'elle n'a pas
        # répondu. Un message ENTRANT (écrit par elle) → réponse → on annule.
        # Conversation illisible (historique non chargé) → on annule aussi (report).
        if skip_if_replied:
            convo = {"total": 0, "incoming": 0}
            for attempt in range(15):  # jusqu'à ~22s : l'historique peut charger lentement
                # Scroll AGRESSIF des conteneurs de messages vers le haut pour forcer
                # le chargement de l'historique (qui est paresseux dans la bulle).
                await page.evaluate("""() => {
                    const sels = [
                        '.msg-s-message-list-content', '.msg-s-message-list',
                        '.msg-overlay-conversation-bubble .msg-s-message-list',
                        '[class*="message-list"]',
                        '[class*="msg-overlay"] [class*="scrollable"]',
                        '.msg-s-message-list__event-list'
                    ];
                    sels.forEach(s => document.querySelectorAll(s).forEach(el => { el.scrollTop = 0; }));
                }""")
                await asyncio.sleep(1.0)
                convo = await page.evaluate("""() => {
                    const roots = document.querySelectorAll(
                        '.msg-overlay-conversation-bubble, [class*="msg-overlay-conversation"], ' +
                        '.msg-s-message-list, .msg-s-message-list-content, ' +
                        '.msg-s-message-list__event-list'
                    );
                    let total = 0, incoming = 0; const seen = new Set();
                    for (const root of roots) {
                        root.querySelectorAll('[class*="msg-s-event-listitem"]').forEach(it => {
                            if (seen.has(it)) return; seen.add(it);
                            // ne compte que les items qui contiennent un VRAI texte de message
                            const body = it.querySelector(
                                '.msg-s-event-listitem__body, [class*="event-listitem__body"], p');
                            const hasText = body && (body.innerText || '').trim().length > 0;
                            if (!hasText) return;
                            total++;
                            const cls = (it.className || '').toString();
                            // message reçu (d'elle) = modificateur "--other" sur l'item ou un enfant
                            if (cls.includes('--other') || it.querySelector('[class*="--other"]')) incoming++;
                        });
                    }
                    return { total, incoming };
                }""")
                if convo["incoming"] > 0:
                    break
                # historique déjà chargé (on voit des messages) et stable → inutile d'attendre plus
                if convo["total"] > 0 and attempt >= 5:
                    break
            if convo["incoming"] > 0:
                log.info(f"  🙅 {recipient_name} a déjà répondu ({convo['incoming']} msg entrant) — relance ANNULÉE")
                return "replied"
            if convo["total"] == 0:
                log.info(f"  ⏸️  Conversation de {recipient_name} illisible → relance reportée (prudence)")
                return "unreadable"
            log.info(f"  ✓ Pas de réponse de {recipient_name} ({convo['total']} msg, 0 entrant) → relance autorisée")

        # 3b. DOUBLE-CHECK : le lien est-il déjà dans la conversation ?
        # Évite d'envoyer un DM en double si Thomas l'a déjà envoyé manuellement.
        # IMPORTANT : on scope STRICTEMENT à la zone messages de la conversation ouverte.
        # Surtout PAS document.body : la sidebar messaging affiche les aperçus de TOUTES
        # les conversations, et comme le même lien est envoyé à tout le monde, ça
        # déclencherait un faux positif quasi-systématique.
        doc_url = _extract_url(message)
        if doc_url and not skip_dup_check:
            # Décompose l'URL : domaine (charlie-mail.vercel.app) + dernier segment
            # de chemin (ex: "rgpd"). LinkedIn affiche les liens envoyés soit en URL
            # brute, soit en CARTE de prévisualisation (domaine tronqué) → on matche
            # l'URL, le domaine, le href ET le segment de chemin.
            doc_host = ""
            doc_path_seg = ""
            try:
                from urllib.parse import urlparse
                parsed = urlparse(doc_url)
                doc_host = parsed.netloc.lower()
                segs = [s for s in parsed.path.split("/") if s]
                doc_path_seg = segs[-1].lower() if segs else ""
            except Exception:
                pass

            # Cas 1 : la fenêtre montre déjà l'historique (bulle ou panneau).
            # BOUCLE DE RETRY : la conversation peut mettre quelques secondes à
            # charger après l'ouverture du dialog → on re-scrolle + re-vérifie
            # plusieurs fois (sinon faux négatif → DM en double).
            # Laisse l'overlay charger son historique (il s'ouvre sur le compositeur,
            # les anciens messages arrivent ensuite) avant de scruter.
            await asyncio.sleep(2.0)
            visible_has_link = False
            for attempt in range(12):  # ~15s max : l'historique peut être lent
                # Scroll vers le haut pour charger l'historique complet
                await page.evaluate("""
                    () => {
                        document.querySelectorAll(
                            '.msg-overlay-conversation-bubble .msg-s-message-list, ' +
                            '.msg-s-message-list-content, .msg-s-message-list, ' +
                            '[class*="message-list"]'
                        ).forEach(el => { el.scrollTop = 0; });
                    }
                """)
                await asyncio.sleep(0.7)

                visible_has_link = await page.evaluate(f"""
                    () => {{
                        const url  = {json.dumps(doc_url.lower())};
                        const host = {json.dumps(doc_host)};
                        const seg  = {json.dumps(doc_path_seg)};
                        const norm = s => (s || '').toLowerCase();

                        // Racines = TOUTES les zones de conversation ouvertes (bulles +
                        // panneaux), pas seulement la dernière — on ratisse large.
                        const roots = [];
                        document.querySelectorAll(
                            '.msg-overlay-conversation-bubble, ' +
                            '.msg-s-message-list-content, .msg-s-message-list, ' +
                            '.msg-s-message-list__event-list, ' +
                            '[class*="msg-overlay-conversation"], ' +
                            '.scaffold-layout__detail .msg-s-message-list'
                        ).forEach(e => roots.push(e));

                        for (const root of roots) {{
                            if (!root) continue;
                            const txt = norm(root.innerText);
                            // 1) URL brute
                            if (url && txt.includes(url)) return true;
                            // 2) Domaine visible (carte de prévisualisation)
                            if (host && txt.includes(host)) return true;
                            // 3) href des liens (URL complète, domaine, ou segment /rgpd)
                            for (const a of root.querySelectorAll('a[href]')) {{
                                const h = norm(a.getAttribute('href'));
                                if (!h) continue;
                                if (url && h.includes(url)) return true;
                                if (host && h.includes(host)) return true;
                                if (seg && host && h.includes(host) && h.includes('/' + seg)) return true;
                            }}
                        }}
                        return false;
                    }}
                """)
                if visible_has_link:
                    break

            if visible_has_link:
                log.info(f"📩 Lien déjà présent dans la conversation (tentative {attempt+1}) "
                         f"pour {recipient_name} — skip DM")
                return "already_in_thread"

            # Anti-doublon DOM : on s'en tient à la lecture IN-PLACE de la conversation
            # ouverte (boucle visible_has_link ci-dessus). On NE navigue JAMAIS vers
            # /messaging/ depuis l'onglet DM (ça détruisait la box et faisait échouer
            # l'envoi). Les autres garde-fous (liste d'exclusion + dm_sent_at en base)
            # couvrent les cas hors conversation visible. Si le lien n'est pas visible
            # ici et que la base ne connaît pas d'envoi → on envoie.
            log.info("  ✓ Lien non vu dans la conversation ouverte → envoi autorisé")

        # 3c. GARDE-FOU DESTINATAIRE : vérifie que le dialog ouvert est bien adressé
        # à la bonne personne AVANT de taper. Évite d'écrire dans la conversation
        # de quelqu'un d'autre (bug observé après navigation /messaging/ en mode compose
        # où la conversation la plus récente restait ouverte → DM à la mauvaise personne).
        expected_tokens = [t for t in re.split(r"[\s\-]+", (recipient_name or "").lower())
                           if len(t) >= 3]
        if expected_tokens:
            header_name = await page.evaluate("""
                () => {
                    const sels = [
                        '.msg-overlay-bubble-header__title',
                        '.msg-overlay-conversation-bubble__participant-names',
                        '[class*="overlay-bubble-header"] [class*="title"]',
                        '.msg-compose__profile-link',
                        '.msg-connections-typeahead__pill',
                        '.artdeco-pill__text',
                        '.msg-thread__link-to-profile',
                        '.msg-entity-lockup__entity-title'
                    ];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
                    }
                    return '';
                }
            """)
            hl = (header_name or "").lower()
            if header_name and not any(tok in hl for tok in expected_tokens):
                log.warning(
                    f"🛑 GARDE-FOU : dialog adressé à «{header_name}» ≠ cible "
                    f"«{recipient_name}» — abandon (aucun message envoyé)"
                )
                return False
            if header_name:
                log.info(f"  ✓ Destinataire confirmé : «{header_name}»")

        # ── 4a. DOUBLE-CHECK PRÉNOM (AVANT toute frappe) ──────────────────────
        # On vérifie sur le MESSAGE prévu → si mismatch, on abandonne sans avoir
        # tapé un seul caractère (zéro risque de demi-message au mauvais endroit).
        def _norm_name(s):
            s = unicodedata.normalize("NFKD", s or "")
            s = "".join(c for c in s if not unicodedata.combining(c))
            return re.sub(r"[^a-z]", "", s.lower())

        if recipient_name and recipient_name.split():
            expected_first = _norm_name(recipient_name.split()[0])
            m = re.search(r"bonjour\s+([^\s,!.\n]+)", message, flags=re.IGNORECASE)
            greeted = _norm_name(m.group(1)) if m else ""
            if expected_first and greeted and greeted != expected_first:
                log.warning(f"🛑 DOUBLE-CHECK PRÉNOM : «{greeted}» ≠ «{recipient_name}» — ABANDON")
                return False
            if expected_first and expected_first not in _norm_name(message):
                log.warning(f"🛑 DOUBLE-CHECK PRÉNOM : prénom «{expected_first}» absent du message — ABANDON")
                return False
            log.info(f"  ✓ Prénom vérifié (pré-frappe) : «{greeted or expected_first}» ↔ «{recipient_name}»")

        # ── 4b. Frappe du message (lettre par lettre — version qui fonctionnait) ─
        try:
            await box.click(force=True)
        except Exception:
            pass
        try:
            await box.focus()
        except Exception:
            pass
        await asyncio.sleep(0.5)

        await human_type(page, message, thinking_pause_chance=0.04,
                         thinking_pause_range=(400, 1200), mistype_chance=0.025)

        content = await box.evaluate("el => el.textContent || el.innerText || ''")
        if not content.strip():
            log.warning("❌ Texte non capté dans la zone de DM — abandon (réessai au prochain tour)")
            return False
        log.info(f"✏️  Contenu textbox : «{content.strip().replace(chr(10),' ')[:60]}»")

        await asyncio.sleep(1.5)

        # 5. Clic sur Envoyer (bulle flottante OU dialog "New message")
        send_clicked = False
        if not send_clicked:
            sent = await page.evaluate("""
                () => {
                    const isSend = b => {
                        const lbl = (b.getAttribute('aria-label')||'').toLowerCase();
                        const txt = (b.textContent||'').trim().toLowerCase();
                        return (lbl.includes('send') || lbl.includes('envo') ||
                                txt === 'send' || txt === 'envoyer') && !b.disabled;
                    };
                    // Cherche dans la bulle flottante
                    const bubbles = document.querySelectorAll('.msg-overlay-conversation-bubble');
                    const last = bubbles[bubbles.length - 1];
                    if (last) {
                        const btn = Array.from(last.querySelectorAll('button')).find(isSend);
                        if (btn) { btn.click(); return 'bulle:' + (btn.getAttribute('aria-label') || 'Send'); }
                    }
                    // Cherche dans le dialog "New message" (.msg-form)
                    const form = document.querySelector('.msg-form, [class*="compose"]');
                    if (form) {
                        const btn = Array.from(form.querySelectorAll('button')).find(isSend);
                        if (btn) { btn.click(); return 'form:' + (btn.getAttribute('aria-label') || 'Send'); }
                    }
                    // Cherche globalement (dernier recours)
                    const allBtns = Array.from(document.querySelectorAll('button')).filter(isSend);
                    if (allBtns.length > 0) {
                        allBtns[allBtns.length - 1].click();
                        return 'global:' + (allBtns[allBtns.length - 1].getAttribute('aria-label') || 'Send');
                    }
                    return null;
                }
            """)
            if sent:
                log.info(f"✅ Send cliqué ({sent})")
                send_clicked = True
            else:
                log.warning("⚠️ Bouton Send introuvable — fallback Meta+Enter")
                await box.focus()
                await page.keyboard.press("Meta+Enter")

        # ── Préparation des marqueurs de confirmation ─────────────────────────
        msg_url = _extract_url(message)
        msg_host = ""
        if msg_url:
            try:
                from urllib.parse import urlparse
                msg_host = urlparse(msg_url).netloc.lower()
            except Exception:
                pass
        flat_msg = " ".join(message.split())
        # segment distinctif au milieu (évite le "Bonjour {prénom}")
        text_marker = (flat_msg[len(flat_msg) // 3:len(flat_msg) // 3 + 28]
                       if len(flat_msg) > 50 else flat_msg)
        text_marker_norm = "".join(text_marker.split()).lower()

        async def _thread_has_message():
            """Le message envoyé apparaît-il dans le thread ? (page.evaluate = stable,
            ne dépend PAS de l'élément box qui devient périmé après l'envoi)."""
            try:
                return await page.evaluate(f"""
                    () => {{
                        const txtM = {json.dumps(text_marker_norm)};
                        const url  = {json.dumps((msg_url or '').lower())};
                        const host = {json.dumps(msg_host)};
                        const flat = s => (s || '').split(/\\s+/).join('').toLowerCase();
                        const low  = s => (s || '').toLowerCase();
                        const roots = [];
                        const bubbles = document.querySelectorAll('.msg-overlay-conversation-bubble');
                        if (bubbles.length) roots.push(bubbles[bubbles.length - 1]);
                        document.querySelectorAll(
                            '.msg-s-message-list-content, .msg-s-message-list, .msg-s-message-list__event-list'
                        ).forEach(e => roots.push(e));
                        for (const root of roots) {{
                            if (!root) continue;
                            const flatTxt = flat(root.innerText);
                            const lowTxt  = low(root.innerText);
                            if (txtM && flatTxt.includes(txtM)) return true;
                            if (url  && lowTxt.includes(url))   return true;
                            if (host && lowTxt.includes(host))  return true;
                            for (const a of root.querySelectorAll('a[href]')) {{
                                const h = low(a.getAttribute('href'));
                                if ((url && h.includes(url)) || (host && h.includes(host))) return true;
                            }}
                        }}
                        return false;
                    }}
                """)
            except Exception:
                return False

        async def _box_is_empty():
            """État de la box, SANS crasher si l'élément est périmé (détaché = envoyé)."""
            try:
                txt = await box.evaluate("el => el.textContent || el.innerText || ''")
                return not txt.strip()
            except Exception:
                # box détachée du DOM = LinkedIn a re-rendu après envoi → signe d'envoi
                return True

        # 6. CONFIRMATION PRIMAIRE : le message apparaît dans le thread (signal fiable)
        await asyncio.sleep(2)
        confirmed = False
        for _ in range(7):  # ~7s : laisse LinkedIn afficher le message envoyé
            if await _thread_has_message():
                confirmed = True
                break
            await asyncio.sleep(1)

        if confirmed:
            log.info(f"📨 DM envoyé et confirmé (thread) → {recipient_name}")
            daily_counts["dms"] += 1
            return True

        # 7. Pas trouvé dans le thread → on regarde l'état de la box (stale-safe).
        box_empty = await _box_is_empty()
        if not box_empty:
            # Box encore pleine → message vraiment pas parti → retry Meta+Enter
            log.warning("⚠️ Textbox non vidée — retry Meta+Enter")
            try:
                await box.focus()
                await page.keyboard.press("Meta+Enter")
            except Exception:
                pass
            await asyncio.sleep(2)
            # Re-scan thread après le retry
            if await _thread_has_message():
                log.info(f"📨 DM envoyé et confirmé après retry → {recipient_name}")
                daily_counts["dms"] += 1
                return True
            box_empty = await _box_is_empty()

        # Box vidée/détachée + une action d'envoi a eu lieu (bouton Send OU Meta+Enter,
        # les deux envoient réellement d'après les conversations observées) → ENVOYÉ.
        if box_empty:
            log.info(
                f"📨 DM envoyé → {recipient_name} "
                f"(box vidée{', Send cliqué' if send_clicked else ', Meta+Enter'}, thread non re-scanné)"
            )
            daily_counts["dms"] += 1
            return True

        log.error(f"❌ DM non envoyé pour {recipient_name} : textbox toujours pleine, message absent du thread")
        return False

    except asyncio.CancelledError:
        raise  # laisse le CancelledError se propager — ne pas avaler
    except Exception as e:
        log.error(f"Erreur _send_dm: {e}", exc_info=True)
        return False


# ── Réponse inline (sur la page du post, sans navigation) ─────────────────────

async def reply_inline(post_page, slug, first_name, text, full_name=None):
    """Répond à un commentaire directement sur la page du post.
    Ne navigue jamais hors du post — la position de scroll est préservée."""
    search_name = (full_name or first_name).strip()
    # Échappe les apostrophes pour l'injection JS (pas de backslash dans f-string)
    APOS = "\\'"
    safe_name = search_name.replace("'", APOS)
    # Nom de famille (dernier token de longueur >= 2) — sert de double-vérif :
    # l'entité ciblée par slug doit aussi contenir ce nom, sinon on ne poste pas
    # (garde-fou contre un slug désynchronisé / homonymes). On IGNORE les tokens
    # "hash" (id LinkedIn collé : alphanum avec chiffre, len>=5) pour ne pas prendre
    # « 4a396880 » comme nom de famille.
    def _is_hash(t):
        return len(t) >= 5 and t.isalnum() and any(c.isdigit() for c in t)
    _name_tokens = [t for t in re.split(r"[\s\-]+", (full_name or "")) if len(t) >= 2 and not _is_hash(t)]
    last_name = _name_tokens[-1] if len(_name_tokens) >= 2 else ""
    safe_last = _norm_txt(last_name)

    log.info(f"{'='*50}")
    log.info(f"💬 reply_inline → {search_name} (slug={slug})")

    # Supprime les overlays qui pourraient intercepter les clics
    await post_page.evaluate("""
        document.querySelectorAll(
            '._34a12934, .msg-overlay-conversation-bubble, [class*="msg-overlay"]'
        ).forEach(el => el.remove());
    """)
    await asyncio.sleep(0.3)

    # JS : cherche et clique le bouton Reply du commentaire ciblé, ET marque
    # l'entité commentaire cible avec un attribut data-bot-target unique.
    # CRITIQUE : on relie toujours le bouton Reply à SON entité (.comments-comment-entity)
    # pour pouvoir, après le clic, récupérer la box ql-editor À L'INTÉRIEUR de
    # cette entité précise — et non se fier à l'ordre DOM global (all_boxes[-1]).
    # Stratégie 1 : aria-label "Reply to X" / "Répondre à X" → entity = closest()
    # Stratégie 2 : lien /in/slug dans l'entité commentaire → bouton Reply parent
    ENTITY_SEL = ".comments-comment-entity, .comments-post-meta, [data-urn*=\"comment\"]"
    JS_FIND_REPLY = f"""
        () => {{
            const slug = '{slug}';
            const name = '{safe_name}';
            const lastName = '{safe_last}';
            const ENTITY = '{ENTITY_SEL}';
            const _norm = s => (s || '').normalize('NFKD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();

            // Nettoie un éventuel marquage d'un appel précédent
            document.querySelectorAll('[data-bot-target]').forEach(
                e => e.removeAttribute('data-bot-target'));

            // ── On cible l'entité dont l'AUTEUR est ce slug (sa photo de profil
            // pointe vers /in/slug) — PAS une simple mention du nom dans le
            // commentaire de quelqu'un d'autre (ex: Pauline qui tague Franck).
            // Si on ne trouve PAS l'auteur avec certitude → on retourne null et on
            // NE poste RIEN (mieux vaut pas de commentaire qu'un commentaire sous le
            // mauvais thread).
            const entities = Array.from(document.querySelectorAll('.comments-comment-entity'));

            // Le href du DOM est percent-encodé (…gu%C3%A9nault…) alors que le slug
            // est décodé (…guénault…) → on DÉCODE le href avant de comparer.
            const linkSlug = l => {{
                try {{ return decodeURIComponent(l.getAttribute('href') || ''); }}
                catch (e) {{ return l.getAttribute('href') || ''; }}
            }};
            const topLevel = (l, ent) => {{
                let p = l.parentElement;
                while (p && p !== ent) {{
                    if (p.classList && p.classList.contains('comments-comment-entity')) return false;
                    p = p.parentElement;
                }}
                return true;
            }};
            const slugLinks = ent => Array.from(ent.querySelectorAll('a[href*="/in/"]'))
                .filter(l => linkSlug(l).includes('/in/' + slug));

            const isAuthorLink = (l, ent) => {{
                if (!linkSlug(l).includes('/in/' + slug)) return false;
                if (!topLevel(l, ent)) return false;
                // auteur = lien avec photo, OU dans l'en-tête du commentaire
                return !!l.querySelector('img') ||
                       !!l.closest('[class*="commenter"], [class*="actor"], [class*="comment-meta"], [class*="headline"]');
            }};

            let target = null, viaAuthor = false;
            // 1) auteur = lien-photo /in/slug → match AUTORITAIRE (slug unique).
            for (const ent of entities) {{
                if (slugLinks(ent).some(l => isAuthorLink(l, ent))) {{ target = ent; viaAuthor = true; break; }}
            }}
            // 2) repli : 1er lien /in/slug TOP-LEVEL de l'entité (pas imbriqué).
            if (!target) {{
                for (const ent of entities) {{
                    if (slugLinks(ent).some(l => topLevel(l, ent))) {{ target = ent; break; }}
                }}
            }}
            if (!target) return null;

            // ── DOUBLE-VÉRIF NOM DE FAMILLE : UNIQUEMENT pour le repli (2). Si on a
            // trouvé l'AUTEUR par son slug (1), c'est forcément la bonne personne
            // (slug unique) → on ne re-vérifie PAS le nom (sinon un nom reconstruit
            // depuis le slug, ex. "Pierrelemaire Banqueprivée", bloque à tort).
            if (!viaAuthor && lastName && !_norm(target.innerText).includes(lastName)) {{
                return 'WRONG_NAME';
            }}

            const btn = target.querySelector(
                'button[aria-label*="Reply"], button[aria-label*="Répondre"], '
                + '.comments-comment-social-actions__reply-action-button'
            );
            if (!btn) return null;
            target.setAttribute('data-bot-target', '1');
            btn.scrollIntoView({{behavior:'instant', block:'center'}});
            btn.click();
            return 'slug-author';
        }}
    """

    # ── Ferme TOUTES les box résiduelles AVANT de cliquer Reply ─────────────────
    # On repart propre : aucune box d'un autre commentaire ne doit rester ouverte,
    # sinon elle pollue la détection. Boucle d'Escape jusqu'à 0 box (max 5 essais).
    count_before = len(await post_page.query_selector_all(
        "div.ql-editor[contenteditable='true']"))
    log.info(f"📊 {count_before} reply box(es) ouverte(s) avant le clic")
    for _ in range(5):
        if count_before == 0:
            break
        await post_page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        count_before = len(await post_page.query_selector_all(
            "div.ql-editor[contenteditable='true']"))
        log.info(f"📊 Après Escape : {count_before} box(es)")

    # Tente de trouver et cliquer le bouton Reply (5 essais)
    reply_clicked = False
    for attempt in range(5):
        result = await post_page.evaluate(JS_FIND_REPLY)
        if result == "WRONG_NAME":
            # Entité trouvée par slug mais le nom de famille ne correspond pas →
            # on n'écrit PAS (sécurité homonymes / slug désynchronisé).
            log.warning(f"🛑 Nom de famille «{last_name}» absent du commentaire ciblé "
                        f"(slug={slug}) — abandon, aucun commentaire posté")
            return False
        if result:
            log.info(f"✅ Reply cliqué via '{result}'")
            reply_clicked = True
            break
        await asyncio.sleep(0.8)

    if not reply_clicked:
        log.warning(f"❌ Bouton Reply introuvable pour '{search_name}'")
        return False

    # ── Récupère la reply box À L'INTÉRIEUR de l'entité ciblée ────────────────
    # CRITIQUE : on prend la box ql-editor qui appartient à l'entité marquée
    # data-bot-target='1' (le commentaire du bon slug/nom), PAS all_boxes[-1].
    # Cela garantit que la réponse part sous le bon commentaire, indépendamment
    # de l'ordre DOM ou de box résiduelles d'autres commentaires.
    await asyncio.sleep(1.5)
    reply_box = None
    for attempt in range(10):
        reply_box = await post_page.query_selector(
            "[data-bot-target] div.ql-editor[contenteditable='true']")
        if reply_box:
            log.info(f"✅ Reply box trouvée dans l'entité cible (data-bot-target)")
            break
        await asyncio.sleep(0.8)

    if not reply_box:
        # Fallback : entité non marquée (cas aria-label sans entity parent, ou
        # LinkedIn rend la box hors de l'entité). On retombe sur la nouvelle box
        # apparue par rapport à count_before, sinon la dernière.
        all_boxes = await post_page.query_selector_all("div.ql-editor[contenteditable='true']")
        if len(all_boxes) > count_before:
            reply_box = all_boxes[-1]
            log.warning(f"⚠️ Entité non marquée — fallback sur la nouvelle box "
                        f"({len(all_boxes)} total, attendu >{count_before})")
        elif all_boxes:
            reply_box = all_boxes[-1]
            log.warning(f"⚠️ Pas de box dans l'entité ni de nouvelle box — "
                        f"fallback sur la dernière ({len(all_boxes)} boxes)")
        else:
            log.warning(f"❌ Aucune reply box trouvée pour '{search_name}'")
            return False

    # ── Frappe du texte dans CETTE box précise, avec vérif que le texte atterrit ──
    async def _type_into_box():
        await reply_box.scroll_into_view_if_needed()
        await reply_box.click()
        await asyncio.sleep(1.2)  # LinkedIn insère le @mention ici
        # Place le curseur en fin de contenu (après le @mention) puis sélectionne tout
        await reply_box.focus()
        await asyncio.sleep(0.2)
        await post_page.keyboard.press("Meta+a")
        await asyncio.sleep(0.3)
        await human_type(post_page, text, thinking_pause_chance=0.10,
                         thinking_pause_range=(800, 2500), mistype_chance=0.04)

    log.info(f"⌨️  Frappe humanisée ({len(text)} chars) : «{text[:50]}»")
    await _type_into_box()

    # Vérifie que le VRAI texte (pas seulement le @mention) est bien dans la box.
    # On compare un échantillon du texte attendu au contenu réel de reply_box.
    sample = "".join(text[:18].split()).lower()  # sans espaces, robuste aux retours ligne

    def _content_has_sample(content: str) -> bool:
        return bool(sample) and sample in "".join(content.split()).lower()

    content = await reply_box.evaluate("el => el.textContent || ''")
    if not _content_has_sample(content):
        log.warning(f"⚠️ Texte absent de la box (contenu='{content.strip()[:40]}') — nouvelle tentative")
        await _type_into_box()
        content = await reply_box.evaluate("el => el.textContent || ''")
        if not _content_has_sample(content):
            log.warning(f"❌ Texte toujours absent après retry — abandon (pas de submit)")
            await post_page.keyboard.press("Escape")
            return False

    log.info(f"✏️  Contenu zone : «{content.strip()[:60]}»")
    await asyncio.sleep(random.uniform(1, 2))

    # ── Submit : bouton Envoyer de la MÊME box que reply_box ──────────────────
    # CRITIQUE : ne JAMAIS cliquer le premier submit de la page — il peut
    # appartenir à une autre box ouverte (qui ne contient que son @mention).
    # Méthode : on cherche d'abord le submit DANS l'entité cible (data-bot-target),
    # scope le plus sûr. Sinon, parmi tous les submit, on prend celui dont
    # l'ancêtre commun avec reply_box est le plus proche (= la même box).
    submitted = await reply_box.evaluate("""
        el => {
            const SEL = 'button.comments-comment-box__submit-button--cr, ' +
                        'button.comments-comment-box__submit-button, ' +
                        'button[class*="comments-comment-box__submit"], ' +
                        'button[class*="submit-button"]';

            const collect = (root) => {
                let btns = Array.from(root.querySelectorAll(SEL));
                if (btns.length === 0) {
                    btns = Array.from(root.querySelectorAll('button')).filter(b => {
                        const t = (b.textContent || '').trim().toLowerCase();
                        return ['reply','répondre','repondre','comment','commenter',
                                'post','publier','envoyer','send'].includes(t);
                    });
                }
                return btns;
            };

            // 1) Submit scopé à l'entité cible marquée data-bot-target
            const entity = document.querySelector('[data-bot-target]');
            if (entity) {
                const scoped = collect(entity);
                if (scoped.length > 0) {
                    const b = scoped[0];
                    b.scrollIntoView({block:'center'});
                    b.click();
                    return 'ok:entity:' + (b.getAttribute('aria-label') || b.textContent.trim() || 'submit').slice(0,30);
                }
            }

            // 2) Fallback : plus proche ancêtre commun avec reply_box
            const allBtns = collect(document);
            if (allBtns.length === 0) return 'no-buttons';
            let best = null, bestDepth = Infinity;
            for (const btn of allBtns) {
                let depth = 0, node = el;
                while (node) {
                    if (node.contains(btn)) break;
                    node = node.parentElement;
                    depth++;
                }
                if (node && depth < bestDepth) { bestDepth = depth; best = btn; }
            }
            if (!best) return 'no-common';

            best.scrollIntoView({block:'center'});
            best.click();
            return 'ok:' + (best.getAttribute('aria-label') || best.textContent.trim() || 'submit').slice(0,30);
        }
    """)

    if not str(submitted).startswith("ok"):
        log.warning(f"❌ Bouton Envoyer introuvable ({submitted}) — abandon")
        await post_page.keyboard.press("Escape")
        return False

    log.info(f"✅ Send cliqué ({submitted})")
    await asyncio.sleep(1.5)
    log.info(f"✅ Réponse postée pour '{search_name}'")
    daily_counts["comments"] += 1
    # Ferme toute box résiduelle + retire le marquage pour ne pas polluer le prochain appel
    await post_page.keyboard.press("Escape")
    await asyncio.sleep(0.5)
    await post_page.evaluate(
        "document.querySelectorAll('[data-bot-target]')"
        ".forEach(e => e.removeAttribute('data-bot-target'))")
    return True


# ── Boucle principale ─────────────────────────────────────────────────────────

async def run_bot(jobs: list, headless: bool = True, on_result=None):
    """
    Boucle principale du bot (nouvelle architecture SQLite).
    - Scan quotidien par job (scanner.py)
    - Exécution des actions en attente (executor.py)
    - Cycle toutes les 30 minutes.
    """
    # Import ici pour éviter les imports circulaires au niveau module
    from core.scanner import daily_scan
    from core.executor import process_pending_actions

    global _bot_running
    _bot_running = True

    # Initialisation de la base de données
    init_db()
    log.info(f"🚀 Bot démarré | {len(jobs)} job(s) | DB initialisée")

    async with async_playwright() as p:
        browser, context = await _new_browser_context(p, headless)
        log.info("🌐 Browser unique créé")

        # Force un scan complet au premier cycle après chaque démarrage du bot
        first_cycle = True

        try:
            while True:
                _reset_daily_counts_if_needed()

                # Working hours (règle 4 anti-ban) : actif uniquement WORK_HOUR_START → WORK_HOUR_END
                hour = datetime.now().hour
                if hour < WORK_HOUR_START or hour >= WORK_HOUR_END:
                    log.info(f"💤 Hors heures de travail ({hour}h) — bot actif {WORK_HOUR_START}h-{WORK_HOUR_END}h. Pause 30 min.")
                    await asyncio.sleep(1800)
                    continue

                active_jobs = [j for j in jobs if j.get("active", True)]

                if not active_jobs:
                    log.info("😴 Aucun job actif — attente 30 min")
                    await asyncio.sleep(1800)
                    continue

                for job in active_jobs:
                    job_id = job.get("id", job["post_url"])
                    label  = job.get("label", job_id)

                    # ── Scan si premier cycle OU >SCAN_INTERVAL_HOURS depuis le dernier ──
                    # first_cycle : scan forcé à chaque démarrage du bot (capte les
                    #   nouveaux commentaires immédiatement).
                    # Sinon 4h = bon compromis fraîcheur / charge.
                    SCAN_INTERVAL_HOURS = 4
                    last_scan  = get_last_scan_time(job_id)
                    need_scan  = (
                        first_cycle
                        or not last_scan
                        or (datetime.now() - last_scan).total_seconds() > SCAN_INTERVAL_HOURS * 3600
                    )

                    if need_scan:
                        reason = "démarrage" if first_cycle else "périodique"
                        # La réconciliation des 'ignore' qui re-matchent le trigger
                        # est désormais faite À CHAQUE scan dans daily_scan (scanner.py),
                        # tous jobs confondus → plus besoin d'un reset spécial au démarrage.
                        log.info(f"🔍 Scan ({reason}) : {label}")
                        post_page = await context.new_page()
                        try:
                            result = await daily_scan(job, post_page, context, log, on_result)
                            log.info(f"✅ Scan terminé : {result}")
                        except Exception as e:
                            log.error(f"❌ Erreur scan {label} : {e}", exc_info=True)
                        finally:
                            try:
                                await post_page.close()
                            except Exception:
                                pass
                        await asyncio.sleep(30)

                    # ── Traitement des actions en attente ─────────────────────────
                    pending = get_contacts_to_process(job_id)
                    if pending:
                        log.info(f"🔧 {len(pending)} action(s) à traiter : {label}")
                        post_page = await context.new_page()
                        try:
                            result = await process_pending_actions(
                                job, post_page, context, log, on_result
                            )
                            log.info(f"✅ Exécution terminée : {result}")
                        except Exception as e:
                            log.error(f"❌ Erreur exécution {label} : {e}", exc_info=True)
                        finally:
                            try:
                                await post_page.close()
                            except Exception:
                                pass
                    else:
                        log.info(f"😴 Rien à traiter pour {label}")

                    # ── Relances (si activé pour ce job) ──────────────────────────
                    if job.get("relance_enabled"):
                        from core.executor import process_relances
                        try:
                            rres = await process_relances(job, context, log, on_result)
                            log.info(f"✅ Relances terminées : {rres}")
                        except Exception as e:
                            log.error(f"❌ Erreur relances {label} : {e}", exc_info=True)

                # Tous les jobs ont été scannés au moins une fois → repasse en mode périodique
                first_cycle = False

                log.info("⏸️ Prochain cycle dans 30 min")
                await asyncio.sleep(1800)

        except asyncio.CancelledError:
            log.info("⛔ Bot arrêté — fermeture du navigateur...")
        finally:
            _bot_running = False
            try:
                await browser.close()
            except Exception:
                pass
            log.info("✅ Navigateur fermé — bot complètement arrêté")
