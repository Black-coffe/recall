"""README "Quick start" <-> code: the install instructions do not diverge from requirements.txt and .env.example.

Why the test exists: on 08.10.2026 the public README did not install on a clean machine in four places —
no `python -m venv`, `pip install -r` failed on `pkg_resources`, torchaudio stayed a CPU build,
PyAV 19 broke the very first transcription. The code and dependencies changed, the README did not, and it was noticed by an
install measurement, not by tests. Now the divergence is caught here, in the private tree and in the public one
(step 4 of `scripts/mirror/README.md`). A full run from scratch — `scripts/mirror/clean_install_check.py`.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / 'README.md').read_text(encoding='utf-8')
REQS = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
ENV_EXAMPLE = (ROOT / '.env.example').read_text(encoding='utf-8')


def _quickstart() -> str:
    start = README.index('## 🚀 Quick start')
    end = README.index('\n## ', start + 1)
    return README[start:end]


def _install_block() -> list[str]:
    """Commands of the first cmd block of section "1. Dependencies", without REM and empty lines."""
    qs = _quickstart()
    sec = qs[qs.index('### 1. Dependencies'):]
    block = re.search(r'```cmd\n(.*?)```', sec, re.S).group(1)
    return [l.strip() for l in block.splitlines() if l.strip() and not l.strip().upper().startswith('REM')]


def _env_keys() -> set[str]:
    return set(re.findall(r'^#?\s*([A-Z][A-Z0-9_]+)=', ENV_EXAMPLE, re.M))


def test_venv_created_before_first_pip():
    cmds = _install_block()
    venv = next(i for i, c in enumerate(cmds) if c.startswith('python -m venv .venv'))
    first_pip = next(i for i, c in enumerate(cmds) if '-m pip' in c)
    assert venv < first_pip, 'python -m venv .venv must come before the first pip'


def test_build_tools_and_no_isolation():
    cmds = _install_block()
    i_setup = next(i for i, c in enumerate(cmds) if '"setuptools<81"' in c)
    i_reqs = next(i for i, c in enumerate(cmds) if '-r requirements.txt' in c)
    assert i_setup < i_reqs
    assert '--no-build-isolation' in cmds[i_reqs], 'without --no-build-isolation pip ignores setuptools<81'


def test_torch_before_requirements_and_matches_pin():
    cmds = _install_block()
    i_torch = next(i for i, c in enumerate(cmds) if 'download.pytorch.org' in c)
    i_reqs = next(i for i, c in enumerate(cmds) if '-r requirements.txt' in c)
    assert i_torch < i_reqs, 'torch cu121 is installed BEFORE requirements.txt, otherwise CPU torch gets downloaded'
    req_cmd = re.search(r'pip install (torch==\S+ .*)$', REQS, re.M).group(1)
    assert req_cmd in cmds[i_torch], f'README and requirements.txt name a different torch: {req_cmd!r}'


def test_every_torch_command_pins_torchaudio_same_suffix():
    for line in README.splitlines():
        if 'download.pytorch.org/whl/' not in line:
            continue
        torch = re.search(r'torch==([\d.]+\+cu\d+)', line)
        audio = re.search(r'torchaudio==([\d.]+\+cu\d+)', line)
        assert torch and audio, f'torch and torchaudio must be pinned: {line.strip()}'
        assert torch.group(1).split('+')[1] == audio.group(1).split('+')[1], line.strip()
        index = re.search(r'/whl/(cu\d+)', line).group(1)
        assert torch.group(1).endswith(index), f'torch suffix does not match the index-url: {line.strip()}'


def test_pyav_pinned_while_faster_whisper_old():
    fw = re.search(r'^faster-whisper==(\d+)\.(\d+)', REQS, re.M)
    if fw and (int(fw.group(1)), int(fw.group(2))) < (1, 2):
        assert re.search(r'^av<19', REQS, re.M), 'faster-whisper 1.1.x does not work with PyAV 19 (metadata_errors)'


def test_env_vars_named_in_quickstart_exist_in_env_example():
    qs = _quickstart()
    named = set(re.findall(r'`([A-Z][A-Z0-9_]{2,})`', qs)) | set(re.findall(r'\b([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)=', qs))
    missing = sorted(named - _env_keys())
    assert not missing, f'README names variables that are not in .env.example: {missing}'
    assert 'copy .env.example .env' in _install_block()


def test_port_in_readme_matches_default():
    port = re.search(r'^FLASK_PORT=(\d+)', ENV_EXAMPLE, re.M).group(1)
    ports = set(re.findall(r'localhost:(\d{4})', _quickstart()))
    assert ports == {port}, f'README says {ports}, .env.example — {port}'
    bat = (ROOT / 'Recall.bat').read_text(encoding='utf-8', errors='replace')
    assert f'localhost:{port}' in bat, 'Recall.bat opens a different port than the default FLASK_PORT'


def test_pyannote_models_match_requirements():
    req_models = set(re.findall(r'pyannote/[\w.-]+-\d\.\d', REQS))
    qs_models = set(re.findall(r'pyannote/[\w.-]+-\d\.\d', _quickstart()))
    assert req_models and req_models <= qs_models, f'licenses that must be accepted: {sorted(req_models - qs_models)}'


def test_python_version_matches():
    assert 'Python 3.12' in _quickstart()
