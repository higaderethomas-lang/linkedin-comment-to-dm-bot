"""
Serveur web pour contrôler le LinkedIn Bot via une interface graphique.
Lance avec : python3 server.py
Puis ouvre : http://localhost:8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sys
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from core.bot import run_bot, login_and_save, _new_browser_context
from core.database import init_db, get_all_contacts, get_stats, reprocess_contact
from core import whatsapp as wa

app = FastAPI()

JOBS_PATH = Path(__file__).parent / "data" / "jobs.json"
JOBS_PATH.parent.mkdir(exist_ok=True, parents=True)

# Initialisation de la DB au démarrage du serveur
init_db()

# ── État global du bot ────────────────────────────────────────────────────────
bot_task: asyncio.Task | None = None
log_queue: queue.Queue = queue.Queue(maxsize=500)
action_results: list = []
session_stats: dict = {"dms": 0, "comments": 0, "pending_by_job": {}}


# ── Handler de log qui pousse dans la queue SSE ──────────────────────────────
class QueueHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        try:
            log_queue.put_nowait(msg)
        except queue.Full:
            log_queue.get_nowait()
            log_queue.put_nowait(msg)


queue_handler = QueueHandler()
queue_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(queue_handler)

# Handler WhatsApp (OpenWA) — no-op tant que data/whatsapp_config.json n'est pas rempli
wa_handler = wa.WhatsAppLogHandler()
wa_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(wa_handler)


# ── Helpers jobs ──────────────────────────────────────────────────────────────
def load_jobs() -> list:
    if JOBS_PATH.exists():
        return json.loads(JOBS_PATH.read_text())
    return []


def save_jobs(jobs: list):
    JOBS_PATH.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))


# ── Modèles ───────────────────────────────────────────────────────────────────
class JobPayload(BaseModel):
    post_url:           str
    doc_url:            str
    trigger:            str = ""
    label:              str = ""
    dm_templates:       list[str] = []
    confirm_templates:  list[str] = []
    connect_templates:  list[str] = []
    handle_degrees:     list[str] = ["1st", "2nd", "3rd"]
    relance_enabled:    bool = False
    relance_delay_days: int = 5
    relance_templates:  list[str] = []


class StartPayload(BaseModel):
    headless: bool = False


# ── Routes jobs ───────────────────────────────────────────────────────────────
@app.get("/jobs")
def get_jobs():
    return load_jobs()


@app.post("/jobs")
def add_job(payload: JobPayload):
    jobs = load_jobs()
    job = {
        "id":                str(uuid.uuid4()),
        "label":             payload.label or f"Post {len(jobs)+1}",
        "post_url":          payload.post_url,
        "doc_url":           payload.doc_url,
        "trigger":           payload.trigger,
        "active":            True,
        "dm_templates":      [t for t in payload.dm_templates      if t.strip()],
        "confirm_templates": [t for t in payload.confirm_templates if t.strip()],
        "connect_templates": [t for t in payload.connect_templates if t.strip()],
        "handle_degrees":    payload.handle_degrees or ["1st", "2nd", "3rd"],
        "relance_enabled":    payload.relance_enabled,
        "relance_delay_days": payload.relance_delay_days,
        "relance_templates":  [t for t in payload.relance_templates if t.strip()],
    }
    jobs.append(job)
    save_jobs(jobs)
    return job


@app.patch("/jobs/{job_id}")
def toggle_job(job_id: str):
    jobs = load_jobs()
    for job in jobs:
        if job["id"] == job_id:
            job["active"] = not job.get("active", True)
            save_jobs(jobs)
            return {"ok": True, "active": job["active"]}
    raise HTTPException(status_code=404, detail="Job introuvable")


@app.put("/jobs/{job_id}")
def update_job(job_id: str, payload: JobPayload):
    jobs = load_jobs()
    for job in jobs:
        if job["id"] == job_id:
            job["label"]             = payload.label or job["label"]
            job["post_url"]          = payload.post_url
            job["doc_url"]           = payload.doc_url
            job["trigger"]           = payload.trigger
            job["dm_templates"]      = [t for t in payload.dm_templates      if t.strip()]
            job["confirm_templates"] = [t for t in payload.confirm_templates if t.strip()]
            job["connect_templates"] = [t for t in payload.connect_templates if t.strip()]
            job["handle_degrees"]    = payload.handle_degrees or ["1st", "2nd", "3rd"]
            job["relance_enabled"]    = payload.relance_enabled
            job["relance_delay_days"] = payload.relance_delay_days
            job["relance_templates"]  = [t for t in payload.relance_templates if t.strip()]
            save_jobs(jobs)
            return job
    raise HTTPException(status_code=404, detail="Job introuvable")


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    jobs = load_jobs()
    new_jobs = [j for j in jobs if j["id"] != job_id]
    if len(new_jobs) == len(jobs):
        raise HTTPException(status_code=404, detail="Job introuvable")
    save_jobs(new_jobs)
    return {"ok": True}


# ── Routes base de données ────────────────────────────────────────────────────

@app.post("/scan")
async def trigger_scan():
    jobs = load_jobs()
    active = [j for j in jobs if j.get("active", True)]
    if not active:
        return {"ok": False, "message": "Aucun job actif"}

    async def do_scan():
        from core.scanner import daily_scan
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser, context = await _new_browser_context(p, headless=True)
            try:
                for job in active:
                    page = await context.new_page()
                    try:
                        await daily_scan(
                            job, page, context,
                            logging.getLogger("scanner")
                        )
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    asyncio.create_task(do_scan())
    return {"ok": True, "message": f"Scan lancé pour {len(active)} job(s)"}


@app.get("/contacts")
def api_get_contacts(job_id: str = None):
    return get_all_contacts(job_id)


@app.get("/stats")
def api_get_stats():
    return get_stats()


@app.post("/contacts/{contact_id}/reprocess")
def api_reprocess(contact_id: str):
    reprocess_contact(contact_id)
    return {"ok": True}


# ── Routes API ────────────────────────────────────────────────────────────────
@app.get("/status")
def status():
    running = bot_task is not None and not bot_task.done()
    return {"running": running}


@app.post("/login")
async def login():
    if bot_task and not bot_task.done():
        return {"ok": False, "message": "Arrête le bot avant de te reconnecter"}
    asyncio.create_task(login_and_save())
    return {"ok": True, "message": "Navigateur ouvert — connecte-toi manuellement puis ferme la fenêtre"}


@app.get("/results")
def get_results():
    return action_results


@app.get("/session-stats")
def get_session_stats():
    return session_stats


@app.post("/start")
async def start(payload: StartPayload):
    global bot_task, action_results

    if bot_task and not bot_task.done():
        return {"ok": False, "message": "Bot déjà en cours"}

    jobs = load_jobs()
    if not jobs:
        return {"ok": False, "message": "Aucun job configuré"}

    action_results = []
    session_stats["dms"] = 0
    session_stats["comments"] = 0
    session_stats["pending_by_job"] = {}

    def on_result(r):
        t = r.get("type", "result")
        job_id = r.get("job_id")

        if t == "pending" and job_id:
            # Nouveau scrape : on initialise / réinitialise le pending pour ce job
            session_stats["pending_by_job"][job_id] = {
                "label":   r.get("job_label", job_id),
                "pending": r.get("count", 0),
            }
        elif t == "progress" and job_id:
            if job_id in session_stats["pending_by_job"]:
                session_stats["pending_by_job"][job_id]["pending"] = r.get("remaining", 0)
        elif t == "result":
            action = r.get("action", "")
            if r.get("status") == "ok":
                if "DM" in action:
                    session_stats["dms"] += 1
                else:
                    session_stats["comments"] += 1
            action_results.append(r)

    bot_task = asyncio.create_task(
        run_bot(jobs=jobs, headless=payload.headless, on_result=on_result)
    )
    return {"ok": True, "message": f"Bot démarré ({len(jobs)} job(s))"}


@app.post("/stop")
async def stop():
    global bot_task
    if bot_task and not bot_task.done():
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
        bot_task = None
        log_queue.put_nowait("⛔ Bot arrêté manuellement.")
        return {"ok": True, "message": "Bot arrêté"}
    return {"ok": False, "message": "Aucun bot en cours"}


@app.get("/logs")
async def logs():
    async def generate():
        yield "data: ✅ Connecté au flux de logs\n\n"
        try:
            while True:
                try:
                    msg = log_queue.get_nowait()
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            # Le client s'est déconnecté — on arrête proprement le générateur
            pass

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Pilotage par WhatsApp (OpenWA) ──────────────────────────────────────────
async def handle_wa_command(text: str) -> str:
    """Mappe un message WhatsApp entrant vers une action du bot."""
    cmd = text.strip().lower()

    if cmd in ("start", "démarrer", "demarrer", "go", "▶️", "▶"):
        res = await start(StartPayload(headless=True))
        return "▶️ " + res.get("message", "OK")

    if cmd in ("stop", "arrêter", "arreter", "arrête", "arrete", "■"):
        res = await stop()
        return "⛔ " + res.get("message", "OK")

    if cmd in ("status", "statut", "état", "etat"):
        return "🟢 Bot en cours" if status()["running"] else "⚪️ Bot arrêté"

    if cmd in ("stats", "stat"):
        pending = sum(j.get("pending", 0) for j in session_stats.get("pending_by_job", {}).values())
        return (f"📊 Session : {session_stats['dms']} DM, "
                f"{session_stats['comments']} commentaires, {pending} en attente")

    if cmd == "scan":
        res = await trigger_scan()
        return "🔍 " + res.get("message", "OK")

    if cmd in ("help", "aide", "?", "menu", "commandes"):
        return ("🤖 Commandes :\n"
                "• *start* — démarrer le bot\n"
                "• *stop* — arrêter le bot\n"
                "• *status* — bot en cours ?\n"
                "• *stats* — DM/commentaires de la session\n"
                "• *scan* — relancer un scan")

    # Commande non reconnue → None : on N'ENVOIE PAS de réponse. C'est volontaire :
    # ça évite toute boucle avec les messages que le bot s'envoie à lui-même
    # (notifs/réponses), captés aussi par l'event message.sent.
    return None


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    raw = await request.body()
    if not wa.verify_signature(raw, dict(request.headers)):
        raise HTTPException(status_code=401, detail="Signature invalide")
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        return {"ok": False, "error": "payload illisible"}

    sender, text, from_me, chat_id = wa.parse_incoming(payload)
    if not text:
        return {"ok": True, "ignored": True}
    # Sécurité : on n'accepte le pilotage QUE depuis ton self-chat (conversation
    # avec toi-même). Ça empêche tout déclenchement accidentel si tu écris une
    # commande (« stop », « go »…) à un autre contact, et bloque les inconnus.
    if not wa.is_owner(chat_id):
        return {"ok": True, "ignored": True}

    # handle_wa_command renvoie None si ce n'est pas une commande connue → on
    # n'envoie alors RIEN (les notifs/réponses du bot ne re-déclenchent pas d'action).
    reply = await handle_wa_command(text)
    if reply:
        wa.send_message(reply, chat_id=chat_id)
    return {"ok": True}


# ── Page principale ───────────────────────────────────────────────────────────
_INDEX_PATH = Path(__file__).parent / "static" / "index.html"
# Lecture UNIQUE au démarrage (mise en cache) → on ne relit pas le fichier à
# chaque requête. Plus rapide et plus robuste (évite un échec d'accès récurrent).
try:
    _INDEX_HTML = _INDEX_PATH.read_text()
except Exception as _e:  # noqa: BLE001
    _INDEX_HTML = None
    logging.getLogger(__name__).warning(f"⚠️ index.html non lu au démarrage : {_e}")


@app.get("/", response_class=HTMLResponse)
async def index():
    global _INDEX_HTML
    if _INDEX_HTML is None:
        # Tentative de relecture (au cas où l'accès était temporairement bloqué).
        try:
            _INDEX_HTML = _INDEX_PATH.read_text()
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(
                "<h2>Interface indisponible</h2>"
                "<p>Le serveur n'arrive pas à lire <code>static/index.html</code> "
                "(permission macOS sur le Bureau). Lance le serveur depuis ton "
                "propre Terminal : <code>cd ~/Desktop/linkedin-bot &amp;&amp; python3 server.py</code></p>",
                status_code=503,
            )
    return _INDEX_HTML


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
