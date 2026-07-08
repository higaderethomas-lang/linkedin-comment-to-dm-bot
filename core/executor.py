"""
Exécution des actions (DM + commentaires) sur les contacts en attente.
Ne rescrolle JAMAIS depuis le début — navigation unique vers le post.
"""

import asyncio
import logging
import random
from datetime import datetime
from typing import Optional, Callable

from core.database import (
    get_contacts_to_process,
    get_contacts_pending_comment,
    update_status,
    check_daily_limits,
    check_weekly_limits,
    increment_daily,
    get_contacts_for_relance,
    mark_relance_sent,
)
from core.bot import (
    send_dm_tab,
    reply_inline,
    pick_dm,
    pick_confirm,
    pick_confirm_already,
    pick_connect,
    pick_relance,
    DM_DELAY_MIN,
    DM_DELAY_MAX,
    DM_PAUSE_EVERY,
    DM_PAUSE_MIN,
    DM_PAUSE_MAX,
    CONNECT_DELAY_MIN,
    CONNECT_DELAY_MAX,
    CONNECT_PAUSE_EVERY,
    CONNECT_PAUSE_MIN,
    CONNECT_PAUSE_MAX,
    MAX_DM_PER_DAY,
    MAX_COMMENTS_PER_DAY,
    MAX_DM_PER_WEEK,
    MAX_COMMENTS_PER_WEEK,
    _bot_running,
)

log = logging.getLogger(__name__)


async def _sleep_with_eta(logger, seconds: float, label: str):
    """Pause anti-ban LISIBLE : affiche l'heure de reprise et un battement toutes
    les ~60 s avec le temps restant → l'interface ne paraît jamais figée.
    S'interrompt si le bot est arrêté."""
    import core.bot as _bot_module
    from datetime import timedelta
    resume = datetime.now() + timedelta(seconds=seconds)
    logger.info(f"{label} — reprise à {resume.strftime('%H:%M:%S')} ({seconds/60:.1f} min)")
    remaining = seconds
    while remaining > 0:
        if not _bot_module._bot_running:
            return
        chunk = min(60, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk
        if remaining > 30:
            logger.info(f"   ⏱️  …{remaining/60:.1f} min restantes avant la prochaine action")


async def _comment_degree(page, profile_slug: str):
    """Lit le degré de connexion (1st/2nd/3rd) directement depuis le commentaire
    de la personne sur le post (badge « • 1er / • 2e »). Sert de re-vérif avant de
    poster un commentaire « connectez-vous » : on ne veut JAMAIS le faire à une
    relation déjà en 1er degré. Retourne None si indéterminé."""
    from core.scanner import _detect_degree
    try:
        text = await page.evaluate(
            """(slug) => {
                const dec = a => { try { return decodeURIComponent(a.getAttribute('href')||''); }
                                   catch(e){ return a.getAttribute('href')||''; } };
                for (const ent of document.querySelectorAll('.comments-comment-entity')) {
                    const hit = Array.from(ent.querySelectorAll('a[href*="/in/"]'))
                        .some(a => dec(a).includes('/in/' + slug));
                    if (hit) return ent.textContent || '';
                }
                return '';
            }""",
            profile_slug,
        )
    except Exception:
        return None
    return _detect_degree(text or "")


async def process_pending_actions(
    job: dict,
    page,
    context,
    logger=None,
    on_result: Optional[Callable] = None,
) -> dict:
    """
    Exécute les actions en attente pour un job donné.
    Navigue vers post_url UNE SEULE FOIS puis scroll initial.
    """
    if logger is None:
        logger = log

    job_id            = job.get("id", job["post_url"])
    label             = job.get("label", job_id)
    doc_url           = job.get("doc_url", "")
    dm_templates      = job.get("dm_templates") or None
    confirm_templates = job.get("confirm_templates") or None
    connect_templates = job.get("connect_templates") or None

    contacts = get_contacts_to_process(job_id)
    # Contacts ayant reçu un DM mais sans commentaire de confirmation → à rattraper.
    pending_comments = get_contacts_pending_comment(job_id)
    if not contacts and not pending_comments:
        logger.info(f"😴 Aucun contact à traiter pour {label}")
        return {"processed": 0, "dms": 0, "comments": 0}

    logger.info(f"🔧 {len(contacts)} action(s) + {len(pending_comments)} commentaire(s) "
                f"à rattraper pour {label}")

    # ── Navigation unique vers le post (avec retry anti-coupure réseau) ────────
    logger.info(f"🌐 Navigation vers {job['post_url']}")
    nav_ok = False
    for attempt in range(3):
        try:
            await page.goto(job["post_url"], wait_until="domcontentloaded", timeout=60000)
            nav_ok = True
            break
        except Exception as e:
            wait = (attempt + 1) * 15
            logger.warning(f"⚠️  Navigation échouée (essai {attempt+1}/3) : {e}. Retry dans {wait}s…")
            await asyncio.sleep(wait)
    if not nav_ok:
        logger.error(f"❌ Impossible de charger le post après 3 essais — job abandonné ce cycle")
        return {"processed": 0, "dms": 0, "comments": 0}
    await asyncio.sleep(5)

    # Scroll initial pour charger les commentaires visibles
    for pct in [0.3, 0.6, 0.9, 1.0]:
        await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pct})")
        await asyncio.sleep(1.2)

    processed  = 0
    dms_done   = 0
    comments_done = 0
    dm_count_session  = 0   # pour les pauses DM
    conn_count_session = 0  # pour les pauses connect

    total = len(contacts)
    for idx, contact in enumerate(contacts, 1):
        # Vérifie que le bot est toujours actif
        import core.bot as _bot_module
        if not _bot_module._bot_running:
            logger.info("⛔ Bot arrêté — arrêt de l'exécuteur")
            break

        contact_id   = contact["id"]
        profile_slug = contact["profile_slug"]
        full_name    = contact["full_name"]
        first_name   = contact["first_name"]
        profile_url  = contact["profile_url"]
        status       = contact["status"]
        remaining    = total - idx

        # ── Vérification des limites journalières ET hebdomadaires ────────────
        dms_today, comments_today = check_daily_limits()
        dms_week,  comments_week  = check_weekly_limits()

        if dms_today >= MAX_DM_PER_DAY and comments_today >= MAX_COMMENTS_PER_DAY:
            logger.info(f"⚠️  Limites journalières atteintes (DM:{dms_today}/{MAX_DM_PER_DAY}, Comments:{comments_today}/{MAX_COMMENTS_PER_DAY}) — arrêt")
            break

        if dms_week >= MAX_DM_PER_WEEK and comments_week >= MAX_COMMENTS_PER_WEEK:
            logger.info(f"⚠️  Limites HEBDO atteintes (DM:{dms_week}/{MAX_DM_PER_WEEK}, Comments:{comments_week}/{MAX_COMMENTS_PER_WEEK}) — arrêt")
            break

        action_lbl = {"a_traiter": "DM + commentaire", "a_convertir": "DM (reconnecté)",
                      "a_connecter": "commentaire connexion"}.get(status, status)
        logger.info(f"{'='*50}")
        logger.info(f"🎯 [{idx}/{total}] {full_name} → {action_lbl}  ·  {remaining} restant(s) ensuite")

        # ── Liste d'exclusion manuelle : skip TOTAL (ni DM ni commentaire) ───
        # Placé AVANT la recherche du commentaire pour ne perdre aucune seconde.
        if _bot_module.is_excluded(profile_slug, full_name):
            logger.info(f"  🛑 {full_name} dans la liste d'exclusion → skip total (ni DM ni commentaire)")
            update_status(contact_id, "exclu")
            continue

        # ── Scroll vers le commentaire cible ──────────────────────────────────
        await scroll_to_comment(page, full_name, profile_slug, logger)

        # ── Actions selon le statut ───────────────────────────────────────────
        try:
            if status in ("a_traiter", "a_convertir"):
                # DM puis commentaire de confirmation
                if dms_today >= MAX_DM_PER_DAY:
                    logger.info(f"   ⏭️  Quota DM atteint — skip {full_name}")
                    continue

                msg       = pick_dm(first_name, doc_url, dm_templates)
                dm_result = await send_dm_tab(context, profile_url, msg, full_name)

                # Codes retour de send_dm_tab / _send_dm :
                #   True               → DM réellement envoyé maintenant
                #   "already_in_thread"→ déjà reçu (lien vu dans la conversation)
                #   "already_sent_db"  → déjà reçu (DM tracé sur un autre post)
                #   "excluded"         → liste d'exclusion
                #   False              → échec

                if dm_result is True:
                    # ── DM envoyé MAINTENANT → on l'enregistre EN BASE IMMÉDIATEMENT,
                    # AVANT de tenter le commentaire. Si reply_inline plante, le DM
                    # reste tracé → la dédup bloque tout renvoi (cause du double-DM
                    # à Franck : le crash du commentaire annulait l'enregistrement).
                    # Le commentaire manquant sera rattrapé au cycle suivant.
                    logger.info(f"   DM : ✅ OK (envoyé)")
                    now = datetime.now().isoformat(sep=" ", timespec="seconds")
                    update_status(contact_id, "traite", dm_sent_at=now, dm_doc_url=doc_url)
                    increment_daily("dms_sent")
                    dms_today += 1
                    dms_done  += 1
                    dm_count_session += 1
                    await asyncio.sleep(random.uniform(10, 25))

                    confirm    = pick_confirm(first_name, confirm_templates)
                    comment_ok = await reply_inline(page, profile_slug, first_name, confirm, full_name)
                    if comment_ok:
                        increment_daily("comments")
                        comments_done += 1
                        now = datetime.now().isoformat(sep=" ", timespec="seconds")
                        update_status(contact_id, "traite", comment_sent_at=now)

                    if on_result:
                        on_result({
                            "type":   "result", "job_id": job_id, "name": full_name,
                            "action": "DM", "status": "ok",
                            "ts":     datetime.now().strftime("%H:%M"),
                        })

                    # Pause anti-ban après un vrai DM (avec heure de reprise + reste)
                    if remaining > 0:
                        if dm_count_session % DM_PAUSE_EVERY == 0:
                            pause = random.uniform(DM_PAUSE_MIN, DM_PAUSE_MAX)
                        else:
                            pause = random.uniform(DM_DELAY_MIN, DM_DELAY_MAX)
                        await _sleep_with_eta(logger, pause, f"⏳ Pause anti-ban — {remaining} contact(s) en attente")

                elif dm_result in ("already_in_thread", "already_sent_db"):
                    # ── Déjà reçu le DM (pas d'envoi maintenant) → on revient quand même
                    # sur le commentaire pour répondre « Bien reçu en DM 👍 » (véridique).
                    logger.info(f"   DM : 📩 Déjà reçu ({dm_result}) → commentaire 'bien reçu'")
                    await asyncio.sleep(3)
                    confirm    = pick_confirm_already(first_name)
                    comment_ok = await reply_inline(page, profile_slug, first_name, confirm, full_name)
                    if comment_ok:
                        increment_daily("comments")
                        comments_done += 1
                        now = datetime.now().isoformat(sep=" ", timespec="seconds")
                        update_status(contact_id, "traite", comment_sent_at=now)
                    else:
                        # Commentaire non posté → on NE marque PAS 'traite' : le contact
                        # reste a_traiter et sera retenté au prochain cycle (la dédup
                        # re-détectera 'déjà reçu' et on retentera juste le commentaire).
                        logger.info(f"   ⏭️  Commentaire 'bien reçu' non posté → réessai au prochain cycle")
                    if on_result:
                        on_result({
                            "type":   "result", "job_id": job_id, "name": full_name,
                            "action": "DM", "status": "deja_recu",
                            "ts":     datetime.now().strftime("%H:%M"),
                        })
                    continue

                elif dm_result == "excluded":
                    logger.info(f"   🛑 {full_name} exclu → marqué 'exclu'")
                    update_status(contact_id, "exclu")
                    continue

                else:
                    # DM échoué → pas de changement de statut, on tente le suivant
                    logger.info(f"   DM : ❌ ÉCHEC")
                    if on_result:
                        on_result({
                            "type":   "result", "job_id": job_id, "name": full_name,
                            "action": "DM", "status": "fail",
                            "ts":     datetime.now().strftime("%H:%M"),
                        })
                    continue

            elif status == "a_connecter":
                # ── GARDE-FOU : ne JAMAIS demander de se connecter à une relation
                # déjà en 1er degré. On re-lit le degré sur le commentaire ; si 1er
                # degré → on repasse en a_traiter (DM direct) et on saute le tour.
                deg = await _comment_degree(page, profile_slug)
                if deg == "1st":
                    logger.info(f"   🔁 {full_name} est en fait 1er degré → PAS de 'connectez-vous', "
                                f"repassé en a_traiter (DM au prochain tour)")
                    update_status(contact_id, "a_traiter")
                    continue

                # Commentaire "connectez-vous"
                if comments_today >= MAX_COMMENTS_PER_DAY:
                    logger.info(f"   ⏭️  Quota comments atteint — skip {full_name}")
                    continue

                connect    = pick_connect(first_name, connect_templates)
                comment_ok = await reply_inline(page, profile_slug, first_name, connect, full_name)
                logger.info(f"   Comment : {'✅ OK' if comment_ok else '❌ ÉCHEC'}")

                if comment_ok:
                    increment_daily("comments")
                    comments_today += 1
                    comments_done  += 1
                    conn_count_session += 1

                    now = datetime.now().isoformat(sep=" ", timespec="seconds")
                    update_status(contact_id, "en_attente", connect_sent_at=now)

                    if on_result:
                        on_result({
                            "type":    "result",
                            "job_id":  job_id,
                            "name":    full_name,
                            "action":  "Connexion demandée",
                            "status":  "ok",
                            "ts":      datetime.now().strftime("%H:%M"),
                        })

                    # Pauses anti-ban pour les commentaires de connexion (lisibles)
                    if remaining > 0:
                        if conn_count_session % CONNECT_PAUSE_EVERY == 0 and conn_count_session > 0:
                            pause = random.uniform(CONNECT_PAUSE_MIN, CONNECT_PAUSE_MAX)
                        else:
                            pause = random.uniform(CONNECT_DELAY_MIN, CONNECT_DELAY_MAX)
                        await _sleep_with_eta(logger, pause, f"⏳ Pause anti-ban — {remaining} contact(s) en attente")
                else:
                    if on_result:
                        on_result({
                            "type":   "result",
                            "job_id": job_id,
                            "name":   full_name,
                            "action": "Connexion demandée",
                            "status": "fail",
                            "ts":     datetime.now().strftime("%H:%M"),
                        })
                    continue

            processed += 1

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"❌ Erreur traitement {full_name}: {e}", exc_info=True)
            continue

    # ── RATTRAPAGE : commentaires de confirmation manquants ───────────────────
    # Contacts qui ont reçu leur DM mais dont le commentaire « envoyé en DM » n'a
    # jamais été posté → on le poste maintenant (visibilité claire), puis on
    # enregistre comment_sent_at pour ne plus y revenir.
    if pending_comments:
        import core.bot as _bot_module
        _, comments_today = check_daily_limits()
        logger.info(f"💬 Rattrapage : {len(pending_comments)} commentaire(s) de confirmation")
        for contact in pending_comments:
            if not _bot_module._bot_running:
                break
            if comments_today >= MAX_COMMENTS_PER_DAY:
                logger.info("⚠️  Quota commentaires atteint — rattrapage stoppé")
                break
            cid        = contact["id"]
            full_name  = contact["full_name"]
            slug       = contact["profile_slug"]
            first_name = contact["first_name"]
            try:
                logger.info(f"{'='*50}")
                logger.info(f"🩹 Rattrapage commentaire → {full_name} (slug={slug})")
                await scroll_to_comment(page, full_name, slug, logger)
                confirm = pick_confirm(first_name, confirm_templates)
                ok = await reply_inline(page, slug, first_name, confirm, full_name)
                now = datetime.now().isoformat(sep=" ", timespec="seconds")
                if ok:
                    increment_daily("comments")
                    comments_today += 1
                    comments_done  += 1
                    update_status(cid, "traite", comment_sent_at=now)
                    logger.info(f"   ✅ Commentaire rattrapé pour {full_name}")
                else:
                    logger.info(f"   ⏭️  Commentaire non posté pour {full_name} (réessai au prochain cycle)")
                # Délai anti-ban entre commentaires
                await asyncio.sleep(random.uniform(CONNECT_DELAY_MIN, CONNECT_DELAY_MAX))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"❌ Erreur rattrapage commentaire {full_name}: {e}")
                continue

    result = {"processed": processed, "dms": dms_done, "comments": comments_done}
    logger.info(f"🎯 Exécution terminée : {result}")
    return result


async def process_relances(job: dict, context, logger=None, on_result=None) -> dict:
    """
    Envoie un DM de relance aux contacts 'traite' (DM déjà reçu) dont le DM initial
    date de plus de `relance_delay_days` jours, et qui n'ont pas encore été relancés.

    DM uniquement (pas de commentaire). Réutilise send_dm_tab avec skip_dup_check=True
    (on VEUT écrire à quelqu'un déjà contacté) — le garde-fou destinataire reste actif.
    Respecte les quotas DM journaliers/hebdo et les mêmes délais anti-ban.
    """
    if logger is None:
        logger = log

    if not job.get("relance_enabled"):
        return {"relances": 0}

    job_id      = job.get("id", job["post_url"])
    label       = job.get("label", job_id)
    doc_url     = job.get("doc_url", "")
    delay_days  = int(job.get("relance_delay_days", 5))
    templates   = job.get("relance_templates") or None

    contacts = get_contacts_for_relance(job_id, delay_days)
    if not contacts:
        logger.info(f"😴 Aucune relance à envoyer pour {label}")
        return {"relances": 0}

    logger.info(f"🔄 {len(contacts)} relance(s) à envoyer pour {label} (DM initial > {delay_days}j)")

    sent = 0
    dm_count_session = 0

    for contact in contacts:
        import core.bot as _bot_module
        if not _bot_module._bot_running:
            logger.info("⛔ Bot arrêté — arrêt des relances")
            break

        # ── Quotas DM (journalier + hebdo) ────────────────────────────────────
        dms_today, _   = check_daily_limits()
        dms_week,  _   = check_weekly_limits()
        if dms_today >= MAX_DM_PER_DAY:
            logger.info(f"⚠️  Quota DM journalier atteint ({dms_today}/{MAX_DM_PER_DAY}) — relances stoppées")
            break
        if dms_week >= MAX_DM_PER_WEEK:
            logger.info(f"⚠️  Quota DM hebdo atteint ({dms_week}/{MAX_DM_PER_WEEK}) — relances stoppées")
            break

        contact_id   = contact["id"]
        first_name   = contact["first_name"]
        full_name    = contact["full_name"]
        profile_url  = contact["profile_url"]

        logger.info(f"{'='*50}")
        logger.info(f"🔄 Relance → {full_name} | DM initial : {contact.get('dm_sent_at')}")

        msg = pick_relance(first_name, doc_url, templates)
        dm_result = await send_dm_tab(context, profile_url, msg, full_name,
                                      skip_dup_check=True, skip_if_replied=True)

        if dm_result == "replied":
            # La personne a déjà répondu → on N'ENVOIE PAS la relance. Statut
            # 'a_repondu' = sort de la file de relance + visibilité dans les stats.
            logger.info(f"   🙅 {full_name} a déjà répondu → relance annulée (statut 'a_repondu')")
            update_status(contact_id, "a_repondu")
            if on_result:
                on_result({
                    "type": "result", "job_id": job_id, "name": full_name,
                    "action": "Relance", "status": "a_repondu",
                    "ts": datetime.now().strftime("%H:%M"),
                })
            continue

        if dm_result == "unreadable":
            # Conversation illisible → par prudence (Option A) on NE relance pas ce
            # cycle ; pas de marquage → on réessaiera au prochain passage.
            logger.info(f"   ⏸️  Conversation illisible pour {full_name} → relance reportée")
            continue

        if dm_result is True:
            increment_daily("dms_sent")
            mark_relance_sent(contact_id)
            sent += 1
            dm_count_session += 1
            logger.info(f"   ✅ Relance envoyée à {full_name}")
            if on_result:
                on_result({
                    "type": "result", "job_id": job_id, "name": full_name,
                    "action": "Relance", "status": "ok",
                    "ts": datetime.now().strftime("%H:%M"),
                })

            # Délais anti-ban (mêmes que les DMs normaux)
            if dm_count_session % DM_PAUSE_EVERY == 0:
                pause = random.uniform(DM_PAUSE_MIN, DM_PAUSE_MAX)
                logger.info(f"😴 Pause DM ({pause/60:.1f} min)...")
                await asyncio.sleep(pause)
            else:
                delay = random.uniform(DM_DELAY_MIN, DM_DELAY_MAX)
                logger.info(f"⏳ Pause relance : {delay:.0f}s")
                await asyncio.sleep(delay)

        elif dm_result in ("excluded", "already_in_thread", "already_sent_db"):
            # Exclu ou déjà reçu → on ne relance pas, on marque pour ne pas boucler.
            logger.info(f"   📩 Relance non envoyée ({dm_result}) — marqué relancé")
            mark_relance_sent(contact_id)

        else:
            # Échec (garde-fou destinataire, reCAPTCHA, etc.) → pas de marquage,
            # on réessaiera au prochain cycle.
            logger.info(f"   ❌ Relance échouée pour {full_name} — retry au prochain cycle")
            if on_result:
                on_result({
                    "type": "result", "job_id": job_id, "name": full_name,
                    "action": "Relance", "status": "fail",
                    "ts": datetime.now().strftime("%H:%M"),
                })

    result = {"relances": sent}
    logger.info(f"🎯 Relances terminées : {result}")
    return result


async def scroll_to_comment(page, full_name: str, profile_slug: str, logger=None):
    """
    Fait défiler la page vers le commentaire du contact cible.
    Charge les commentaires supplémentaires (Load more) jusqu'à trouver le commentaire
    ou atteindre la fin de la page — sans limite de temps fixe.
    """
    if logger is None:
        logger = log

    LOAD_MORE_SEL = (
        "button.comments-comments-list__load-more-comments-button, "
        "button[aria-label*='Load more comments'], "
        "button[aria-label*='Voir plus de commentaires'], "
        "button[aria-label*='Afficher plus de commentaires'], "
        "button[aria-label*='Charger plus']"
    )

    # Échappe les apostrophes pour l'injection JS
    APOS = "\\'"
    safe_name = full_name.replace("'", APOS)
    safe_slug = profile_slug.replace("'", APOS)

    JS_FIND = f"""
        () => {{
            const name = '{safe_name}';
            const slug = '{safe_slug}';

            // Stratégie 1 : bouton Reply avec aria-label contenant le nom
            const replyBtns = Array.from(document.querySelectorAll(
                'button[aria-label*="Reply to"], button[aria-label*="Répondre à"], '
                + 'button[aria-label*="répondre à"]'
            ));
            const byLabel = replyBtns.find(b =>
                (b.getAttribute('aria-label') || '').includes(name)
            );
            if (byLabel) {{
                byLabel.scrollIntoView({{behavior:'smooth', block:'center'}});
                return 'label';
            }}

            // Stratégie 2 : lien href contenant le slug (href DÉCODÉ : le DOM est
            // percent-encodé alors que le slug est décodé/accentué).
            const ENTITY = '.comments-comment-entity, [data-urn*="comment"]';
            const dec = a => {{ try {{ return decodeURIComponent(a.getAttribute('href')||''); }} catch(e) {{ return a.getAttribute('href')||''; }} }};
            const link = Array.from(document.querySelectorAll('a[href*="/in/"]'))
                             .find(a => dec(a).includes('/in/' + slug) && a.closest(ENTITY));
            if (link) {{
                link.scrollIntoView({{behavior:'smooth', block:'center'}});
                return 'slug';
            }}

            return null;
        }}
    """

    # Essai 1 : commentaire déjà dans le DOM
    result = await page.evaluate(JS_FIND)
    if result:
        logger.info(f"  📍 Commentaire trouvé via '{result}'")
        await asyncio.sleep(1.0)
        return True

    # Essai 2+ : charge les commentaires manquants et réessaye
    logger.info(f"  🔄 Commentaire non visible — chargement des commentaires...")
    stable_rounds = 0
    round_num = 0

    while stable_rounds < 3:
        round_num += 1
        old_height = await page.evaluate("document.body.scrollHeight")

        # Scroll progressif jusqu'en bas
        for pct in [0.5, 0.75, 1.0]:
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pct})")
            await asyncio.sleep(0.4)

        # Clique tous les boutons "Load more" visibles
        btns = await page.query_selector_all(LOAD_MORE_SEL)
        clicked = False
        for btn in btns:
            try:
                if await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    await btn.click()
                    clicked = True
                    await asyncio.sleep(2.0)
            except Exception:
                continue

        if clicked:
            await asyncio.sleep(1.5)

        new_height = await page.evaluate("document.body.scrollHeight")

        # Vérifie si le commentaire est maintenant dans le DOM
        result = await page.evaluate(JS_FIND)
        if result:
            logger.info(f"  📍 Commentaire de {full_name} trouvé après {round_num} round(s) via '{result}'")
            await asyncio.sleep(1.0)
            return True

        # Vérifie si on a chargé du nouveau contenu
        if new_height > old_height or clicked:
            stable_rounds = 0
            logger.debug(f"  📜 Round {round_num} : nouveaux commentaires chargés")
        else:
            stable_rounds += 1
            logger.debug(f"  📜 Round {round_num} : page stable ({stable_rounds}/3)")

    logger.warning(f"  ⚠️  Commentaire de {full_name} introuvable après chargement complet")
    return False
