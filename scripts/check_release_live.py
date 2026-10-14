"""Дійшов випуск до копії Recall, на якій працює власник? (docs/defects/shipped-but-not-restarted.md)

    python scripts/check_release_live.py              # живий стан: код, /api/health, власник порту 5050
    python scripts/check_release_live.py <snap.json>  # знімок стану (фікстури картки дефекту)

Знімок: {"code_version", "running_version", "port_owner_cmdline", "task_exe"}.
Exit 1 — випуск не дійшов: працююча версія ≠ версії в коді, або порт тримає копія app.py,
запущена НЕ задачею RecallAutostart (ручний Recall.bat — після рестарту оновлення до неї не доходять).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def verdict(snap: dict) -> list[str]:
    problems = []
    code, running = snap.get('code_version'), snap.get('running_version')
    if not running:
        problems.append('Recall не відповідає на /api/health')
    elif code != running:
        problems.append(f'працює {running}, а в коді {code} — Recall не перезапущено на випуск')
    owner, task_exe = (snap.get('port_owner_cmdline') or ''), (snap.get('task_exe') or '')
    if owner and task_exe and os.path.normcase(task_exe) not in os.path.normcase(owner):
        problems.append(f'порт тримає не копія RecallAutostart: {owner}')
    # MCP-сервер — окремий процес кожного клієнта (Claude Desktop, сесії Claude Code): він
    # завантажив mcp_server.py при старті й нових тулзів не бачить, доки клієнт не перезапущено.
    code_mtime = snap.get('mcp_code_mtime')
    stale = [p for p in (snap.get('mcp_servers') or [])
             if code_mtime is not None and p.get('started', 0) < code_mtime]
    if stale:
        problems.append(f"{len(stale)} MCP-сервер(ів) на старому mcp_server.py (PID "
                        f"{', '.join(str(p.get('pid')) for p in stale)}) — перезапусти Claude Desktop "
                        f"/ сесії Claude Code або /mcp → reconnect")
    return problems


def _live() -> dict:
    src = open(os.path.join(ROOT, 'app', '__init__.py'), encoding='utf-8').read()
    code = re.search(r'__version__ = "([^"]+)"', src).group(1)
    try:
        with urllib.request.urlopen('http://127.0.0.1:5050/api/health', timeout=5) as r:
            running = json.load(r).get('version')
    except Exception:  # noqa: BLE001 — недоступний Recall і є відповідь
        running = None
    ps = ("$o=(Get-NetTCPConnection -LocalPort 5050 -State Listen -EA SilentlyContinue | Select -First 1).OwningProcess;"
          "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$o\";"
          "$q=Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.ParentProcessId)\" -EA SilentlyContinue;"
          "if($q -and $q.CommandLine -match 'app\\.py'){$p=$q};"
          "$t=(Get-ScheduledTask RecallAutostart -EA SilentlyContinue).Actions[0].Execute;"
          "@{owner=$p.ExecutablePath+' '+$p.CommandLine; task=$t} | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True,
                             text=True, timeout=30).stdout
        info = json.loads(out or '{}')
    except Exception:  # noqa: BLE001
        info = {}
    mcp_ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mcp_server\\.py' "
              "-and $_.ExecutablePath -match '\\\\.venv\\\\' } | ForEach-Object { @{pid=$_.ProcessId; "
              "started=([DateTimeOffset]$_.CreationDate).ToUnixTimeSeconds()} } | ConvertTo-Json -Compress")
    try:
        raw = subprocess.run(['powershell', '-NoProfile', '-Command', mcp_ps], capture_output=True,
                             text=True, timeout=30).stdout.strip()
        servers = json.loads(raw) if raw else []
        servers = [servers] if isinstance(servers, dict) else servers
    except Exception:  # noqa: BLE001
        servers = []
    return {'code_version': code, 'running_version': running,
            'port_owner_cmdline': info.get('owner') or '', 'task_exe': info.get('task') or '',
            'mcp_code_mtime': os.path.getmtime(os.path.join(ROOT, 'mcp_server.py')),
            'mcp_servers': servers}


def main(argv: list[str]) -> int:
    snap = json.load(open(argv[1], encoding='utf-8')) if len(argv) > 1 else _live()
    problems = verdict(snap)
    for p in problems:
        print('НЕ ДІЙШОВ:', p)
    if not problems:
        print(f"ok: працює {snap.get('running_version')} — та сама версія, що в коді")
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
