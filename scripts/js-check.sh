#!/usr/bin/env bash
# Syntax gate for the SPA. The project has no linter and no JS test runner, so a
# broken brace in one file reaches the browser as a blank page and is only caught
# by a human at stage 05. `node --check` parses each file without running it.
#
#   Usage: bash scripts/js-check.sh [path ...]     (default: the SPA + the worker)
#
# These files are plain scripts (window.Recall globals, no modules), so the
# default --check parse goal is the right one.

set -u

command -v node >/dev/null 2>&1 || {
  echo "js-check: node not found in PATH - cannot verify SPA syntax." >&2
  exit 2
}

if [ "$#" -gt 0 ]; then
  FILES="$*"
else
  FILES="$(find static/js/recall -name '*.js' -type f | sort) static/sw.js"
fi

fails=0
n=0
for f in $FILES; do
  [ -f "$f" ] || { echo "js-check: missing $f" >&2; fails=$((fails+1)); continue; }
  n=$((n+1))
  node --check "$f" || fails=$((fails+1))
done

if [ "$fails" -ne 0 ]; then
  echo "js-check: $fails of $n file(s) failed to parse." >&2
  exit 1
fi
echo "js-check: $n file(s) parse clean."
