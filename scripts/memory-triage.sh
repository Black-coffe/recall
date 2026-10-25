#!/usr/bin/env bash
# VULYK memory triage - the lister behind `/vulyk-gc memory` (rules-tree, ADR-021, ask 11).
#
#   Usage: bash scripts/memory-triage.sh [<memory-dir>]
#          bash scripts/memory-triage.sh --index-lines <MEMORY.md> <file>...
#
# Lists this project's private auto memory so the main session can sort it into cards, personal or
# stays, and delete. Default dir: ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<slug>/memory, where
# <slug> is the repo root path with every character but [0-9A-Za-z] turned into `-`, as Claude Code
# names it (E:\Projects\vulyk -> E--Projects-vulyk, D:/My_App -> D--My-App). Prints one TSV row per *.md except MEMORY.md / memory.md:
#   path<TAB>type<TAB>first heading
# `type` comes from a `type:` line inside the leading frontmatter (nested or not), else `-`; the
# heading is the first `#` line, else `-`. An absent dir prints `no private memory: <dir>`, exit 0.
#
# --index-lines prints the lines of the given MEMORY.md that name any of the given files (by base
# name), so the session deletes exactly those lines and no other.
#
# The script reads only: it never writes, moves or deletes. The session acts after the owner's yes.
set -u

if [ "${1:-}" = "--index-lines" ]; then
  idx="${2:-}"
  [ -n "$idx" ] && [ -f "$idx" ] || { echo "memory-triage: no index file: ${idx:-<none>}" >&2; exit 2; }
  shift 2
  [ $# -gt 0 ] || { echo "memory-triage: --index-lines needs at least one file" >&2; exit 2; }
  for f in "$@"; do
    b="${f//\\//}"; b="${b##*/}"
    grep -F -- "$b" "$idx" | tr -d '\r'
  done | awk '!seen[$0]++'
  exit 0
fi

dir="${1:-}"
if [ -z "$dir" ]; then
  root="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "memory-triage: not in a git repo; name the dir" >&2; exit 2; }
  slug="$(printf '%s' "$root" | LC_ALL=C.UTF-8 sed 's#[^0-9A-Za-z]#-#g')"
  dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$slug/memory"
fi
[ -d "$dir" ] || { echo "no private memory: $dir"; exit 0; }

for f in "$dir"/*.md; do
  [ -f "$f" ] || continue
  case "${f##*/}" in MEMORY.md|memory.md) continue ;; esac
  tr -d '\r' < "$f" | awk -v p="$f" '
    NR == 1 && $0 == "---" { fm = 1; next }
    fm && $0 == "---" { fm = 0; next }
    fm && type == "" && match($0, /^[ \t]*type:[ \t]*/) { type = substr($0, RLENGTH + 1); sub(/[ \t]+$/, "", type); next }
    !fm && head == "" && /^#+[ \t]/ { head = $0; sub(/^#+[ \t]+/, "", head) }
    END { printf "%s\t%s\t%s\n", p, (type == "" ? "-" : type), (head == "" ? "-" : head) }'
done
