#!/usr/bin/env bash
# VULYK human check - stage 05 of the cycle, the one no agent may take.
#
#   Usage: scripts/human-check.sh <spec-dir> <ACCEPTED|REJECTED> [where you looked / note]
#          scripts/human-check.sh docs/specs/oauth ACCEPTED "staging, logged in as a new user, logout works"
#          scripts/human-check.sh --check <spec-dir>       # is the newest check still about THIS pack at THIS commit?
#
# The blind acceptance gate can say the software does what the brief's words said. Only
# the person who wrote the words can say whether the words said what they meant, so the
# owner looks - on a test or a live version - and this script writes down that they did.
# It never judges anything: the verdict is the caller's, and the caller is a human. The
# Queen runs it AFTER the owner has answered, quoting their words in the note, and never
# before - a check recorded on the owner's behalf is the failure this stage exists to stop.
#
# Two records, same fact:
#   plan.md                  - one `**Checked:**` line appended, beside `**Approved:**`: the
#                              artifact /vulyk-ship refuses without.
#   memory/stats/human.jsonl - the same line as data, stamped with the commit and the pack
#                              fingerprint it was given against, so `--check` can say whether
#                              a later commit or a repair story has made it about something else.
#
# Exit status is always 0: this records, it does not block. `/vulyk-ship` is what blocks.
set -u

# The identity of a pack: its story files, by name, in a stable order - must match
# acceptance-log.sh exactly, because `--check` compares against verdicts written there too.
pack_fingerprint() { # pack_fingerprint <spec-dir>
  local dir="$1" names hasher=""
  names="$(
    for f in "$dir"/*.md; do
      [ -f "$f" ] || continue
      grep -q '^story:' "$f" 2>/dev/null || continue
      basename "$f"
    done | LC_ALL=C sort | tr '\n' ' '
  )"
  if command -v sha256sum >/dev/null 2>&1; then hasher="sha256sum"
  elif command -v shasum >/dev/null 2>&1; then hasher="shasum -a 256"; fi
  if [ -n "$hasher" ]; then
    printf '%s' "$names" | $hasher | cut -c1-12
  else
    printf 'n%s' "$(printf '%s' "$names" | wc -w | tr -d ' ')"
  fi
}

# Recording a check is itself a commit, and so is recording the ship. Those commits move
# HEAD without moving the software, so a check is still about what ships when every path
# changed since it was given is one of the cycle's own records. Anything else - one line of
# code, one test - is a change the owner did not see. Must match ship-check.sh exactly.
paperwork_only() { # paperwork_only <root> <from-commit> <to-commit>
  local changed p
  git -C "$1" merge-base --is-ancestor "$2" "$3" 2>/dev/null || return 1
  changed="$(git -C "$1" diff --name-only "$2" "$3" 2>/dev/null)" || return 1
  [ -n "$changed" ] || return 0
  while IFS= read -r p; do
    case "$p" in
      */plan.md|memory/stats/human.jsonl|memory/stats/acceptance.jsonl|memory/stats/ship.jsonl) ;;
      *) return 1 ;;
    esac
  done <<EOF
$changed
EOF
  return 0
}

# --- `--check`: does the newest recorded human check still describe this pack, here? ----
if [ "${1:-}" = "--check" ]; then
  CSPEC="${2:-}"
  [ -n "$CSPEC" ] && [ -d "$CSPEC" ] || {
    echo "human-check: usage: $0 --check <spec-dir>" >&2; exit 0; }
  CROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "human-check: CANNOT RUN - not a git repo, so there is no memory/stats to read." >&2
    exit 0; }
  CSTATS="$CROOT/memory/stats/human.jsonl"
  CNAME="$(basename "$CSPEC")"
  LAST="$(grep -F "\"spec\":\"$CNAME\"" "$CSTATS" 2>/dev/null | tail -1)"
  if [ -z "$LAST" ]; then
    echo "human-check: $CNAME - NO HUMAN CHECK RECORDED. Nobody has looked at this pack yet."
    exit 0
  fi
  WAS_V="$(printf '%s' "$LAST" | sed -n 's/.*"verdict":"\([^"]*\)".*/\1/p')"
  WAS_P="$(printf '%s' "$LAST" | sed -n 's/.*"pack":"\([^"]*\)".*/\1/p')"
  WAS_H="$(printf '%s' "$LAST" | sed -n 's/.*"head":"\([^"]*\)".*/\1/p')"
  NOW_P="$(pack_fingerprint "$CSPEC")"
  NOW_H="$(git -C "$CROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  if [ "$WAS_V" != "ACCEPTED" ]; then
    echo "human-check: $CNAME - REJECTED. The owner looked and said no; the newest record is a rejection."
    echo "  Route the findings into fix stories and re-run the cycle from /vulyk-build; then look again."
    exit 0
  fi
  if [ "$WAS_P" != "$NOW_P" ]; then
    echo "human-check: $CNAME - STALE (pack). The owner accepted a different set of stories"
    echo "  (recorded $WAS_P, now $NOW_P). Stories were added, removed or renamed since - look again."
    exit 0
  fi
  if [ "$WAS_H" != "$NOW_H" ] && ! paperwork_only "$CROOT" "$WAS_H" "$NOW_H"; then
    echo "human-check: $CNAME - STALE (commit). The owner accepted at $WAS_H; HEAD is now $NOW_H."
    echo "  Something was committed after they looked. What they saw is not what ships - look again."
    exit 0
  fi
  echo "human-check: $CNAME - CURRENT. The owner accepted this exact pack at commit $WAS_H, and nothing but cycle paperwork has landed since."
  exit 0
fi

SPEC="${1:-}"
VERDICT="${2:-}"
NOTE="${3:-}"

if [ -z "$SPEC" ] || [ ! -d "$SPEC" ] || [ -z "$VERDICT" ]; then
  echo "human-check: usage: $0 <spec-dir> <ACCEPTED|REJECTED> [note]" >&2
  exit 0
fi

case "$VERDICT" in
  ACCEPTED|REJECTED) ;;
  *) echo "human-check: verdict must be ACCEPTED or REJECTED (got '$VERDICT') - a human check has no cannot-run branch; if you could not look, you have not checked." >&2; exit 0 ;;
esac

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "human-check: CANNOT RUN - not a git repo, so there is no memory/stats to append to." >&2
  exit 0
}

PLAN="$SPEC/plan.md"
[ -f "$PLAN" ] || {
  echo "human-check: CANNOT RUN - $PLAN does not exist. A check is recorded beside the approval it follows; without a plan there is nothing to record it against." >&2
  exit 0
}

# Who looked: the committer identity is the closest thing to a signature git has. Never
# invent a name - an unnamed check is still a check, an attributed one nobody made is not.
OWNER="$(git -C "$ROOT" config user.name 2>/dev/null || true)"
[ -n "$OWNER" ] || OWNER="${USER:-${USERNAME:-owner}}"
DATE="$(date -u +%Y-%m-%d)"
HEAD_SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
PACK="$(pack_fingerprint "$SPEC")"

# The note is the owner's own words about what they saw, and it lands in two committed
# files - same exposure as an acceptance note, same filter first.
if [ -f "$ROOT/scripts/redact.sh" ]; then
  SAFE_NOTE="$(printf '%s' "$NOTE" | bash "$ROOT/scripts/redact.sh")"
else
  SAFE_NOTE="$NOTE"
fi
ONE_LINE="$(printf '%s' "$SAFE_NOTE" | tr -d '\n' | tr -d '\r')"
ESCAPED="$(printf '%s' "$ONE_LINE" | tr -d '\\' | tr '"' "'")"

# plan.md: append, never replace. A second look after a repair round is a second line, and
# the sequence is the record - a rejection that was later accepted stays visible as both.
LINE="**Checked:** $VERDICT by $OWNER, $DATE, at $HEAD_SHA"
[ -n "$ONE_LINE" ] && LINE="$LINE - $ONE_LINE"
# Drop the template placeholder the first time a real line lands.
if grep -q '^\*\*Checked:\*\* <' "$PLAN" 2>/dev/null; then
  TMP="$PLAN.tmp$$"
  grep -v '^\*\*Checked:\*\* <' "$PLAN" > "$TMP" && mv "$TMP" "$PLAN"
fi
printf '%s\n' "$LINE" >> "$PLAN"

mkdir -p "$ROOT/memory/stats"
STATS="$ROOT/memory/stats/human.jsonl"
BY_ESC="$(printf '%s' "$OWNER" | tr -d '\\' | tr '"' "'")"
printf '{"ts":"%s","spec":"%s","verdict":"%s","by":"%s","head":"%s","pack":"%s","note":"%s"}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(basename "$SPEC")" "$VERDICT" "$BY_ESC" "$HEAD_SHA" "$PACK" "$ESCAPED" >> "$STATS"

echo "human-check: $(basename "$SPEC") - $VERDICT by $OWNER at $HEAD_SHA (pack $PACK)"
echo "  recorded in $PLAN and memory/stats/human.jsonl. Commit the record; any CODE commit after it makes it stale;"
echo "  '$0 --check $SPEC' says whether it still describes what ships."
if [ "$VERDICT" = REJECTED ]; then
  echo "  ! The owner said no. Each thing they named becomes a fix story in this spec, through"
  echo "    /vulyk-build - never a hand patch - and both gates run again before the next look."
fi
exit 0
