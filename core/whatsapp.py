"""
Intégration WhatsApp via OpenWA (https://github.com/rmyndharis/OpenWA).

Deux usages :
  1. Envoyer les logs importants du bot sur ton WhatsApp (handler de logs filtré).
  2. Recevoir des commandes WhatsApp (start/stop/stats…) via le webhook OpenWA.

Tout est DÉSACTIVÉ tant que `data/whatsapp_config.json` n'est pas rempli
(enabled=true + api_key + session_id + owner_chat_id). Si OpenWA est injoignable,
les échecs sont silencieux : le bot LinkedIn n'est jamais bloqué ni planté.

API OpenWA utilisée :
  POST {base_url}/api/sessions/{session_id}/messages/send-text
  Header : X-API-Key
  Body   : {"chatId": "33XXXXXXXXX@c.us", "text": "..."}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import re
import threading
from pathlib import Path

import httpx

log = logging.getLogger("whatsapp")

CONFIG_PATH = Path(__file__).parent.parent / "data" / "whatsapp_config.json"

DEFAULT_CONFIG = {
    "enabled":        False,            # passe à true une fois la session OpenWA prête
    "base_url":       "http://localhost:2785",
    "api_key":        "",               # clé API OpenWA (header X-API-Key)
    "session_id":     "",               # id de la session WhatsApp créée dans OpenWA
    "owner_chat_id":  "",               # TON numéro, format international : 33XXXXXXXXX@c.us
    "webhook_secret": "",               # secret HMAC configuré côté OpenWA (optionnel)
    "notify_events":  True,             # envoyer les logs importants sur WhatsApp
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception as e:  # config corrompue → on garde les défauts
            log.warning(f"Config WhatsApp illisible ({e}) — intégration désactivée")
    return cfg


_config = load_config()


def reload_config() -> dict:
    """Recharge la config depuis le disque (après édition du JSON)."""
    global _config
    _config = load_config()
    return _config


def is_enabled() -> bool:
    # api_key est optionnel (OpenWA en dev peut tourner sans clé)
    return bool(
        _config.get("enabled")
        and _config.get("session_id")
        and _config.get("owner_chat_id")
    )


# ── Envoi non-bloquant (thread worker + file) ────────────────────────────────
_send_queue: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=200)
_worker_started = False
_worker_lock = threading.Lock()


def _post_send(chat_id: str, text: str):
    cfg = _config
    url = f"{cfg['base_url'].rstrip('/')}/api/sessions/{cfg['session_id']}/messages/send-text"
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["X-API-Key"] = cfg["api_key"]
    with httpx.Client(timeout=15) as client:
        r = client.post(url, headers=headers, json={"chatId": chat_id, "text": text})
        r.raise_for_status()


def _worker():
    while True:
        chat_id, text = _send_queue.get()
        try:
            _post_send(chat_id, text)
        except Exception as e:
            log.debug(f"Échec envoi WhatsApp : {e}")
        finally:
            _send_queue.task_done()


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True, name="whatsapp-sender").start()
            _worker_started = True


def send_message(text: str, chat_id: str | None = None):
    """Envoie un message WhatsApp (non bloquant). No-op si l'intégration est OFF."""
    if not is_enabled():
        return
    chat_id = chat_id or _config.get("owner_chat_id")
    if not chat_id or not text:
        return
    _ensure_worker()
    try:
        _send_queue.put_nowait((chat_id, text))
    except queue.Full:
        pass  # on préfère perdre une notif que bloquer le bot


# ── Handler de logs : forward des lignes importantes vers WhatsApp ───────────
# On ne forwarde QUE l'essentiel pour éviter le spam.
_NOTIFY_KEYWORDS = (
    "🚀 Bot démarré",
    "⛔ Bot arrêté",
    "📨 DM envoyé",
    "✅ Réponse postée",
    "✅ Exécution terminée",
    "🔄 Reset démarrage",
    "🛑",            # garde-fous (mauvais destinataire / prénom)
    "🚫",            # redirect / CAPTCHA / session
    "CAPTCHA",
    "session expirée",
    "❌ ÉCHEC",
)
# Lignes bruyantes à NE jamais envoyer même si WARNING.
_NOTIFY_BLOCKLIST = (
    "Bouton Send introuvable",
)


class WhatsAppLogHandler(logging.Handler):
    """Pousse les logs importants vers WhatsApp (filtré, non bloquant)."""

    def emit(self, record: logging.LogRecord):
        try:
            if not is_enabled() or not _config.get("notify_events"):
                return
            msg = record.getMessage()
            if any(b in msg for b in _NOTIFY_BLOCKLIST):
                return
            important = record.levelno >= logging.ERROR or any(k in msg for k in _NOTIFY_KEYWORDS)
            if important:
                send_message(f"🤖 {msg}")
        except Exception:
            pass  # un handler de log ne doit jamais lever


# ── Sécurité webhook ─────────────────────────────────────────────────────────
def verify_signature(raw: bytes, headers: dict) -> bool:
    """Vérifie la signature HMAC si un secret est configuré ET qu'un header
    de signature reconnu est présent. Sinon True (le contrôle propriétaire
    via is_owner() reste la garde principale)."""
    secret = _config.get("webhook_secret")
    if not secret:
        return True
    headers_l = {k.lower(): v for k, v in (headers or {}).items()}
    sig = None
    for h in ("x-signature", "x-hub-signature-256", "x-webhook-signature", "x-openwa-signature"):
        if h in headers_l:
            sig = headers_l[h]
            break
    if not sig:
        return True
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    sig_clean = sig.split("=")[-1].strip()
    return hmac.compare_digest(digest, sig_clean)


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def is_owner(sender: str | None) -> bool:
    """Seul le numéro propriétaire peut piloter le bot."""
    own = _digits(_config.get("owner_chat_id"))
    snd = _digits(sender)
    if not own or not snd:
        return False
    return snd == own or snd.endswith(own) or own.endswith(snd)


# ── Parsing des messages entrants (payload OpenWA, forme tolérante) ──────────
def _deep_find(obj, keys: tuple, want_bool: bool = False):
    """Cherche récursivement la 1re valeur scalaire pour l'une des clés `keys`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (str, bool, int)):
                if want_bool and isinstance(v, bool):
                    return v
                if not want_bool and isinstance(v, str) and v.strip():
                    return v
        for v in obj.values():
            found = _deep_find(v, keys, want_bool)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find(item, keys, want_bool)
            if found is not None:
                return found
    return None


def parse_incoming(payload: dict):
    """Extrait (sender, text, from_me, chat_id) d'un webhook OpenWA, quelle que
    soit la forme exacte du payload (recherche récursive des champs courants).

    chat_id = la CONVERSATION (champ `chatId`). Pour un message à toi-même, c'est
    ton propre numéro ; pour un autre contact, c'est le sien. Sert à n'autoriser
    le pilotage que depuis ton self-chat."""
    sender = _deep_find(payload, ("from", "author", "sender", "remoteJid"))
    text = _deep_find(payload, ("body", "text", "message", "content", "caption"))
    chat_id = _deep_find(payload, ("chatId", "to", "remoteJid"))
    from_me = _deep_find(payload, ("fromMe",), want_bool=True) or False
    return sender, text, bool(from_me), chat_id
