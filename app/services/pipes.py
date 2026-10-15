"""Труби за розкладом (K4): архів сам приходить брифінгом.

Труба — файл ``pipes/<name>.md``: frontmatter (розклад, read-функції, канал,
бюджет) + тіло-промпт. Планувальник — daemon-потік у ``app.py`` (лише під
``__main__`` і з ``PIPES_ENABLED``), пропущений запуск доганяється один раз.

Свідомі межі:
- Функції даних — закритий реєстр ``TOOLS`` (лише читання). Довільних
  MCP-тулзів труба не кличе: ключ поза реєстром → труба не вантажиться.
- Cron — власний 5-польовий парсер (``*``, числа, списки, діапазони, крок),
  без croniter/apscheduler. Час — локальний час машини.
- Переказ робить Claude у межах ``budget_usd`` (оцінка ДО виклику). Нема
  ключа, бюджет або помилка → простий список із позначкою причини.
- Кожен запуск — рядок у ``pipe_runs`` (міграція v50); збій не вбиває потік.

CLI:
    python -m app.services.pipes list            # труби, розклад, наступний запуск
    python -m app.services.pipes run <name>      # пробний запуск, текст у stdout
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from app.services import pipes_privacy

logger = logging.getLogger(__name__)

PIPES_DIR = Path(__file__).resolve().parents[2] / "pipes"
PLAIN_MARK = "⚠️ Простий список"
DEFAULT_MAX_TOKENS = 1500


class PipeError(ValueError):
    """Файл труби невалідний — труба не вантажиться."""


# ----------------------------------------------------------------- cron

_FIELDS = (("хвилина", 0, 59), ("година", 0, 23), ("день", 1, 31),
           ("місяць", 1, 12), ("день тижня", 0, 7))


def _parse_field(spec: str, name: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise PipeError(f"cron: поле «{name}»: невалідний крок «{step_s}»")
            step = int(step_s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            if not (a_s.isdigit() and b_s.isdigit()):
                raise PipeError(f"cron: поле «{name}»: невалідний діапазон «{part}»")
            a, b = int(a_s), int(b_s)
        elif part.isdigit():
            a = b = int(part)
        else:
            raise PipeError(f"cron: поле «{name}»: невалідне значення «{part}»")
        if a < lo or b > hi or a > b:
            raise PipeError(f"cron: поле «{name}»: «{part}» поза межами {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


@dataclass(frozen=True)
class Cron:
    """5-польовий cron. День тижня: 0 і 7 — неділя, 1 — понеділок."""
    expr: str
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset          # 0=неділя … 6=субота
    dom_any: bool
    dow_any: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        parts = (expr or "").split()
        if len(parts) != 5:
            raise PipeError(f"cron: потрібно 5 полів, отримано {len(parts)}: «{expr}»")
        sets = [_parse_field(p, *f) for p, f in zip(parts, _FIELDS)]
        dow = frozenset(d % 7 for d in sets[4])
        return cls(expr, frozenset(sets[0]), frozenset(sets[1]), frozenset(sets[2]),
                   frozenset(sets[3]), dow, parts[2] == "*", parts[4] == "*")

    def _day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        # Класичне правило cron: обидва поля обмежені → достатньо одного.
        if self.dom_any or self.dow_any:
            return dom and dow
        return dom or dow

    def prev(self, now: datetime) -> Optional[datetime]:
        """Останній момент розкладу ≤ now (у межах року), інакше None."""
        now = now.replace(second=0, microsecond=0)
        for back in range(0, 367):
            d = (now - timedelta(days=back)).date()
            if not self._day_matches(d):
                continue
            for h in sorted(self.hours, reverse=True):
                if back == 0 and h > now.hour:
                    continue
                for m in sorted(self.minutes, reverse=True):
                    t = datetime(d.year, d.month, d.day, h, m)
                    if t <= now:
                        return t
        return None

    def next(self, now: datetime) -> Optional[datetime]:
        """Перший момент розкладу > now (у межах року), інакше None."""
        now = now.replace(second=0, microsecond=0)
        for fwd in range(0, 367):
            d = (now + timedelta(days=fwd)).date()
            if not self._day_matches(d):
                continue
            for h in sorted(self.hours):
                if fwd == 0 and h < now.hour:
                    continue
                for m in sorted(self.minutes):
                    t = datetime(d.year, d.month, d.day, h, m)
                    if t > now:
                        return t
        return None


# ------------------------------------------------------- реєстр функцій

def _weekly_digest(db_path: str, now: datetime) -> dict:
    from app.services import commitments
    return commitments.weekly_digest(db_path, today=now.date())


def _dropped_commitments(db_path: str, now: datetime) -> dict:
    from app.services import commitments
    return {"dropped": commitments.dropped_commitments(db_path, today=now.date())}


def _open_questions(db_path: str, now: datetime) -> dict:
    from app.services import tg_questions
    return tg_questions.open_questions(db_path, today=now)


#: Закритий реєстр read-функцій. Нова функція — лише сюди, лише читання.
TOOLS: dict[str, Callable[[str, datetime], dict]] = {
    "weekly_digest": _weekly_digest,
    "dropped_commitments": _dropped_commitments,
    "open_questions": _open_questions,
}


# ----------------------------------------------------------- файл труби

@dataclass
class Pipe:
    name: str
    schedule: Cron
    tools: list[str]
    channel: str
    budget_usd: float
    skip_if_empty: bool
    prompt: str
    path: Optional[Path] = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    extra: dict = field(default_factory=dict)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise PipeError("нема frontmatter (файл має починатись рядком ---)")
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:]).strip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise PipeError(f"frontmatter: рядок без «:» — «{line.strip()}»")
        k, v = line.split(":", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        meta[k.strip()] = v
    raise PipeError("frontmatter не закритий рядком ---")


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "так", "да")


def parse_pipe(text: str, name: str, path: Optional[Path] = None) -> Pipe:
    meta, body = _split_frontmatter(text)
    for key in ("schedule", "tools", "channel"):
        if not meta.get(key):
            raise PipeError(f"нема обовʼязкового поля «{key}»")
    tools = [t.strip() for t in meta["tools"].strip("[]").split(",") if t.strip()]
    unknown = [t for t in tools if t not in TOOLS]
    if unknown:
        raise PipeError(f"функції поза реєстром: {unknown} (дозволені: {sorted(TOOLS)})")
    if not tools:
        raise PipeError("порожній список tools")
    if not body:
        raise PipeError("порожній промпт (тіло файлу)")
    try:
        budget = float(meta.get("budget_usd", "0") or 0)
        max_tokens = int(meta.get("max_tokens", DEFAULT_MAX_TOKENS))
    except ValueError as e:
        raise PipeError(f"budget_usd/max_tokens не число: {e}") from e
    known = {"schedule", "tools", "channel", "budget_usd", "skip_if_empty", "max_tokens"}
    return Pipe(name=name, schedule=Cron.parse(meta["schedule"]), tools=tools,
                channel=meta["channel"], budget_usd=budget,
                skip_if_empty=_bool(meta.get("skip_if_empty", "false")),
                prompt=body, path=path, max_tokens=max_tokens,
                extra={k: v for k, v in meta.items() if k not in known})


def load_pipes(pipes_dir: Optional[Path] = None) -> list[Pipe]:
    """Усі валідні труби з теки. Невалідна — пропуск із причиною в лозі."""
    d = Path(pipes_dir or PIPES_DIR)
    out: list[Pipe] = []
    if not d.is_dir():
        logger.info("Труби: теки %s нема — труб нема", d)
        return out
    for p in sorted(d.glob("*.md")):
        try:
            out.append(parse_pipe(p.read_text(encoding="utf-8"), p.stem, p))
        except (PipeError, OSError) as e:
            logger.warning("Труба %s не завантажена: %s", p.name, e)
    return out


# ------------------------------------------------------------- історія

def _ts(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Коміт і ЗАКРИТТЯ: `with sqlite3.connect()` лише комітить, зʼєднання висить до GC."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def list_runs(db_path: str, *, pipe: Optional[str] = None, limit: int = 20) -> list[dict]:
    q = "SELECT * FROM pipe_runs"
    args: list[Any] = []
    if pipe:
        q += " WHERE pipe = ?"
        args.append(pipe)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


#: Запуск, що висить у `running` довше — процес помер посеред нього.
STALE_RUNNING = timedelta(minutes=30)


def _next_attempt(db_path: str, pipe: str, due: datetime, now: datetime, *,
                  retry_minutes: int, retry_max: int) -> Optional[int]:
    """Номер спроби, яку треба зробити для моменту ``due`` зараз, або None.

    Момент закритий, якщо після нього є ``ok``/``skipped``/``blocked`` або
    ``retry_max`` помилок. ``error`` і завислий ``running`` — це НЕ запуск, що
    відбувся: брифінг тижня не має пропадати через один збій о 9:00. Ручний
    ``pipes run`` (у stdout, не в бот) моменту не закриває.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, started_at, finished_at FROM pipe_runs WHERE pipe = ? "
            "AND trigger != 'manual' AND (scheduled_for >= ? OR started_at >= ?)",
            (pipe, _ts(due), _ts(due))).fetchall()
    errors, last_error = 0, None
    for r in rows:
        if r["status"] in ("ok", "skipped", "blocked"):
            return None
        started = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
        if r["status"] == "running" and now - started < STALE_RUNNING:
            return None                       # іде просто зараз
        errors += 1
        t = (datetime.strptime(r["finished_at"], "%Y-%m-%d %H:%M:%S")
             if r["finished_at"] else started)
        last_error = max(last_error or t, t)
    if errors >= retry_max:
        return None
    if last_error and now - last_error < timedelta(minutes=retry_minutes):
        return None
    return errors + 1


# --------------------------------------------------------------- рендер

def is_empty(data: dict) -> bool:
    """Порожньо = у даних усіх функцій нема жодного непорожнього списку."""
    for result in data.values():
        if not isinstance(result, dict):
            continue
        for v in result.values():
            if isinstance(v, list) and v:
                return False
    return True


_TOOL_LABEL = {"weekly_digest": "Звід", "dropped_commitments": "Забуті обіцянки",
               "open_questions": "Питання з Telegram"}


def not_collected(data: dict) -> list[str]:
    """Рядки «не зібрано» для функцій, що повернули `reason` (дані не вдалося зібрати).

    Порожній список із `reason` — це НЕ «нічого нема»: напр. `open_questions` без
    відомого власника Telegram віддає [] і пояснення. Тиша тут не відрізнялась би від
    «питань нема», тому рядок додає код, а не модель."""
    out = []
    for tool, res in data.items():
        if isinstance(res, dict) and res.get("reason"):
            out.append(f"⚠️ {_TOOL_LABEL.get(tool, tool)} не зібрано: {res['reason']}")
    return out


def _item_line(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    text = (item.get("task") or item.get("question") or item.get("topic")
            or item.get("name") or item.get("title") or "")
    if not text:
        return json.dumps(item, ensure_ascii=False, default=str)[:200]
    tail = [str(x) for x in (item.get("owner"), item.get("due_date") or item.get("due_raw"),
                             item.get("chat") or item.get("meeting")) if x]
    return f"{text}" + (f" ({'; '.join(tail)})" if tail else "")


def render_plain(pipe: Pipe, data: dict, reason: str) -> str:
    lines = [f"{PLAIN_MARK} ({reason}) · {pipe.name}"]
    for tool, result in data.items():
        if not isinstance(result, dict):
            continue
        for key, v in result.items():
            if not isinstance(v, list) or not v:
                continue
            lines.append("")
            lines.append(f"{tool} · {key} ({len(v)}):")
            lines.extend(f"• {_item_line(it)}" for it in v)
    if len(lines) == 1:
        lines.append("Даних нема.")
    return "\n".join(lines)


_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            import anthropic  # lazy
            _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        return _client


_SYSTEM = (
    "Ти переказуєш дані з особистого архіву дзвінків і чатів власнику у Telegram. "
    "Пиши українською, коротко, без вступів. Використовуй ЛИШЕ факти з JSON-даних: "
    "не додавай висновків, оцінок і подій, яких там нема. Імена, дати й назви — дослівно."
)


def _model() -> str:
    from app.core import settings as _settings
    return _settings.env("PIPES_MODEL")


def render_claude(pipe: Pipe, data: dict) -> tuple[str, float]:
    """Переказ Claude. Повертає (текст, вартість $). Кидає _PlainFallback з причиною."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise _PlainFallback("нема ANTHROPIC_API_KEY")
    from app.services.pricing import estimate_cost
    model = _model()
    payload = json.dumps(data, ensure_ascii=False, default=str)
    user = f"{pipe.prompt}\n\nДані (JSON):\n{payload}"
    # Груба оцінка вхідних токенів із запасом: кирилиця ~2 символи на токен.
    est = estimate_cost(model, (len(user) + len(_SYSTEM)) // 2, pipe.max_tokens)
    if est > pipe.budget_usd:
        raise _PlainFallback(f"бюджет: оцінка ${est:.4f} > ${pipe.budget_usd:.4f}")
    try:
        msg = _get_client().messages.create(
            model=model, max_tokens=pipe.max_tokens, system=_SYSTEM,
            messages=[{"role": "user", "content": user}])
    except Exception as e:
        logger.warning("Труба %s: Claude не відповів (%s)", pipe.name, type(e).__name__,
                       exc_info=True)
        raise _PlainFallback(f"помилка Claude: {type(e).__name__}") from e
    text = "".join(getattr(b, "text", "") for b in msg.content).strip()
    if not text:
        raise _PlainFallback("Claude повернув порожню відповідь")
    usage = getattr(msg, "usage", None)
    cost = estimate_cost(model, getattr(usage, "input_tokens", 0) or 0,
                         getattr(usage, "output_tokens", 0) or 0,
                         getattr(usage, "cache_read_input_tokens", 0) or 0)
    return text, cost


class _PlainFallback(Exception):
    """Переказ неможливий — іде простий список; аргумент — причина для людини."""


# ---------------------------------------------------------------- запуск

def run_pipe(pipe: Pipe, *, db_path: str, deliver: Callable[[str], None],
             now: Optional[datetime] = None, trigger: str = "manual",
             scheduled_for: Optional[datetime] = None,
             attempt: int = 1, max_attempts: int = 1, retry_minutes: int = 0) -> dict:
    """Один запуск труби: дані → рендер → deliver → рядок у pipe_runs.

    Ніколи не кидає: збій функції даних чи доставки пишеться як status='error'.
    Збій ДО доставки → коротке повідомлення в той самий канал (лише
    назва труби і клас помилки, без даних). Збій самої доставки — лише історія й
    лог: слати повідомлення про збій каналу тим самим каналом нема сенсу.
    ``blocked`` (фільтр приватного): брифінг не йде, але йде повідомлення про це —
    без даних і без самого стоп-слова (тиша читалась би як «брифінгу не буде взагалі»).
    З повторами (``max_attempts`` > 1) повідомлення йде на першій помилці і на
    останній спробі, між ними — тиша.
    """
    now = now or datetime.now()
    t0 = datetime.now()                   # тривалість — по справжньому годиннику
    with _connect(db_path) as conn:
        run_id = conn.execute(
            "INSERT INTO pipe_runs (pipe, trigger, scheduled_for, started_at, status) "
            "VALUES (?, ?, ?, ?, 'running')",
            (pipe.name, trigger, _ts(scheduled_for), _ts(now))).lastrowid
    res: dict[str, Any] = {"id": run_id, "pipe": pipe.name, "trigger": trigger,
                           "status": "error", "rendered_by": None, "cost_usd": 0.0,
                           "error": None, "chars": 0}
    delivering = False
    try:
        raw = {t: TOOLS[t](db_path, now) for t in pipe.tools}
        # Приватне викидається ДО рендеру: ні Claude, ні простий список його не бачать.
        data, hidden = pipes_privacy.filter_data(raw, db_path=db_path)
        missing = not_collected(data)
        if pipe.skip_if_empty and is_empty(data) and not hidden and not missing:
            res["status"] = "skipped"
            res["error"] = "порожні дані (skip_if_empty)"
        else:
            try:
                text, cost = render_claude(pipe, data)
                res.update(rendered_by="claude", cost_usd=cost)
            except _PlainFallback as fb:
                text = render_plain(pipe, data, str(fb))
                res.update(rendered_by="plain", error=str(fb))
            if hidden:
                text = f"{text}\n\n{pipes_privacy.hidden_line(hidden)}"
            if missing:
                text = "\n".join(missing) + f"\n\n{text}"
                note = "не зібрано: " + ", ".join(t for t in pipe.tools
                                                  if isinstance(data.get(t), dict)
                                                  and data[t].get("reason"))
                res["error"] = f"{res['error']}; {note}" if res["error"] else note
            text, leaked = pipes_privacy.scrub_text(text)
            if leaked:
                # Слова не називаємо навіть в історії — вона читається через MCP.
                res.update(status="blocked",
                           error=f"стоп-слово в готовому тексті ({len(leaked)}) — не надіслано")
            else:
                delivering = True
                deliver(text)
                res.update(status="ok", chars=len(text))
    except pipes_privacy.PrivacyError as e:
        logger.warning("Труба %s: приватність не перевірено (%s) — не надсилаю", pipe.name, e)
        what = "private.txt не читається" if str(e).startswith("private.txt") else "напрямки"
        res.update(status="blocked", error=f"приватність не перевірено ({what})")
    except Exception as e:
        logger.warning("Труба %s: запуск зламався (%s)", pipe.name, e, exc_info=True)
        res.update(status="error", error=f"{type(e).__name__}: {e}"[:500])
    if (res["status"] == "error" and not delivering
            and (attempt == 1 or attempt >= max_attempts)):
        _notify_failure(pipe, res, deliver, attempt=attempt, max_attempts=max_attempts,
                        retry_minutes=retry_minutes)
    elif res["status"] == "blocked":
        _notify_blocked(pipe, res, deliver)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE pipe_runs SET finished_at=?, status=?, rendered_by=?, cost_usd=?, "
            "error=?, chars=? WHERE id=?",
            (_ts(now + (datetime.now() - t0)), res["status"], res["rendered_by"], res["cost_usd"],
             res["error"], res["chars"], run_id))
    logger.info("Труба %s (%s): %s, %s, %s симв.", pipe.name, trigger, res["status"],
                res["rendered_by"] or "-", res["chars"])
    return res


_BLOCKED_REASONS = {
    "стоп-слово": "у готовому тексті лишилось слово зі стоп-листа",
    "приватність не перевірено (private.txt": "файл стоп-слів private.txt не читається",
    "приватність не перевірено (напрямки": "не вдалося перевірити приватні напрямки (збій БД)",
}


def _notify_blocked(pipe: Pipe, res: dict, deliver: Callable[[str], None]) -> None:
    """Фільтр зупинив брифінг: власник має знати, що його не буде. Без даних і без самого слова."""
    err = res["error"] or ""
    reason = next((v for k, v in _BLOCKED_REASONS.items() if err.startswith(k)), "фільтр приватного")
    try:
        deliver(f"🔒 Брифінг «{pipe.name}» не надіслано: його зупинив фільтр приватного — {reason}. "
                f"Подивись у Recall; деталі — у list_pipe_runs.")
    except Exception as e:  # noqa: BLE001 — історія вже має blocked
        logger.warning("Труба %s: повідомлення про блокування не надіслано (%s)",
                       pipe.name, type(e).__name__)


def _notify_failure(pipe: Pipe, res: dict, deliver: Callable[[str], None], *,
                    attempt: int = 1, max_attempts: int = 1, retry_minutes: int = 0) -> None:
    reason = (res["error"] or "").split(":", 1)[0]
    if max_attempts <= 1:
        tail = ""
    elif attempt < max_attempts:
        tail = f" Повторю через {retry_minutes} хв (спроба {attempt} з {max_attempts})."
    else:
        tail = f" Спроби вичерпано ({max_attempts}), до наступного розкладу не пробую."
    try:
        deliver(f"⚠️ Труба «{pipe.name}» впала: {reason}.{tail} Деталі — у list_pipe_runs.")
    except Exception as e:  # noqa: BLE001 — канал теж лежить; історія вже має збій
        logger.warning("Труба %s: повідомлення про збій не надіслано (%s)",
                       pipe.name, type(e).__name__)


# ---------------------------------------------------------- планувальник

class PipeScheduler:
    """Daemon-потік: раз на ``tick`` секунд запускає труби, чий момент настав.

    Перший прохід (при старті) — догін: для кожної труби останній момент
    розкладу ≤ зараз, і якщо його не закрито — рівно один запуск ``catchup``,
    скільки б моментів не було пропущено. Збій не закриває момент: повтор
    (``retry``) через ``PIPES_RETRY_MINUTES``, до ``PIPES_RETRY_MAX`` спроб.
    """

    def __init__(self, pipes: list[Pipe], *, db_path: str,
                 deliver: Callable[[str], None], tick: float = 30.0,
                 clock: Callable[[], datetime] = datetime.now,
                 retry_minutes: Optional[int] = None, retry_max: Optional[int] = None):
        self.pipes = pipes
        self.db_path = db_path
        self.deliver = deliver
        self.tick = tick
        self.clock = clock
        from app.core import settings as _settings
        self.retry_minutes = (retry_minutes if retry_minutes is not None
                              else _settings.env_int("PIPES_RETRY_MINUTES"))
        self.retry_max = max(1, retry_max if retry_max is not None
                             else _settings.env_int("PIPES_RETRY_MAX"))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def step(self, trigger: str = "schedule") -> list[dict]:
        now = self.clock()
        out = []
        for pipe in self.pipes:
            try:
                due = pipe.schedule.prev(now)
                if due is None:
                    continue
                attempt = _next_attempt(self.db_path, pipe.name, due, now,
                                        retry_minutes=self.retry_minutes,
                                        retry_max=self.retry_max)
                if attempt is None:
                    continue
                out.append(run_pipe(pipe, db_path=self.db_path, deliver=self.deliver,
                                    now=now, trigger="retry" if attempt > 1 else trigger,
                                    scheduled_for=due, attempt=attempt,
                                    max_attempts=self.retry_max,
                                    retry_minutes=self.retry_minutes))
            except Exception as e:
                logger.warning("Планувальник труб: %s — %s", pipe.name, e, exc_info=True)
        return out

    def _loop(self):
        self.step("catchup")
        while not self._stop.wait(self.tick):
            self.step("schedule")

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="pipes-scheduler",
                                        daemon=True)
        self._thread.start()
        logger.info("Труби: планувальник запущено (%s)",
                    ", ".join(f"{p.name} «{p.schedule.expr}»" for p in self.pipes) or "труб нема")

    def stop(self):
        self._stop.set()


def _cli(argv: list[str]) -> int:
    import argparse
    from config import Config
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    ap = argparse.ArgumentParser(prog="python -m app.services.pipes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("name")
    a = ap.parse_args(argv)
    pipes = load_pipes()
    if a.cmd == "list":
        now = datetime.now()
        for p in pipes:
            print(f"{p.name}: «{p.schedule.expr}» → наступний {p.schedule.next(now)}; "
                  f"tools={p.tools}; budget=${p.budget_usd}")
        return 0
    pipe = next((p for p in pipes if p.name == a.name), None)
    if pipe is None:
        print(f"нема труби «{a.name}»")
        return 1
    res = run_pipe(pipe, db_path=Config.DATABASE, deliver=print, trigger="manual")
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["status"] in ("ok", "skipped") else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
