"""One-time migration: remove false tg_link values in personal chats (test-log-isolation-02).

**Problem:** before the `_chat_link` fix in `telegram_listener.py`, the link for a
personal chat (`tg_chat_id > 0`) was built as `https://t.me/<username>/<msg_id>`
from the counterpart's username. That format denotes a post in a public group/channel
(core.telegram.org/api/links); a link to a specific message does not exist
for private conversations at all. In the live DB records were marked this way
(example: chat 250264900 → https://t.me/adamharber/515482 actually opens
the person's profile, the message number means nothing).

**What the pass does:** clears only the `tg_link` field for rows with `tg_chat_id > 0`;
rows are not deleted, `tg_link` for supergroups/channels (`tg_chat_id < 0`) is not
touched. Idempotent: a second run always sees 0 candidates.

CLI:
    python -m app.services.telegram_link_repair clear-bogus-links --dry-run
    python -m app.services.telegram_link_repair clear-bogus-links
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Optional

from app.db.connection import get_db_connection

logger = logging.getLogger(__name__)


def clear_bogus_private_links(db_path: str, *, dry_run: bool = False) -> dict:
    """Remove false `tg_link` values in personal chats (`tg_chat_id > 0`)."""
    sql = (
        "SELECT id FROM transcriptions "
        "WHERE tg_chat_id > 0 AND tg_link IS NOT NULL AND tg_link <> ''"
    )
    with get_db_connection(db_path) as conn:
        ids = [r["id"] for r in conn.execute(sql).fetchall()]
        stats = {"dry_run": dry_run, "matched": len(ids), "cleared": 0}
        if ids and not dry_run:
            conn.executemany(
                "UPDATE transcriptions SET tg_link = NULL WHERE id = ?",
                [(i,) for i in ids])
            conn.commit()
            stats["cleared"] = len(ids)
    logger.info("clear_bogus_private_links: matched=%(matched)s cleared=%(cleared)s "
                "dry_run=%(dry_run)s", stats)
    return stats


# ============================================================
# CLI
# ============================================================

def _cmd(args) -> int:
    if args.command == "clear-bogus-links":
        res = clear_bogus_private_links(args.db, dry_run=args.dry_run)
    else:  # pragma: no cover
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: Optional[list] = None) -> int:
    from config import Config
    default_db = str(Config.BASE_DIR / Config.DATABASE)

    p = argparse.ArgumentParser(
        prog="telegram_link_repair",
        description="Remove false tg_link values invented for personal chats.")
    p.add_argument("--db", default=default_db)
    # --dry-run is accepted both before and after the subcommand. argparse trap: if
    # both have the same dest, _SubParsersAction overwrites the parent
    # namespace with its own; the subparser's default False clobbers the True set
    # before the subcommand. So the top-level flag gets its own dest, and the final
    # value is the OR of both; word order does not affect the result.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true",
                   help="Write nothing to the DB")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Write nothing to the DB")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("clear-bogus-links", parents=[common],
                   help="tg_link → NULL for tg_chat_id > 0")

    args = p.parse_args(argv)
    args.dry_run = args.dry_run or args.dry_run_pre
    return _cmd(args)


if __name__ == "__main__":
    raise SystemExit(main())
