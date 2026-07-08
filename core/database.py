"""
Source de vérité SQLite pour le LinkedIn Bot.
Remplace processed_slugs (in-memory) par une DB persistante.
"""

import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent.parent
DB_PATH  = BASE_DIR / "data" / "contacts.db"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def init_db():
    """
    Crée les tables si elles n'existent pas.
    Migre l'ancienne table contacts (schéma v1) vers le nouveau schéma si nécessaire.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as con:
        # Vérifie si la table contacts existe déjà avec l'ancien schéma (v1)
        existing_cols = [
            r[1] for r in con.execute("PRAGMA table_info(contacts)").fetchall()
        ]
        is_old_schema = existing_cols and "id" not in existing_cols

        if is_old_schema:
            # Renomme l'ancienne table pour préserver les données
            con.execute("ALTER TABLE contacts RENAME TO contacts_v1")

        con.executescript("""
            CREATE TABLE IF NOT EXISTS contacts (
                id              TEXT PRIMARY KEY,
                job_id          TEXT NOT NULL,
                profile_slug    TEXT NOT NULL,
                full_name       TEXT NOT NULL,
                first_name      TEXT NOT NULL,
                profile_url     TEXT NOT NULL,
                degree          TEXT,
                status          TEXT DEFAULT 'pending_scan',
                thread_text     TEXT,
                comment_text    TEXT,
                dm_sent_at      TEXT,
                comment_sent_at TEXT,
                connect_sent_at TEXT,
                last_scan_at    TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_counts (
                date        TEXT PRIMARY KEY,
                dms_sent    INTEGER DEFAULT 0,
                comments    INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS scan_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id        TEXT NOT NULL,
                scanned_at    TEXT DEFAULT (datetime('now')),
                total_found   INTEGER DEFAULT 0,
                new_contacts  INTEGER DEFAULT 0
            );
        """)

        # Migration : ajoute relance_sent_at si la colonne n'existe pas encore
        cols = [r[1] for r in con.execute("PRAGMA table_info(contacts)").fetchall()]
        if "relance_sent_at" not in cols:
            con.execute("ALTER TABLE contacts ADD COLUMN relance_sent_at TEXT")
        # Migration : dm_doc_url = le lien réellement envoyé en DM (pour la dédup
        # cross-post PAR DOCUMENT — une personne peut recevoir des docs différents
        # sur des posts différents).
        if "dm_doc_url" not in cols:
            con.execute("ALTER TABLE contacts ADD COLUMN dm_doc_url TEXT")


def upsert_contact(
    job_id: str,
    profile_slug: str,
    full_name: str,
    first_name: str,
    profile_url: str,
    degree: Optional[str] = None,
    comment_text: Optional[str] = None,
    thread_text: Optional[str] = None,
) -> bool:
    """
    INSERT OR IGNORE sur l'id, puis UPDATE degree/comment_text/thread_text/last_scan_at.
    NE change PAS le status si déjà != 'pending_scan'.
    Retourne True si c'est un nouveau contact.
    """
    contact_id = f"{job_id}::{profile_slug}"
    now = datetime.now().isoformat(sep=" ", timespec="seconds")

    with _conn() as con:
        existing = con.execute(
            "SELECT id, status FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()
        is_new = existing is None

        con.execute(
            """INSERT OR IGNORE INTO contacts
               (id, job_id, profile_slug, full_name, first_name, profile_url,
                degree, comment_text, thread_text, last_scan_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, job_id, profile_slug, full_name, first_name,
             profile_url, degree, comment_text, thread_text, now),
        )

        if not is_new:
            current_status = existing["status"]
            if current_status == "pending_scan":
                con.execute(
                    """UPDATE contacts
                       SET degree=?, comment_text=?, thread_text=?,
                           last_scan_at=?, updated_at=?,
                           full_name=?, first_name=?, profile_url=?
                       WHERE id=?""",
                    (degree, comment_text, thread_text, now, now,
                     full_name, first_name, profile_url, contact_id),
                )
            else:
                # Statut avancé → met à jour degree, thread_text et last_scan_at uniquement
                con.execute(
                    """UPDATE contacts
                       SET degree=?, thread_text=?, last_scan_at=?, updated_at=?
                       WHERE id=?""",
                    (degree, thread_text, now, now, contact_id),
                )

    return is_new


def update_status(contact_id: str, status: str, **kwargs):
    """Met à jour le statut + timestamps optionnels."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    fields = {"status": status, "updated_at": now}

    # Timestamps connus
    ts_map = {
        "dm_sent_at":      "dm_sent_at",
        "comment_sent_at": "comment_sent_at",
        "connect_sent_at": "connect_sent_at",
        "thread_text":     "thread_text",
        "degree":          "degree",
        "dm_doc_url":      "dm_doc_url",
    }
    for k, col in ts_map.items():
        if k in kwargs:
            fields[col] = kwargs[k]

    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [contact_id]

    with _conn() as con:
        con.execute(f"UPDATE contacts SET {set_clause} WHERE id=?", values)


def update_contact_identity(
    contact_id: str,
    full_name: Optional[str] = None,
    first_name: Optional[str] = None,
    degree: Optional[str] = None,
):
    """Met à jour le nom et/ou le degré d'un contact (rattrapage profil).
    Ne touche qu'aux champs fournis (non vides). No-op si rien à mettre à jour."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    fields = {"updated_at": now}
    if full_name:
        fields["full_name"] = full_name
    if first_name:
        fields["first_name"] = first_name
    if degree:
        fields["degree"] = degree
    if len(fields) == 1:  # seulement updated_at → rien à faire
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [contact_id]
    with _conn() as con:
        con.execute(f"UPDATE contacts SET {set_clause} WHERE id=?", values)


def has_doc_been_sent_to_slug(profile_slug: str, doc_url: str) -> bool:
    """True si cette personne a déjà reçu CE MÊME lien (doc_url) en DM, sur
    n'importe quel post. Dédup cross-post PAR DOCUMENT : une personne peut
    légitimement recevoir des docs DIFFÉRENTS sur des posts différents — on ne
    bloque QUE si c'est exactement le même lien déjà envoyé.

    Match exact sur le doc_url tracé (dm_doc_url) ET preuve d'envoi (dm_sent_at).
    Si doc_url vide → on ne peut pas dédupliquer par doc → False."""
    if not profile_slug or not doc_url:
        return False
    url = doc_url.strip().lower().rstrip("/")
    with _conn() as con:
        row = con.execute(
            """SELECT 1 FROM contacts
               WHERE profile_slug=? AND dm_sent_at IS NOT NULL
                 AND dm_doc_url IS NOT NULL
                 AND lower(rtrim(dm_doc_url, '/')) = ?
               LIMIT 1""",
            (profile_slug, url),
        ).fetchone()
    return row is not None


def get_contacts_to_process(job_id: str) -> list:
    """Retourne les contacts actionnables pour ce job, triés par statut."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM contacts
               WHERE job_id=? AND status IN ('a_traiter','a_connecter','a_convertir')
               ORDER BY
                 CASE status
                   WHEN 'a_traiter'   THEN 1
                   WHEN 'a_convertir' THEN 2
                   WHEN 'a_connecter' THEN 3
                 END""",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_contacts_pending_comment(job_id: str) -> list:
    """Contacts 'traite' qui ont bien reçu un DM (dm_sent_at) mais dont le
    commentaire de confirmation n'a JAMAIS été posté (comment_sent_at NULL).
    → à rattraper au prochain passage pour une visibilité claire."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM contacts
               WHERE job_id=? AND status='traite'
                 AND dm_sent_at IS NOT NULL AND comment_sent_at IS NULL
               ORDER BY dm_sent_at ASC""",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_connection_checks(job_id: str) -> list:
    """Contacts en attente de connexion pour ce job."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM contacts WHERE job_id=? AND status='en_attente'",
            (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_scan_time(job_id: str) -> Optional[datetime]:
    """Retourne la date du dernier scan pour ce job, ou None."""
    with _conn() as con:
        row = con.execute(
            "SELECT MAX(scanned_at) as last FROM scan_history WHERE job_id=?",
            (job_id,),
        ).fetchone()
    if row and row["last"]:
        try:
            return datetime.fromisoformat(row["last"])
        except ValueError:
            return None
    return None


def check_daily_limits() -> tuple[int, int]:
    """Retourne (dms_today, comments_today) pour la date du jour."""
    today = date.today().isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT dms_sent, comments FROM daily_counts WHERE date=?", (today,)
        ).fetchone()
    if row:
        return row["dms_sent"], row["comments"]
    return 0, 0


def check_weekly_limits() -> tuple[int, int]:
    """Retourne (dms_7d, comments_7d) — sliding window 7 jours."""
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=6)).isoformat()
    with _conn() as con:
        row = con.execute(
            """SELECT COALESCE(SUM(dms_sent),0) as dms,
                      COALESCE(SUM(comments),0) as cmt
               FROM daily_counts WHERE date >= ?""", (cutoff,)
        ).fetchone()
    return (row["dms"], row["cmt"]) if row else (0, 0)


def increment_daily(type_: str):
    """Incrémente 'dms_sent' ou 'comments' pour aujourd'hui."""
    today = date.today().isoformat()
    col = "dms_sent" if type_ == "dms_sent" else "comments"
    with _conn() as con:
        con.execute(
            f"""INSERT INTO daily_counts (date, {col}) VALUES (?, 1)
                ON CONFLICT(date) DO UPDATE SET {col}={col}+1""",
            (today,),
        )


def get_all_contacts(job_id: Optional[str] = None) -> list:
    """Tous les contacts, optionnellement filtrés par job_id."""
    with _conn() as con:
        if job_id:
            rows = con.execute(
                "SELECT * FROM contacts WHERE job_id=? ORDER BY created_at DESC",
                (job_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM contacts ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Retourne un dict avec compteurs par statut."""
    with _conn() as con:
        rows = con.execute(
            "SELECT status, COUNT(*) as cnt FROM contacts GROUP BY status"
        ).fetchall()
    counts = {r["status"]: r["cnt"] for r in rows}

    dms_today, comments_today = check_daily_limits()
    return {
        "by_status":      counts,
        "total":          sum(counts.values()),
        "dms_today":      dms_today,
        "comments_today": comments_today,
    }


def reprocess_contact(contact_id: str):
    """Remet un contact en 'a_traiter'."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    with _conn() as con:
        con.execute(
            "UPDATE contacts SET status='a_traiter', updated_at=? WHERE id=?",
            (now, contact_id),
        )


def get_contacts_for_relance(job_id: str, delay_days: int) -> list:
    """
    Contacts à relancer pour ce job :
    - status = 'traite' (DM déjà envoyé + commentaire posté)
    - dm_sent_at renseigné et antérieur à (maintenant - delay_days)
    - relance pas encore envoyée (relance_sent_at IS NULL)
    Triés du plus ancien DM au plus récent.
    """
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=int(delay_days))).isoformat(
        sep=" ", timespec="seconds"
    )
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM contacts
               WHERE job_id=? AND status='traite'
                 AND dm_sent_at IS NOT NULL
                 AND relance_sent_at IS NULL
                 AND dm_sent_at <= ?
               ORDER BY dm_sent_at ASC""",
            (job_id, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_relance_sent(contact_id: str):
    """Marque qu'une relance a été envoyée à ce contact (évite les doublons)."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    with _conn() as con:
        con.execute(
            "UPDATE contacts SET relance_sent_at=?, updated_at=? WHERE id=?",
            (now, now, contact_id),
        )


def record_scan(job_id: str, total_found: int, new_contacts: int):
    """Enregistre un scan dans scan_history."""
    with _conn() as con:
        con.execute(
            """INSERT INTO scan_history (job_id, total_found, new_contacts)
               VALUES (?, ?, ?)""",
            (job_id, total_found, new_contacts),
        )
