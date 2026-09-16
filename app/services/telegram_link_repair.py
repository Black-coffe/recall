"""Одноразова міграція: прибрати хибні tg_link в особистих чатах (test-log-isolation-02).

**Проблема:** до фіксу `_chat_link` у `telegram_listener.py` посилання для
особистого чату (`tg_chat_id > 0`) будувалось як `https://t.me/<username>/<msg_id>`
з юзернейма співрозмовника. Такий формат означає пост у публічній групі/каналі
(core.telegram.org/api/links) — для особистого листування посилання на конкретне
повідомлення не існує в принципі. У бойовій БД так позначено записів
(приклад: chat 250264900 → https://t.me/adamharber/515482 — насправді відкриває
профіль людини, номер повідомлення нічого не означає).

**Що робить прохід:** очищає лише поле `tg_link` для рядків з `tg_chat_id > 0` —
рядки не видаляються, `tg_link` для супергруп/каналів (`tg_chat_id < 0`) не
чіпається. Ідемпотентно: другий прогін завжди бачить 0 кандидатів.

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
    """Прибрати хибні `tg_link` у особистих чатах (`tg_chat_id > 0`)."""
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
        description="Прибрати хибні tg_link, вигадані для особистих чатів.")
    p.add_argument("--db", default=default_db)
    # --dry-run приймається і до, і після підкоманди. argparse-пастка: якщо
    # обидва мають однаковий dest, _SubParsersAction переписує batьківський
    # namespace своїм — дефолт False з підпарсера затирає True, виставлений
    # до підкоманди. Тому в top-level флаг йде в окремий dest, а фінальне
    # значення — OR обох; порядок слів на результат не впливає.
    p.add_argument("--dry-run", dest="dry_run_pre", action="store_true",
                   help="Нічого не писати в БД")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Нічого не писати в БД")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("clear-bogus-links", parents=[common],
                   help="tg_link → NULL для tg_chat_id > 0")

    args = p.parse_args(argv)
    args.dry_run = args.dry_run or args.dry_run_pre
    return _cmd(args)


if __name__ == "__main__":
    raise SystemExit(main())
