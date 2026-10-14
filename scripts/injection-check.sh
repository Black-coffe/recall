#!/usr/bin/env bash
# VULYK injection check - the `check:` of docs/defects/command-injection-follows-cwd.md.
#
#   Usage: bash scripts/injection-check.sh <file>
#
# A command's or skill's `!` injection runs in the session's shell cwd, and Claude Code keeps a
# Bash `cd` that stays inside the project - so an injection that names its script by a relative path
# breaks the moment the session drifts into a subfolder ("bash: scripts/state.sh: No such file or
# directory"). Name it through ${CLAUDE_PROJECT_DIR} (or ${CLAUDE_SKILL_DIR}) instead.
#
# Both injection forms Claude Code runs are read: inline !`cmd` after whitespace or at the start,
# and a fenced ```! block (one command per line). A path is relative unless it starts with a
# ${CLAUDE_PROJECT_DIR} / ${CLAUDE_SKILL_DIR} / ${CLAUDE_PLUGIN_ROOT} / $HOME variable, `/`, `~` or a
# drive letter. After an interpreter (bash, sh, python, node, pwsh ...) its first argument is the
# script; with no interpreter the command itself is judged, and only when it holds a slash.
#
# Exit 1 naming each relative injection; exit 0 for a clean file and for any file that is not a
# command (.claude/commands/*.md), a skill (SKILL.md) or a defect fixture (*.fixture): the defect
# gate hands every block check whatever argument it got. Exit 2 when no python is on PATH.
set -u

f="${1:-}"
[ -n "$f" ] && [ -f "$f" ] || exit 0
case "${f//\\//}" in
  *.claude/commands/*.md|*SKILL.md|*.fixture) ;;
  *) exit 0 ;;
esac
PY="$(command -v python3 || command -v python)" || { echo "injection-check: needs python3 or python" >&2; exit 2; }

"$PY" - "$f" <<'PY'
import re, shlex, sys

path = sys.argv[1]
text = open(path, encoding="utf-8", errors="replace").read()

FENCED = re.compile(r"```!\s*\n?([\s\S]*?)\n?```")
INLINE = re.compile(r"(?:^|(?<=\s))!`([^`]+)`")
INTERPRETERS = {"bash", "sh", "zsh", "dash", "python", "python3", "py", "node", "pwsh",
                "powershell", "perl", "ruby", "source", "."}
ANCHORED = re.compile(r"^(\$\{?(CLAUDE_PROJECT_DIR|CLAUDE_SKILL_DIR|CLAUDE_PLUGIN_ROOT|HOME)\}?|/|~|[A-Za-z]:[/\\])")

commands = []
for block in FENCED.findall(text):
    commands += [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]
commands += [m.strip() for m in INLINE.findall(FENCED.sub("", text))]


def relative_script(cmd):
    """The relative script path a command names, or None when it names none."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]  # VAR=value prefixes
    if not words:
        return None
    if words[0] in INTERPRETERS:
        for w in words[1:]:
            if w in ("-c", "-e", "-m", "-Command", "-EncodedCommand"):
                return None  # inline code or a module, not a script path
            if w.startswith("-"):
                continue
            return None if ANCHORED.match(w) else w
        return None
    first = words[0]
    if ("/" in first or "\\" in first) and not ANCHORED.match(first):
        return first
    return None


bad = [(c, p) for c in commands for p in [relative_script(c)] if p]
for cmd, rel in bad:
    print("%s: injection `%s` names %s relative to the session cwd - use ${CLAUDE_PROJECT_DIR}/%s"
          % (path, cmd, rel, rel[2:] if rel.startswith("./") else rel))
sys.exit(1 if bad else 0)
PY
