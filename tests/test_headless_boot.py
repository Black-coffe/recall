"""Мірило роадмепу S3 (config-registry-profiles-04) як subprocess-тест.

`RECALL_PROFILE=headless FORCE_CPU=1 python app.py` мусить піднятись без
`pyaudiowpatch`/torch-GPU і віддати `/api/ready` 200 з `ready=true`. Тест
запускає РЕАЛЬНИЙ дочірній процес `app.py` (не `test_client()`, як
`test_endpoints_smoke.py`) — тільки так перевіряється сама boot-послідовність
(module-level код, `__main__`, `app.run()`), а не блуприпнти в ізоляції.

Маркер `slow`: дочірній процес імпортує torch/faster-whisper і піднімає
реальний HTTP-сервер — десятки секунд, не входить у `-m "not slow"`.

Ізоляція бойової БД: `cwd` дочірнього процесу — тимчасова директорія, а
`config.py::Config.DATABASE` — відносний шлях (`'whisper_history.db'`,
`'test_whisper_history.db'` під `APP_ENV=testing`), тож sqlite-файл
створюється у tmp_path, ніколи в корені проєкту. Сам env `DATABASE=<tmp>`
з акцептансу цієї історії НЕ читається жодним кодом (перевірено грепом —
`config.py` тримає хардкод, а `app/services/comments.py` читає той самий
env для непов'язаної фічі) — це задокументований розрив між формулюванням
мірила і кодом, обійдений через `cwd`+`APP_ENV=testing`, без правок app.py
(Non-goal історії — не латати застосунок).

Гейт Telegram (config-registry-fix-02): `config.py::TELEGRAM_SESSION` збирається
через `str(BASE_DIR / settings.env('TELEGRAM_SESSION'))` (`config.py:269`) —
реєстр дає ім'я, `BASE_DIR` дає АБСОЛЮТНИЙ шлях, тож `cwd=tmp_path` цей гейт
не закриває — на машині власника з реальними `TELEGRAM_API_ID`/`HASH` в `.env`
і встановленим telethon дочірній процес підняв би бойового Telegram-слухача
(окремий subprocess, живе TG-МТProto-з'єднання, пише в `telegram_media/` і
б'ється за `telegram.session`/порт 5051 із живим слухачем власника). Тому
`_spawn_headless` явно передає `TELEGRAM_SESSION=<tmp_path>/telegram` — файлу
`.session` там нема і не буде. `_maybe_launch_telegram_listener` (`app.py:1156`)
перевіряє `TELEGRAM_ENABLED` (ключі+telethon) ПЕРШИМ: немає ключів — гілка
`disabled`; є ключі (як на машині власника) — далі перевіряється файл сесії, і
його відсутність дає гілку `absent`. В обох випадках жоден Telegram-subprocess
не з'являється. Обидва тести асертять `checks.telegram in ("disabled", "absent")`
— явний доказ, а не сподівання. Прибирання (`_terminate`) не покладається на
`atexit` дочірнього процесу (на Windows `terminate()`/`TerminateProcess` його
не виконує) — термінує/вбиває через psutil усе дерево процесів дочірнього PID
за PID (ніколи за іменем образу — `memory/never-taskkill-by-image-name.md`), і
тест явно перевіряє, що жоден pid із цього дерева не пережив прибирання.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_PY = PROJECT_ROOT / "app.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_pyaudiowpatch_stub(tmp_path: Path) -> Path:
    """PYTHONPATH-заглушка: `import pyaudiowpatch` кидає ImportError.

    Незалежно від `sys.modules` дочірнього процесу (це окремий процес) —
    доводить, що headless-boot реально НЕ намагається імпортувати
    pyaudiowpatch, а не просто "у нас його нема встановленого".
    """
    stub_root = tmp_path / "pyaudiowpatch_stub"
    pkg = stub_root / "pyaudiowpatch"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "raise ImportError("
        "'pyaudiowpatch stub: headless boot не має його імпортувати')\n",
        encoding="utf-8",
    )
    return stub_root


def _get_json(port: int, path: str, timeout: float = 2.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        data = json.loads(body) if body else {}
        return resp.status, data
    finally:
        conn.close()


def _spawn_headless(tmp_path: Path, port: int, extra_env: dict | None = None) -> subprocess.Popen:
    stub_root = _make_pyaudiowpatch_stub(tmp_path)
    env = dict(os.environ)
    env.update({
        "RECALL_PROFILE": "headless",
        "FORCE_CPU": "1",
        "FLASK_PORT": str(port),
        "FLASK_HOST": "127.0.0.1",
        "FLASK_DEBUG": "false",
        "APP_ENV": "testing",
        "AUTO_CLEANUP_ENABLED": "false",
        "PYTHONPATH": str(stub_root) + os.pathsep + env.get("PYTHONPATH", ""),
        # Гейт Telegram (Wave 1, config-registry-fix-02): TELEGRAM_SESSION —
        # абсолютний дефолт у config.py, cwd=tmp_path його не закриває. Файл
        # <tmp_path>/telegram.session свідомо не існує → _maybe_launch_telegram_
        # listener падає в 'absent' незалежно від TELEGRAM_API_ID/HASH у
        # реальному .env власника — жоден Telegram-subprocess не стартує.
        "TELEGRAM_SESSION": str(tmp_path / "telegram"),
    })
    # Явно НЕ довіряти localhost за замовчуванням (headless fail-closed) —
    # мірило не потребує RECALL_API_KEY, бо /api/ready в exempt-списку.
    env.pop("RECALL_API_KEY", None)
    env.pop("RECALL_LOCAL_TRUSTED", None)
    if extra_env:
        env.update(extra_env)
    log_path = tmp_path / "headless_boot.log"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP_PY)],
        cwd=str(tmp_path),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    proc._recall_log_file = log_file  # type: ignore[attr-defined]
    proc._recall_log_path = log_path  # type: ignore[attr-defined]
    return proc


def _terminate(proc: subprocess.Popen) -> list[int]:
    """Гарантоване прибирання дерева процесів дочірнього PID.

    Не покладається на `atexit` дочірнього процесу: на Windows `terminate()`
    робить `TerminateProcess`, який `atexit`-хуки НЕ виконує (саме тому
    `_stop_listener` у `app.py` не рятує — ми взагалі не даємо слухачу
    стартувати, гейт з боку env). Термінує/вбиває САМ процес і всіх його
    нащадків за PID через psutil (ніколи за іменем образу —
    `memory/never-taskkill-by-image-name.md`), з жорстким `kill()`-фолбеком
    після тайм-ауту `terminate()`. Повертає pid-и дерева на момент виклику —
    виклик перевіряє явно, що жоден із них не пережив прибирання, а не лише
    вірить `proc.wait()`.
    """
    try:
        parent = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        parent = None
    tree = list(parent.children(recursive=True)) if parent is not None else []
    if parent is not None:
        tree.append(parent)
    pids = [p.pid for p in tree]

    for p in tree:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(tree, timeout=15)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=15)

    try:
        proc.wait(timeout=5)
    except Exception:
        pass

    log_file = getattr(proc, "_recall_log_file", None)
    if log_file is not None:
        log_file.close()

    return pids


def _assert_process_tree_gone(pids: list[int]) -> None:
    """Явна перевірка прибирання: жоден pid із дерева не має бути живий."""
    survivors = [pid for pid in pids if psutil.pid_exists(pid)]
    assert survivors == [], (
        f"процеси пережили прибирання (pid не за іменем образу): {survivors}"
    )


def _boot_log(proc: subprocess.Popen) -> str:
    log_path = getattr(proc, "_recall_log_path", None)
    if log_path is None or not log_path.exists():
        return "(нема логу)"
    return log_path.read_text(encoding="utf-8", errors="replace")[-4000:]


def _full_boot_log(proc: subprocess.Popen) -> str:
    """Повний (необрізаний) вміст boot-логу.

    `_boot_log` навмисно ріже до останніх 4000 символів (розмір для
    діагностичного повідомлення у `pytest.fail`/assert). Тести, які
    перевіряють КОНКРЕТНІ рядки логера (напр. дві фрази `_warm_up` або
    валідність кожного JSON-рядка), не можуть покладатись на цей хвіст —
    при тривалому поллінгу access-логи Werkzeug виштовхують потрібні рядки
    з вікна 4000 символів.
    """
    log_path = getattr(proc, "_recall_log_path", None)
    if log_path is None or not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


def test_headless_boot_ready_with_preload_disabled(tmp_path):
    """Мірило роадмепу: headless + FORCE_CPU + без прогріву -> ready=true швидко."""
    port = _free_port()
    proc = _spawn_headless(tmp_path, port, extra_env={"RECALL_PRELOAD_WHISPER": "0"})
    try:
        deadline = time.time() + 120
        status, data = None, None
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"дочірній процес завершився передчасно (код {proc.returncode}):\n"
                    f"{_boot_log(proc)}"
                )
            try:
                status, data = _get_json(port, "/api/ready")
                if status == 200:
                    break
            except (ConnectionRefusedError, OSError):
                pass
            time.sleep(0.5)

        assert status == 200, f"/api/ready не віддав 200 за 120с:\n{_boot_log(proc)}"
        assert data["ready"] is True
        assert data["profile"] == "headless"
        assert data["checks"]["whisper"]["preload"] == "disabled"
        # Гейт Telegram (config-registry-fix-02): TELEGRAM_SESSION вказує в
        # tmp_path без .session-файлу → слухач не міг стартувати незалежно
        # від TELEGRAM_API_ID/HASH у реальному .env цієї машини.
        assert data["checks"]["telegram"] in ("disabled", "absent"), (
            f"Telegram-слухач стартував у тесті: {data['checks']['telegram']}"
        )

        # `/` вимкнено у headless (shell не піднімається).
        root_status, root_data = _get_json(port, "/")
        assert root_status == 404
        assert root_data.get("error") == "headless"
    finally:
        _assert_process_tree_gone(_terminate(proc))


def _preload_cache_status() -> tuple[bool, str]:
    """(is_cached, skip_reason) для `DEFAULT_PRELOAD_MODEL` у локальному HF-кеші.

    Без голого `except Exception` (знахідка N1): `ImportError`
    (нема `whisper_manager_new`/`faster-whisper`) тепер спливає як помилка
    збору тесту, а не тихо ковтається в те саме `False`, що і «модель не
    прогріта» — раніше `skipif` був невідрізненний від реального збою
    імпорту.
    """
    from whisper_manager_new import _hf_cache_base, _hf_models_map, _repo_cached
    from app.services.whisper_preload import DEFAULT_PRELOAD_MODEL

    repo = _hf_models_map().get(DEFAULT_PRELOAD_MODEL)
    if not repo:
        return False, (
            f"faster-whisper не знає репозиторію для моделі {DEFAULT_PRELOAD_MODEL!r} "
            "(перевір встановлену версію faster-whisper)"
        )
    cache_base = _hf_cache_base()
    cached = _repo_cached(repo)
    reason = (
        f"модель прогріву {DEFAULT_PRELOAD_MODEL!r} (репо {repo!r}) відсутня у "
        f"HF-кеші {cache_base} цієї машини — не якати мережу в тесті"
    )
    return cached, reason


_PRELOAD_CACHED, _PRELOAD_SKIP_REASON = _preload_cache_status()


@pytest.mark.skipif(not _PRELOAD_CACHED, reason=_PRELOAD_SKIP_REASON)
def test_headless_boot_ready_waits_for_preload(tmp_path):
    """Без RECALL_PRELOAD_WHISPER=0: спочатку 503 (модель ще вантажиться), потім 200.

    Доказ, що процес реально пройшов стан «модель вантажиться», НЕ спирається
    на гонку HTTP-поллінгу (вікно 503 може бути коротшим за один опит — тому
    Non-goals цієї історії забороняють робити `saw_not_ready` обов'язковим).
    Натомість перевіряється `_boot_log`: обидва INFO-рядки `_warm_up`
    (`whisper_preload.py:57-63`) мусять бути в логу дочірнього процесу, і
    жодного WARNING про невдалий прогрів — це доказ через сам продукт, а не
    через таймінг тесту.
    """
    from app.services.whisper_preload import DEFAULT_PRELOAD_MODEL

    port = _free_port()
    proc = _spawn_headless(tmp_path, port)
    try:
        deadline = time.time() + 180
        preload_sequence: list[str] = []
        status, data = None, None
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"дочірній процес завершився передчасно (код {proc.returncode}):\n"
                    f"{_boot_log(proc)}"
                )
            try:
                status, data = _get_json(port, "/api/ready")
            except (ConnectionRefusedError, OSError):
                # (а) полінг стартує до підйому HTTP: connection refused -> повтор,
                # крок ≤ 50мс.
                time.sleep(0.05)
                continue
            if status == 200 and data.get("ready") is True:
                preload_sequence.append(
                    data.get("checks", {}).get("whisper", {}).get("preload")
                )
                break
            if status == 503:
                preload = data.get("checks", {}).get("whisper", {}).get("preload")
                preload_sequence.append(preload)
                # (б) кожен 503 несе preload=='loading' рівно: 'pending' більше
                # не в дозволених, 'disabled'/'failed' тут — провал (робота
                # не блокується б цими станами взагалі -- system.py:172-181
                # пускає 200 і при 'disabled', і 'loading' -- єдиний стан, що
                # тримає 503, поки whisper_manager є і прогрів увімкнено).
                assert preload == "loading", (
                    f"неочікуваний стан прогріву у 503-відповіді "
                    f"(мусить бути 'loading'): {data}"
                )
            time.sleep(0.05)

        assert status == 200, f"/api/ready не став 200 за 180с:\n{_boot_log(proc)}"
        assert data["ready"] is True
        # (в) фінальна відповідь.
        assert data["checks"]["whisper"]["preload"] == "ready"
        assert data["checks"]["whisper"]["model"] == DEFAULT_PRELOAD_MODEL
        # Гейт Telegram (config-registry-fix-02) — див. коментар у першому тесті.
        assert data["checks"]["telegram"] in ("disabled", "absent"), (
            f"Telegram-слухач стартував у тесті: {data['checks']['telegram']}"
        )

        # (д) якщо 'loading' спостерігався хоч раз — фінальний 'ready' йде
        # строго після нього в послідовності відповідей.
        if "loading" in preload_sequence:
            ready_idx = len(preload_sequence) - 1
            assert preload_sequence[ready_idx] == "ready"
            last_loading_idx = max(
                i for i, v in enumerate(preload_sequence) if v == "loading"
            )
            assert last_loading_idx < ready_idx, (
                f"'ready' не йде строго після 'loading': {preload_sequence}"
            )

        # (г) доказ через сам продукт, не через гонку поллінгу: обидва рядки
        # `_warm_up` присутні в boot-логу, і жодного провалу прогріву.
        full_log = _full_boot_log(proc)
        loading_line = f"Whisper preload: завантажую модель {DEFAULT_PRELOAD_MODEL} у фоні..."
        ready_line_prefix = f"Whisper preload: модель {DEFAULT_PRELOAD_MODEL} готова за"
        failed_line = f"Whisper preload: не вдалося прогріти {DEFAULT_PRELOAD_MODEL}"
        assert loading_line in full_log, (
            f"boot-лог не містить рядка старту прогріву {loading_line!r}:\n{full_log[-4000:]}"
        )
        assert ready_line_prefix in full_log, (
            f"boot-лог не містить рядка завершення прогріву {ready_line_prefix!r}:\n"
            f"{full_log[-4000:]}"
        )
        assert failed_line not in full_log, (
            f"прогрів впав у виняток, хоча очікувався успіх:\n{full_log[-4000:]}"
        )
    finally:
        _assert_process_tree_gone(_terminate(proc))


def test_headless_boot_json_logs(tmp_path):
    """Критерій N2 (config-registry-fix-r3-02): наскрізне покриття гейта
    `RECALL_LOG_FORMAT=json` (`app.py:239-245`) РЕАЛЬНИМ дочірнім процесом —
    на відміну від старого `test_json_format_end_to_end_via_env`
    (`tests/test_json_logs.py`), який відтворював ту саму умову в тілі
    тесту і тому не міг почервоніти від поламки самого `app.py`.
    `RECALL_PRELOAD_WHISPER=0` — тест не залежить від наявності моделі у
    HF-кеші і лишається швидким."""
    port = _free_port()
    proc = _spawn_headless(
        tmp_path, port,
        extra_env={
            "RECALL_PRELOAD_WHISPER": "0",
            "RECALL_LOG_FORMAT": "json",
            # Пригнічує сторонні `warnings.warn(...)` (напр. ctranslate2's
            # deprecated pkg_resources), які пишуться напряму в stderr в
            # обхід logging і тому НІКОЛИ не будуть JSON незалежно від
            # RECALL_LOG_FORMAT — інакше вони псували б перевірку "кожен
            # непорожній рядок" шумом, не повʼязаним із гейтом app.py.
            "PYTHONWARNINGS": "ignore",
        },
    )
    try:
        deadline = time.time() + 120
        status, data = None, None
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"дочірній процес завершився передчасно (код {proc.returncode}):\n"
                    f"{_boot_log(proc)}"
                )
            try:
                status, data = _get_json(port, "/api/ready")
                if status == 200:
                    break
            except (ConnectionRefusedError, OSError):
                pass
            time.sleep(0.05)

        assert status == 200, f"/api/ready не віддав 200 за 120с:\n{_boot_log(proc)}"
        assert data["ready"] is True

        full_log = _full_boot_log(proc)
        lines = [ln for ln in full_log.splitlines() if ln.strip()]
        assert lines, "boot-лог порожній — нема що перевіряти"
        # Flask CLI друкує ці два рядки через click.echo (flask/cli.py
        # show_server_banner, безумовний виклик з flask/app.py:607) НАПРЯМУ в
        # stdout, в обхід logging-модуля — вони не проходять і не можуть
        # пройти через жоден форматер app.py незалежно від RECALL_LOG_FORMAT.
        # Єдиний виняток із перевірки "кожен рядок — JSON", бо жодна поламка
        # гейта app.py:239-245 не могла б це полагодити чи зламати.
        _FLASK_CLI_BANNER_PREFIXES = (" * Serving Flask app", " * Debug mode:")
        for line in lines:
            if line.startswith(_FLASK_CLI_BANNER_PREFIXES):
                continue
            # Поламка гейта app.py:239-245 (напр. форматер лишається текстовим
            # усупереч RECALL_LOG_FORMAT=json) -> рядок не парситься як JSON.
            parsed = json.loads(line)
            assert {"ts", "level", "logger", "msg", "request_id"} <= set(parsed.keys()), (
                f"рядок логу без обов'язкових полів: {line}"
            )
    finally:
        _assert_process_tree_gone(_terminate(proc))
