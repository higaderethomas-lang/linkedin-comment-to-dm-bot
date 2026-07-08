"""
Scan quotidien des commentaires LinkedIn.
Une seule passe complète du haut vers le bas, sans rescroller.
"""

import asyncio
import json
import logging
import random
import re
from difflib import SequenceMatcher
from datetime import datetime
from urllib.parse import unquote
from typing import Optional, Callable

from core.database import (
    upsert_contact,
    update_status,
    update_contact_identity,
    get_pending_connection_checks,
    record_scan,
    init_db,
)
from core.ai_analyzer import analyze_comment_thread

MY_SLUG = "thomas-higadere"

# ── Rattrapage profil (double vérification) ──────────────────────────────────
# Quand la lecture du commentaire échoue (nom reconstruit depuis le slug, ou
# degré indéterminé), on ouvre le PROFIL dans un onglet temporaire de la MÊME
# session CloakBrowser pour lire le vrai nom + degré. Borné + espacé → discret.
MAX_PROFILE_RECOVERIES = 15   # plafond par scan (protège contre la surcharge)
RECOVER_DELAY_MIN      = 4    # secondes entre deux ouvertures de profil
RECOVER_DELAY_MAX      = 9

log = logging.getLogger(__name__)


def _looks_valid_name(name: str) -> bool:
    """Un nom est exploitable s'il contient au moins une lettre (sinon = masqué
    type « Membre LinkedIn » ou vide)."""
    return bool(name) and any(c.isalpha() for c in name)

# Titres à ignorer en tête de nom (ex: "Dr. François ..." → prénom = "François").
_NAME_TITLES = {
    "dr", "dre", "drs", "m", "mr", "mme", "mrs", "ms", "mlle", "me",
    "pr", "prof", "mgr", "sir", "mx", "maitre", "maître",
}


def _is_hash_token(tok: str) -> bool:
    """Vrai si le token est un identifiant LinkedIn collé au slug (ex: '4a396880',
    '23914315b', '7a5b601') : alphanumérique, contient au moins un chiffre,
    longueur >= 5. Sert à le retirer des noms reconstruits depuis le slug."""
    t = tok.strip()
    return len(t) >= 5 and t.isalnum() and any(c.isdigit() for c in t)


def _clean_first_name(full_name: str) -> str:
    """Premier mot du nom qui n'est PAS un titre (Dr., M., Mme, Me, Pr., Maître…).
    Conserve les prénoms composés ("Pierre-Alban") car le trait d'union ne sépare
    pas sur un espace. Si tout est titre, renvoie le 1er mot tel quel."""
    tokens = full_name.split()
    for tok in tokens:
        norm = tok.strip(".,").lower()
        if norm and norm not in _NAME_TITLES:
            return tok
    return tokens[0] if tokens else ""


def trigger_matches(text: str, triggers: list, threshold: float = 0.82) -> bool:
    """
    Vrai si l'un des triggers apparaît dans `text`, AVEC tolérance aux fautes
    de frappe (ex. « Infrastrusture » matche le trigger « INFRASTRUCTURE »).

    - Pas de trigger configuré → toujours vrai (aucun filtre).
    - 1) Match exact en sous-chaîne (comportement historique, rapide).
    - 2) Sinon match flou mot-à-mot via SequenceMatcher, uniquement pour les
         triggers d'au moins 5 lettres (les triggers courts restent en exact
         pour éviter les faux positifs). On ne compare que des mots de longueur
         proche (±3) pour rester précis.

    `triggers` est attendu en MAJUSCULES (comme construit dans daily_scan).
    """
    if not triggers:
        return True
    up = (text or "").upper()

    # 1) Sous-chaîne exacte
    for t in triggers:
        if t and t in up:
            return True

    # 2) Flou mot-à-mot
    words = re.findall(r"[A-ZÀ-Ÿ]+", up)
    for t in triggers:
        if len(t) < 5:
            continue  # triggers courts → exact uniquement
        for w in words:
            if abs(len(w) - len(t)) > 3:
                continue
            if SequenceMatcher(None, w, t).ratio() >= threshold:
                return True
    return False


async def daily_scan(
    job: dict,
    page,
    context,
    logger=None,
    on_result: Optional[Callable] = None,
) -> dict:
    """
    Scan complet du post LinkedIn en une seule passe.

    1. Navigue vers job['post_url']
    2. Scrolle de haut en bas pour charger tous les commentaires
    3. Extrait chaque commentateur → upsert_contact()
    4. Analyse IA sur les contacts 'pending_scan'
    5. Vérifie les contacts 'en_attente' dont le degré est passé à '1st'
    6. Enregistre dans scan_history
    """
    if logger is None:
        logger = log

    job_id    = job.get("id", job["post_url"])
    post_url  = job["post_url"]
    triggers  = [t.strip().upper() for t in job.get("trigger", "").split(",") if t.strip()]

    logger.info(f"🌐 Scan : navigation vers {post_url}")
    nav_ok = False
    for attempt in range(3):
        try:
            await page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
            nav_ok = True
            break
        except Exception as e:
            wait = (attempt + 1) * 15
            logger.warning(f"⚠️  Navigation scan échouée (essai {attempt+1}/3) : {e}. Retry dans {wait}s…")
            await asyncio.sleep(wait)
    if not nav_ok:
        logger.error(f"❌ Impossible de charger le post pour le scan après 3 essais")
        return {"total_found": 0, "new_contacts": 0, "to_process": 0, "converted": 0}
    await asyncio.sleep(5)

    # ── Scroll complet : charge tous les commentaires ─────────────────────────
    await _full_scroll_load(page, logger)

    # ── Extraction de tous les commentaires visibles ──────────────────────────
    entities = await page.query_selector_all(".comments-comment-entity")
    logger.info(f"📋 {len(entities)} entités commentaires trouvées")

    # ── PASSE 1 : construire la map des réponses de Thomas ────────────────────
    # LinkedIn utilise une structure PLATE (siblings, pas parent/enfant).
    # Approche SÉQUENTIELLE : on garde en mémoire le dernier commentateur vu,
    # et on associe TOUTES les entités de Thomas à ce dernier commentateur.
    # Cela capture les réponses sans @mention (ex: "Bien reçu en DM Baptiste 👌").
    logger.info("🔍 Passe 1 : extraction des réponses de Thomas (approche séquentielle)...")
    thomas_replies: dict = {}   # profile_slug → texte(s) des réponses de Thomas
    last_slug: Optional[str] = None

    for entity in entities:
        try:
            entity_info = await entity.evaluate(f"""el => {{
                const MY_SLUG = {json.dumps(MY_SLUG)};
                const allLinks = Array.from(el.querySelectorAll('a[href*="/in/"]'));

                // Trouve le lien photo top-level (auteur de l'entité)
                let photoLink = null;
                for (const l of allLinks) {{
                    let p = l.parentElement, nested = false;
                    while (p && p !== el) {{
                        if (p.classList.contains('comments-comment-entity')) {{ nested = true; break; }}
                        p = p.parentElement;
                    }}
                    if (!nested && l.querySelector('img')) {{ photoLink = l; break; }}
                }}

                if (!photoLink) return null;

                const isThomas = photoLink.href.includes(MY_SLUG);
                if (isThomas) {{
                    // Extrait uniquement le texte du commentaire (pas le nom, les boutons, etc.)
                    const contentEl = el.querySelector(
                        '.comments-comment-entity__inline-show-more-text, ' +
                        '.feed-shared-inline-show-more-text, ' +
                        '[class*="comment-content"], ' +
                        '[class*="comment-text"]'
                    );
                    let text = '';
                    if (contentEl) {{
                        text = contentEl.innerText.trim();
                    }} else {{
                        // Fallback : clone sans sous-entités ni metadata
                        const clone = el.cloneNode(true);
                        clone.querySelectorAll('.comments-comment-entity').forEach(s => s.remove());
                        clone.querySelectorAll('[class*="commenter-header"],[class*="actor"],[class*="meta"],[class*="social-actions"],[class*="reaction"]').forEach(s => s.remove());
                        text = clone.innerText.trim();
                    }}
                    return {{ isThomas: true, text, slug: null }};
                }}

                // Entité commentateur → extraire le slug depuis le lien photo
                const m = photoLink.href.match(/\\/in\\/([^/?#]+)/);
                const slug = m ? decodeURIComponent(m[1]).replace(/\\/$/, '') : null;
                return {{ isThomas: false, text: null, slug }};
            }}""")

            if not entity_info:
                continue

            if entity_info["isThomas"]:
                # Associe cette réponse de Thomas au dernier commentateur vu
                if last_slug:
                    text = entity_info["text"] or ""
                    if last_slug in thomas_replies:
                        thomas_replies[last_slug] += "\n---\n" + text
                    else:
                        thomas_replies[last_slug] = text
            else:
                slug = entity_info.get("slug")
                if slug and "ACoA" not in slug:
                    last_slug = slug

        except Exception:
            continue

    logger.info(f"   → {len(thomas_replies)} slugs avec réponse de Thomas trouvés")
    # Debug : affiche 3 exemples pour vérifier la qualité du texte extrait
    for i, (slug, text) in enumerate(list(thomas_replies.items())[:3]):
        preview = text[:80].replace('\n', ' ')
        logger.info(f"   🔎 ex.{i+1}: [{slug}] → {preview}…")

    # ── PASSE 2 : extraction des commentateurs ────────────────────────────────
    total_found  = 0
    new_contacts = 0
    skipped: dict = {}        # raison → compteur (hors réponses de Thomas)
    skipped_samples: list = []  # aperçus des entités perdues (pour diagnostic)
    recover_list: list = []   # contacts dont la lecture a échoué → rattrapage profil

    for entity in entities:
        try:
            result = await _extract_contact(entity, job_id, logger, thomas_replies)
            if isinstance(result, str) or result is None:
                # Skip : 'thomas' = légitime ; le reste = entité PERDUE → visible
                reason = result or "inconnu"
                if reason != "thomas":
                    skipped[reason] = skipped.get(reason, 0) + 1
                    if len(skipped_samples) < 5:
                        try:
                            preview = (await entity.inner_text())[:70].replace("\n", " ")
                        except Exception:
                            preview = "?"
                        skipped_samples.append(f"[{reason}] «{preview}»")
                continue

            profile_slug = result["profile_slug"]
            full_name    = result["full_name"]
            first_name   = result["first_name"]
            profile_url  = result["profile_url"]
            degree       = result["degree"]
            comment_text = result["comment_text"]
            thread_text  = result.get("thread_text", comment_text)

            total_found += 1
            is_new = upsert_contact(
                job_id=job_id,
                profile_slug=profile_slug,
                full_name=full_name,
                first_name=first_name,
                profile_url=profile_url,
                degree=degree,
                comment_text=comment_text,
                thread_text=thread_text,
            )
            if is_new:
                new_contacts += 1

            # Lecture partielle (nom reconstruit ou degré absent) → à re-vérifier
            # via le profil après l'extraction.
            if result.get("needs_recovery"):
                recover_list.append({
                    "contact_id":  f"{job_id}::{profile_slug}",
                    "profile_url": profile_url,
                    "full_name":   full_name,
                    "priority":    result.get("recover_priority", 1),
                })

        except Exception as e:
            logger.debug(f"Erreur extraction entité: {e}")
            continue

    logger.info(f"✅ Extraction : {total_found} trouvés, {new_contacts} nouveaux")
    if skipped:
        detail = ", ".join(f"{k}={v}" for k, v in skipped.items())
        logger.warning(f"⚠️  {sum(skipped.values())} entité(s) NON extraites ({detail})")
        for s in skipped_samples:
            logger.warning(f"     ↳ {s}")

    # ── PASSE 2.5 : RATTRAPAGE PROFIL (double vérification) ───────────────────
    # Pour les contacts dont la lecture du commentaire a échoué partiellement, on
    # ouvre leur profil (même session CloakBrowser) pour récupérer nom + degré.
    # Priorité aux noms reconstruits (priority 0), puis aux degrés manquants.
    # Borné à MAX_PROFILE_RECOVERIES et espacé pour rester discret.
    if recover_list:
        recover_list.sort(key=lambda r: r["priority"])
        todo = recover_list[:MAX_PROFILE_RECOVERIES]
        skipped_recover = len(recover_list) - len(todo)
        logger.info(
            f"🔁 Rattrapage profil : {len(todo)} contact(s) à re-vérifier"
            + (f" ({skipped_recover} au-delà du plafond, reportés au prochain scan)"
               if skipped_recover else "")
        )
        recovered = 0
        for r in todo:
            import core.bot as _bot_module
            if not _bot_module._bot_running:
                logger.info("⛔ Bot arrêté — arrêt du rattrapage profil")
                break

            data = await _recover_from_profile(context, r["profile_url"], logger)
            if data:
                new_name = data.get("full_name") or ""
                new_first = data.get("first_name") or ""
                new_deg  = data.get("degree")
                upd = {}
                if _looks_valid_name(new_name) and new_name != r["full_name"]:
                    upd["full_name"]  = new_name
                    upd["first_name"] = new_first or None
                if new_deg:
                    upd["degree"] = new_deg
                if upd:
                    update_contact_identity(r["contact_id"], **upd)
                    recovered += 1
                    changes = []
                    if "full_name" in upd:
                        changes.append(f"nom «{r['full_name']}» → «{new_name}»")
                    if "degree" in upd:
                        changes.append(f"degré → {new_deg}")
                    logger.info(f"   ✅ {' | '.join(changes)}")

            # Pause discrète entre deux profils
            await asyncio.sleep(random.uniform(RECOVER_DELAY_MIN, RECOVER_DELAY_MAX))

        logger.info(f"🔁 Rattrapage terminé : {recovered}/{len(todo)} contact(s) corrigé(s)")

    # ── Phase 2 : analyse des contacts encore en 'pending_scan' ──────────────
    from core.database import _conn  # import local pour éviter les circulaires

    # ── RÉCONCILIATION (scan auto-correcteur) ─────────────────────────────────
    # Récupère les contacts coincés en 'ignore' qui matchent MAINTENANT le trigger
    # (typiquement l'autocorrection « Propale » → « Propane » que l'ancien match
    # exact ratait). On ne ré-ouvre QUE ceux qui re-matchent → coût minimal et
    # idempotent (une fois ré-analysés ils ne sont plus 'ignore'). Les 'en_attente'
    # devenus 1er degré sont gérés séparément en Phase 3.
    reactivated = 0
    if triggers:
        with _conn() as con:
            ignored = con.execute(
                "SELECT id, comment_text FROM contacts WHERE job_id=? AND status='ignore'",
                (job_id,),
            ).fetchall()
        for row in ignored:
            if trigger_matches(row["comment_text"] or "", triggers):
                update_status(row["id"], "pending_scan")
                reactivated += 1
        if reactivated:
            logger.info(f"♻️  Réconciliation : {reactivated} contact(s) 'ignore' re-matchent le trigger → ré-analyse")

    with _conn() as con:
        pending_rows = con.execute(
            "SELECT * FROM contacts WHERE job_id=? AND status='pending_scan'",
            (job_id,),
        ).fetchall()
    pending_contacts = [dict(r) for r in pending_rows]

    logger.info(f"🤖 Analyse IA sur {len(pending_contacts)} contacts pending_scan")

    # Compteurs d'entonnoir : où vont les contacts analysés ce scan
    funnel = {"trigger_absent": 0, "a_traiter": 0, "a_connecter": 0,
              "a_convertir": 0, "en_attente": 0, "traite": 0, "ignore": 0,
              "ia_erreur": 0}

    for contact in pending_contacts:
        contact_id   = contact["id"]
        full_name    = contact["full_name"]
        first_name   = contact["first_name"]
        degree       = contact["degree"] or "2nd"
        comment_text = contact["comment_text"] or ""
        # thread_text inclut la réponse de Thomas si elle existe
        thread_text  = contact.get("thread_text") or comment_text

        # Vérification trigger (flou : tolère les fautes de frappe) sur le
        # commentaire brut, pas sur la réponse de Thomas.
        if not trigger_matches(comment_text, triggers):
            preview = (comment_text or "").replace("\n", " ")[:50]
            logger.info(f"  ⏭️  {full_name} — trigger {triggers} absent → ignore | «{preview}»")
            update_status(contact_id, "ignore")
            funnel["trigger_absent"] += 1
            continue

        # Analyse IA sur le thread complet (commentaire + réponse de Thomas si existante)
        try:
            result = await analyze_comment_thread(full_name, thread_text, degree)
            action    = result.get("action", "nothing")
            ai_status = result.get("status", "")
            reason    = result.get("reason", "")

            db_status = _map_ai_to_db_status(action, ai_status, degree)
            icon = "—" if db_status in ("ignore", "traite") else "✅"
            logger.info(f"  {icon} {full_name} ({degree}) → {db_status} | {reason[:70]}")
            update_status(contact_id, db_status)
            funnel[db_status] = funnel.get(db_status, 0) + 1

        except Exception as e:
            logger.warning(f"  ⚠️  Erreur IA pour {full_name}: {e}")
            fallback = "a_traiter" if degree == "1st" else "a_connecter"
            update_status(contact_id, fallback)
            funnel["ia_erreur"] += 1
            funnel[fallback] = funnel.get(fallback, 0) + 1

    # ── Phase 3 : contacts 'en_attente' dont le degré est maintenant '1st' ────
    waiting = get_pending_connection_checks(job_id)
    converted = 0
    for contact in waiting:
        if contact.get("degree") == "1st":
            update_status(contact["id"], "a_convertir")
            converted += 1
            logger.info(f"  🔄 {contact['full_name']} passé en a_convertir (degré 1st détecté)")

    # ── Enregistrement du scan ────────────────────────────────────────────────
    record_scan(job_id, total_found, new_contacts)

    # Nombre de contacts à traiter après l'analyse
    from core.database import get_contacts_to_process
    to_process = len(get_contacts_to_process(job_id))

    # ── ENTONNOIR : où se perdent les cas ? ───────────────────────────────────
    # État GLOBAL du job en base (tous scans confondus) pour ce job_id.
    with _conn() as con:
        rows = con.execute(
            "SELECT status, COUNT(*) c FROM contacts WHERE job_id=? GROUP BY status",
            (job_id,),
        ).fetchall()
    db_counts = {r["status"]: r["c"] for r in rows}
    db_total = sum(db_counts.values())

    logger.info("📊 ENTONNOIR " + "─" * 40)
    logger.info(f"   ① Chargés (entités DOM)   : {len(entities)}")
    logger.info(f"   ② Commentateurs extraits  : {total_found}  (dont {new_contacts} nouveaux)")
    logger.info(f"   ③ Analysés ce scan        : {len(pending_contacts)}")
    if pending_contacts:
        logger.info(f"        → trigger absent     : {funnel['trigger_absent']}")
        logger.info(f"        → a_traiter (DM)     : {funnel['a_traiter']}")
        logger.info(f"        → a_connecter        : {funnel['a_connecter']}")
        logger.info(f"        → en_attente         : {funnel['en_attente']}")
        logger.info(f"        → déjà traité/ignoré : {funnel['traite'] + funnel['ignore']}")
        if funnel["ia_erreur"]:
            logger.info(f"        → erreurs IA         : {funnel['ia_erreur']}")
    logger.info(f"   ④ ÉTAT GLOBAL job ({db_total} contacts) : "
                + ", ".join(f"{k}={v}" for k, v in sorted(db_counts.items())))
    logger.info(f"   ➡️  À TRAITER maintenant   : {to_process}")
    logger.info("   " + "─" * 51)

    result_summary = {
        "total_found":   total_found,
        "new_contacts":  new_contacts,
        "to_process":    to_process,
        "converted":     converted,
    }
    logger.info(f"📊 Scan terminé : {result_summary}")
    return result_summary


def _map_ai_to_db_status(action: str, ai_status: str, degree: str) -> str:
    """Convertit la réponse de l'IA en statut DB."""
    if action == "send_dm_then_comment":
        return "a_traiter" if degree == "1st" else "a_connecter"
    if action in ("post_connect_comment", "send_connect_comment"):
        return "a_connecter"
    # action == "nothing"
    if ai_status == "traite":
        return "traite"
    if ai_status in ("en_attente_connexion", "en_attente"):
        return "en_attente"
    if ai_status == "a_convertir":
        return "a_convertir"
    return "ignore"


async def _full_scroll_load(page, logger):
    """
    Scrolle le post du haut en bas EN UNE SEULE PASSE pour charger tous
    les commentaires, avec clics sur les boutons 'Charger plus'.
    Critère d'arrêt : 3 itérations consécutives sans augmentation de scrollHeight.
    """
    LOAD_MORE_SEL = (
        "button.comments-comments-list__load-more-comments-button, "
        "button[aria-label*='Load more comments'], "
        "button[aria-label*='Voir plus de commentaires'], "
        "button[aria-label*='Afficher plus de commentaires'], "
        "button[aria-label*='Charger plus']"
    )

    consecutive_stable = 0
    iteration = 0
    MAX_ITER  = 100

    while consecutive_stable < 3 and iteration < MAX_ITER:
        iteration += 1
        old_height = await page.evaluate("document.body.scrollHeight")

        # Scroll progressif en 5 étapes
        for pct in [0.2, 0.4, 0.6, 0.8, 1.0]:
            await page.evaluate(
                f"window.scrollTo(0, document.body.scrollHeight * {pct})"
            )
            await asyncio.sleep(0.4)

        # Clique sur tous les boutons "load more" visibles
        btns = await page.query_selector_all(LOAD_MORE_SEL)
        clicked_any = False
        for btn in btns:
            try:
                if await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    await btn.click()
                    clicked_any = True
                    await asyncio.sleep(2.0)
            except Exception:
                continue

        if clicked_any:
            await asyncio.sleep(1.5)

        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height > old_height:
            consecutive_stable = 0
            logger.debug(f"  📜 Scroll {iteration} : {old_height}→{new_height}px")
        else:
            consecutive_stable += 1
            logger.debug(f"  📜 Scroll {iteration} stable ({consecutive_stable}/3)")

    logger.info(f"✅ Scroll terminé en {iteration} itération(s)")


async def _extract_contact(entity, job_id: str, logger, thomas_replies: Optional[dict] = None):
    """
    Extrait les infos d'un commentateur depuis une entité DOM.

    Retourne :
      - dict          → contact extrait avec succès
      - "thomas"      → entité = une de mes propres réponses (skip légitime)
      - "no_link"     → aucun lien /in/ exploitable (entité perdue, à diagnostiquer)
      - "acoa"        → profil rendu en id hashé ACoA… (entité perdue)

    LinkedIn utilise une structure PLATE (siblings) — thomas_replies est la map
    construite en passe 1 (slug → texte de la réponse de Thomas).
    """
    # ── Identifie l'auteur de l'entité via le lien de la photo de profil ────────
    # Le lien contenant un <img> = lien de la photo = auteur réel du commentaire.
    # Les @mentions dans le texte sont de simples liens sans image.
    author_info = await entity.evaluate("""el => {
        const allLinks = Array.from(el.querySelectorAll('a[href*="/in/"]'));
        // Cherche le lien de la photo de profil (contient un <img>) en excluant les sous-entités
        for (const link of allLinks) {
            // Vérifie que ce lien n'est pas dans une .comments-comment-entity imbriquée
            let p = link.parentElement;
            let inNested = false;
            while (p && p !== el) {
                if (p.classList.contains('comments-comment-entity')) { inNested = true; break; }
                p = p.parentElement;
            }
            if (inNested) continue;
            // Le lien de la photo contient un <img>
            if (link.querySelector('img')) {
                return { href: link.href, isPhoto: true };
            }
        }
        // Fallback : premier lien top-level sans img (lien du nom)
        for (const link of allLinks) {
            let p = link.parentElement;
            let inNested = false;
            while (p && p !== el) {
                if (p.classList.contains('comments-comment-entity')) { inNested = true; break; }
                p = p.parentElement;
            }
            if (!inNested && link.href.includes('/in/')) {
                return { href: link.href, isPhoto: false };
            }
        }
        return null;
    }""")

    if not author_info:
        return "no_link"      # entité sans aucun lien /in/ → diagnosticable par l'appelant

    author_href = author_info.get("href", "")
    # Si l'auteur est Thomas → c'est une de ses réponses → skip légitime
    if MY_SLUG in author_href:
        return "thomas"

    # ── Slug du commentateur ──────────────────────────────────────────────────
    if "/in/" not in author_href:
        return "no_link"

    raw = unquote(author_href.split("/in/")[1].split("?")[0].rstrip("/"))
    if not raw or "ACoA" in raw:
        # Profil rendu avec un id hashé (ACoA…) au lieu du slug → pas exploitable,
        # mais on le signale à l'appelant pour ne plus perdre des gens en silence.
        return "acoa"
    profile_slug = raw

    # ── Nom (depuis le lien texte = lien du nom, pas de la photo) ────────────
    # IMPORTANT : on DÉCODE le href avant de comparer au slug. Le href du DOM est
    # percent-encodé (claire-gu%C3%A9nault-…) alors que le slug est décodé
    # (claire-guénault-…) → sans decodeURIComponent, le lien du nom n'était jamais
    # trouvé et le nom était reconstruit (avec le hash) → matching cassé ensuite.
    link_text = await entity.evaluate(f"""el => {{
        const slug = {json.dumps(profile_slug)};
        const allLinks = Array.from(el.querySelectorAll('a[href*="/in/"]'));
        for (const link of allLinks) {{
            let h;
            try {{ h = decodeURIComponent(link.getAttribute('href') || ''); }}
            catch (e) {{ h = link.getAttribute('href') || ''; }}
            if (h.includes(slug) && !link.querySelector('img')) {{
                return link.innerText.trim();
            }}
        }}
        return '';
    }}""")

    # Nom introuvable dans le DOM → on NE jette PLUS le contact : on a un slug
    # valide, le fallback ci-dessous reconstruit le nom depuis le slug.
    # (Avant : return None ici → le fallback était inatteignable → des
    # commentateurs valides étaient perdus en silence.)
    full_name  = (link_text or "").split("\n")[0].strip()
    # Le prénom = premier mot NON-TITRE du nom affiché ("Dr. François …" → "François").
    # Gère nativement les prénoms composés ("Pierre-Alban GUEZO" → "Pierre-Alban").
    first_name = _clean_first_name(full_name) if full_name else ""

    # Fallback slug UNIQUEMENT si le nom affiché est vide ou non exploitable
    # (pas de lettre = probablement un nom masqué type "Membre LinkedIn").
    # name_reconstructed = on n'a PAS pu lire le nom dans le DOM → candidat au
    # rattrapage profil (le nom reconstruit depuis le slug est peu fiable).
    name_reconstructed = False
    if not _looks_valid_name(first_name):
        name_reconstructed = True
        if "-" in profile_slug:
            # Reconstruit le nom depuis le slug en RETIRANT le hash final
            # (ex: "claire-guénault-4a396880" → "Claire Guénault", sans "4a396880").
            parts = [p for p in profile_slug.split("-") if not _is_hash_token(p)]
            parts = parts or profile_slug.split("-")
            first_name = _clean_first_name(" ".join(p.capitalize() for p in parts))
            full_name  = " ".join(p.capitalize() for p in parts)
        else:
            first_name = profile_slug.capitalize()
            full_name  = profile_slug.capitalize()

    # ── Degré de connexion ────────────────────────────────────────────────────
    # Le badge (• 2e / • 1st) n'est pas dans innerText mais peut être dans textContent
    try:
        entity_tc = await entity.evaluate("el => el.textContent")
    except Exception:
        entity_tc = ""

    degree = _detect_degree(entity_tc)

    # ── Texte du commentaire ──────────────────────────────────────────────────
    try:
        comment_text = await entity.evaluate("""el => {
            const contentEl = el.querySelector(
                '.comments-comment-entity__inline-show-more-text, ' +
                '.feed-shared-inline-show-more-text, ' +
                '[class*="comment-content"], ' +
                '[class*="comment-text"]'
            );
            if (contentEl) return contentEl.innerText.trim();
            // Fallback : texte de l'entité sans les sous-entités
            const clone = el.cloneNode(true);
            clone.querySelectorAll('.comments-comment-entity').forEach(s => s.remove());
            clone.querySelectorAll('[class*="commenter-header"],[class*="actor"],[class*="meta"]').forEach(s => s.remove());
            return clone.innerText.trim();
        }""")
    except Exception:
        comment_text = ""

    # ── Thread complet pour l'IA (commentaire + réponse(s) de Thomas) ────────
    # thomas_replies est la map {slug → texte} construite en passe 1 (séquentielle).
    # Peut contenir plusieurs réponses séparées par "---".
    thomas_reply = (thomas_replies or {}).get(profile_slug, "")
    if thomas_reply:
        # Formate chaque bloc de réponse séparé
        reply_blocks = thomas_reply.split("\n---\n")
        formatted_replies = "\n\n".join(
            f"[Réponse de Thomas]\n{block.strip()}" for block in reply_blocks if block.strip()
        )
        thread_text = (
            f"[Commentaire de {full_name}]\n{comment_text}\n\n"
            f"{formatted_replies}"
        )
    else:
        thread_text = comment_text

    # Candidat au rattrapage profil si la lecture a échoué partiellement :
    # nom reconstruit depuis le slug (priorité 0) ou degré indéterminé (priorité 1).
    needs_recovery   = name_reconstructed or (degree is None)
    recover_priority = 0 if name_reconstructed else 1

    return {
        "profile_slug":     profile_slug,
        "full_name":        full_name,
        "first_name":       first_name,
        "profile_url":      f"https://www.linkedin.com/in/{profile_slug}",
        "degree":           degree,
        "comment_text":     comment_text,
        "thread_text":      thread_text,
        "needs_recovery":   needs_recovery,
        "recover_priority": recover_priority,
    }


async def _recover_from_profile(context, profile_url: str, logger) -> Optional[dict]:
    """
    DOUBLE VÉRIFICATION : ouvre le profil dans un onglet temporaire de la MÊME
    session CloakBrowser (même cookie, même IP → aucun risque ajouté) pour lire
    le vrai nom + degré quand la lecture depuis le commentaire a échoué.

    Retourne {"full_name", "first_name", "degree"} (champs éventuellement None/""),
    ou None si la page n'a pas pu être lue.
    """
    page = None
    try:
        page = await context.new_page()
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(random.uniform(2.5, 4.0))
        data = await page.evaluate("""() => {
            const h1 = document.querySelector('h1');
            const name = h1 ? h1.innerText.trim() : '';
            // Badge de distance (degré) : sélecteurs courants LinkedIn
            const badgeEl = document.querySelector(
                '.dist-value, [class*="dist-value"], .distance-badge'
            );
            const badge = badgeEl ? badgeEl.textContent.trim() : '';
            // Texte de la carte haute en secours pour la détection du degré
            const card = document.querySelector('main, .pv-top-card, section');
            const txt = card ? (card.textContent || '').slice(0, 3000) : '';
            return { name, badge, txt };
        }""")
    except Exception as e:
        logger.debug(f"   ↳ rattrapage profil échoué ({profile_url}) : {e}")
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass

    name = (data.get("name") or "").split("\n")[0].strip()
    # Le badge seul vaut "2nd"/"2e" sans préfixe → on le préfixe pour réutiliser
    # _detect_degree, puis on retombe sur le texte de la carte si besoin.
    badge = data.get("badge") or ""
    degree = _detect_degree("· " + badge) or _detect_degree(data.get("txt") or "")
    first_name = _clean_first_name(name) if name else ""
    return {"full_name": name, "first_name": first_name, "degree": degree}


def _detect_degree(text: str) -> Optional[str]:
    """Détecte le degré de connexion depuis le textContent d'une entité."""
    markers = {
        "1st": ["• 1st", "• 1er", "• 1ère", "•1st", "•1er", "· 1st", "· 1er",
                "1st degree", "1er degré", "1ère degré", "• 1st·", "1st ·"],
        "2nd": ["• 2nd", "• 2e", "•2nd", "•2e", "· 2nd", "· 2e",
                "2nd degree", "2e degré", "2ème degré", "• 2nd·", "2nd ·"],
        "3rd": ["• 3rd", "• 3e", "•3rd", "•3e", "· 3rd", "· 3e",
                "3rd degree", "3e degré", "3ème degré", "• 3rd·", "3rd ·"],
    }
    low = (text or "").lower()
    for degree, patterns in markers.items():
        for pat in patterns:
            if pat.lower() in low:
                return degree
    return None
