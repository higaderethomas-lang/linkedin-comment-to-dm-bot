#!/usr/bin/env python3
"""
LinkedIn Bot — Point d'entrée principal
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Ajoute le répertoire parent au path
sys.path.insert(0, str(Path(__file__).parent))

from core.bot import run_bot, login_and_save


def cmd_login(args):
    """Connexion LinkedIn et sauvegarde de session (manuelle dans le navigateur)."""
    print("\n🌐 Ouverture du navigateur LinkedIn...")
    print("👉 Connecte-toi manuellement dans la fenêtre qui s'ouvre.")
    print("   Une fois sur ton feed, la session sera sauvegardée automatiquement.\n")
    asyncio.run(login_and_save())
    print("✅ Session sauvegardée ! Tu n'as plus besoin de te reconnecter.")


def cmd_run(args):
    """Lance le bot sur un post."""
    if not args.post:
        print("❌ Manque l'URL du post. Usage: python bot.py run --post <URL> --doc <URL>")
        sys.exit(1)
    if not args.doc:
        print("❌ Manque l'URL du document. Usage: python bot.py run --post <URL> --doc <URL>")
        sys.exit(1)

    print(f"\n🚀 Démarrage du bot")
    print(f"📌 Post: {args.post}")
    print(f"📄 Doc:  {args.doc}")
    print(f"\n{'='*50}")
    print("Ctrl+C pour arrêter proprement\n")

    asyncio.run(run_bot(
        post_url=args.post,
        doc_url=args.doc,
        headless=not args.visible
    ))


def cmd_stats(args):
    """Affiche les statistiques."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("\n📊 STATISTIQUES DU BOT")
    print("=" * 50)

    # Total par statut
    rows = conn.execute("""
        SELECT status, COUNT(*) as count
        FROM contacts
        GROUP BY status
        ORDER BY count DESC
    """).fetchall()

    status_labels = {
        "dm_sent":         "📨 DM envoyé (pas encore confirmé)",
        "confirmed":       "✅ DM envoyé + commentaire confirmé",
        "pending_connect": "⏳ En attente de connexion (2ème/3ème)",
        "skipped":         "⏭️  Ignoré (degré inconnu)",
    }

    for row in rows:
        label = status_labels.get(row["status"], row["status"])
        print(f"  {label}: {row['count']}")

    # Comptes journaliers
    print("\n📅 AUJOURD'HUI")
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute("SELECT * FROM daily_counts WHERE date = ?", (today,)).fetchone()
    if row:
        print(f"  📨 DMs envoyés: {row['dms_sent']}/50")
        print(f"  💬 Commentaires: {row['comments']}/30")
    else:
        print("  Aucune activité aujourd'hui")

    # Derniers contacts traités
    print("\n🕒 DERNIERS CONTACTS")
    recents = conn.execute("""
        SELECT full_name, degree, status, dm_sent_at
        FROM contacts
        ORDER BY created_at DESC
        LIMIT 10
    """).fetchall()

    for r in recents:
        print(f"  {r['full_name']} ({r['degree']}) → {r['status']}")

    conn.close()


def cmd_reset(args):
    """Remet à zéro les compteurs journaliers."""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM daily_counts WHERE date = ?", (today,))
    conn.commit()
    conn.close()
    print("✅ Compteurs journaliers remis à zéro")


def main():
    parser = argparse.ArgumentParser(
        description="🤖 LinkedIn Comment-to-DM Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commandes:
  login              Se connecter à LinkedIn (à faire une seule fois)
  run                Lancer le bot sur un post
  stats              Voir les statistiques
  reset              Remettre les compteurs à zéro

Exemples:
  python bot.py login
  python bot.py run --post "https://linkedin.com/posts/..." --doc "https://drive.google.com/..."
  python bot.py run --post "https://..." --doc "https://..." --visible
  python bot.py stats
        """
    )

    subparsers = parser.add_subparsers(dest="command")

    # Login
    subparsers.add_parser("login", help="Connexion LinkedIn (une seule fois)")

    # Run
    run_parser = subparsers.add_parser("run", help="Lancer le bot")
    run_parser.add_argument("--post", help="URL du post LinkedIn à surveiller")
    run_parser.add_argument("--doc",  help="URL du document/playbook à envoyer")
    run_parser.add_argument("--visible", action="store_true", help="Afficher le navigateur (debug)")

    # Stats
    subparsers.add_parser("stats", help="Statistiques")

    # Reset
    subparsers.add_parser("reset", help="Remettre compteurs à zéro")

    args = parser.parse_args()

    if args.command == "login":
        cmd_login(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "reset":
        cmd_reset(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
