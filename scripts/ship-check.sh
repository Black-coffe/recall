#!/usr/bin/env bash
# VULYK ship gate - which stage of the cycle is a spec at, and may it be published?
#
#   Usage: scripts/ship-check.sh <spec-dir>                       # READY or NOT READY, and why
#          scripts/ship-check.sh --record <spec-dir> <version> [where it was published]
#
# Six stages, six confirmation artifacts (docs/cycle.md). This script reads every one of
# them - brief, approval line, branch line, story statuses, the blind gate's verdict, the
# owner's check - and says which is missing or stale. Deterministic, no model, no tokens.
# `/vulyk-ship` runs it first and refuses on NOT READY, the way `/vulyk-build` refuses
# without an approval line. It is the reason "did anyone actually look?" is a question
# with a file behind it rather than a memory.
#
# `--record` writes stage 06's own artifact once the human has pressed the button: a
# `**Shipped:**` line in plan.md and a row in memory/stats/ship.jsonl. The publishing
# itself is never done here and never by an agent.
#
# Exit status is always 0: this reports, it does not block. The refusing is the command's.
set -u

pack_fingerprint() { # pack_fingerprint <spec-dir> - must match acceptance-log.sh exactly
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
# changed since it was given is one of the cycle's own records. Must match human-check.sh.
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

# A marker line is "filled" when it exists and does not still carry the template's `<...>`.
marker() { # marker <plan.md> <Name> -> prints the line's value, empty if absent/placeholder
  local v
  v="$(grep -m1 "^\*\*$2:\*\*" "$1" 2>/dev/null | sed "s/^\*\*$2:\*\*[[:space:]]*//")"
  case "$v" in ''|'<'*) return 0 ;; esac
  printf '%s' "$v"
}

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "ship-check: CANNOT RUN - not a git repo, so there is no history to fix and no stats to read." >&2
  exit 0
}

# --- `--record`: stage 06's artifact -------------------------------------------------------
if [ "${1:-}" = "--record" ]; then
  RSPEC="${2:-}"; RVER="${3:-}"; RNOTE="${4:-}"
  [ -n "$RSPEC" ] && [ -d "$RSPEC" ] && [ -n "$RVER" ] || {
    echo "ship-check: usage: $0 --record <spec-dir> <version> [note]" >&2; exit 0; }
  RPLAN="$RSPEC/plan.md"
  [ -f "$RPLAN" ] || { echo "ship-check: CANNOT RUN - $RPLAN does not exist." >&2; exit 0; }
  if [ -f "$ROOT/scripts/redact.sh" ]; then
    RNOTE="$(printf '%s' "$RNOTE" | bash "$ROOT/scripts/redact.sh")"
  fi
  RNOTE="$(printf '%s' "$RNOTE" | tr -d '\n' | tr -d '\r')"
  RHEAD="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  RDATE="$(date -u +%Y-%m-%d)"
  LINE="**Shipped:** $RVER, $RDATE, at $RHEAD"
  [ -n "$RNOTE" ] && LINE="$LINE - $RNOTE"
  if grep -q '^\*\*Shipped:\*\* <' "$RPLAN" 2>/dev/null; then
    TMP="$RPLAN.tmp$$"; grep -v '^\*\*Shipped:\*\* <' "$RPLAN" > "$TMP" && mv "$TMP" "$RPLAN"
  fi
  printf '%s\n' "$LINE" >> "$RPLAN"
  mkdir -p "$ROOT/memory/stats"
  ESC="$(printf '%s' "$RNOTE" | tr -d '\\' | tr '"' "'")"
  VESC="$(printf '%s' "$RVER" | tr -d '\\' | tr '"' "'")"
  printf '{"ts":"%s","spec":"%s","version":"%s","head":"%s","pack":"%s","note":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(basename "$RSPEC")" "$VESC" "$RHEAD" "$(pack_fingerprint "$RSPEC")" "$ESC" \
    >> "$ROOT/memory/stats/ship.jsonl"
  echo "ship-check: $(basename "$RSPEC") - shipped as $RVER at $RHEAD; recorded in $RPLAN and memory/stats/ship.jsonl."
  echo "  The circle is closed. Its leftovers (UNASKED, ## Descoped, unfixed CONCERNS) are the draft of the next brief."
  exit 0
fi

SPEC="${1:-}"
[ -n "$SPEC" ] && [ -d "$SPEC" ] || {
  echo "ship-check: usage: $0 <spec-dir>   |   $0 --record <spec-dir> <version> [note]" >&2
  exit 0
}
cd "$ROOT" || exit 0
SLUG="$(basename "$SPEC")"
PLAN="$SPEC/plan.md"
MISSING=0
say()  { printf '  %-4s %-8s %s\n' "$1" "$2" "$3"; }
fail() { say "$1" "OPEN" "$2"; MISSING=$((MISSING + 1)); }
ok()   { say "$1" "ok" "$2"; }

echo "ship-check: $SLUG - the six confirmations"
echo ""

# 01 Spec
if [ -f "$SPEC/brief.md" ]; then ok 01 "spec: brief.md exists"
else fail 01 "spec: no brief.md - there is no verbatim request for anything below to answer to"; fi

# 02 Plan
if [ ! -f "$PLAN" ]; then
  fail 02 "plan: no plan.md"
elif [ -n "$(marker "$PLAN" Approved)" ]; then
  ok 02 "plan: approved - $(marker "$PLAN" Approved)"
else
  fail 02 "plan: no **Approved:** line in plan.md - /vulyk-build would have refused this pack"
fi

# 03 Code - the branch line, the branch itself, and whether the tree is settled
BRANCH_NOW="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
DEFAULT="$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
[ -n "$DEFAULT" ] || { git show-ref --verify --quiet refs/heads/main && DEFAULT=main; }
[ -n "$DEFAULT" ] || { git show-ref --verify --quiet refs/heads/master && DEFAULT=master; }
[ -n "$DEFAULT" ] || DEFAULT="main"
BRANCH_REC=""
[ -f "$PLAN" ] && BRANCH_REC="$(marker "$PLAN" Branch)"
if [ -z "$BRANCH_REC" ]; then
  fail 03 "code: no **Branch:** line in plan.md - the build did not record where its commits live"
else
  ok 03 "code: branch recorded - $BRANCH_REC (now on $BRANCH_NOW)"
fi
if [ "$BRANCH_NOW" = "$DEFAULT" ]; then
  say "" "note" "you are on $DEFAULT: ship-check reads the spec branch; run it there, before the merge"
fi
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  fail 03 "code: working tree is not clean - uncommitted changes are not part of any story's commit"
fi

# Stories - every one closed, one way or the other
TOTAL=0; DONE=0; BLOCKED=0; OPEN=0; UNREC=0
for f in "$SPEC"/*.md; do
  [ -f "$f" ] || continue
  grep -q '^story:' "$f" 2>/dev/null || continue
  TOTAL=$((TOTAL+1))
  st="$(awk -F': *' '$1 == "status" { sub(/[[:space:]]*#.*$/, "", $2); gsub(/[[:space:]]/, "", $2); print $2; exit }' "$f")"
  case "$st" in
    done) DONE=$((DONE+1)) ;;
    blocked) BLOCKED=$((BLOCKED+1)) ;;
    todo|in-progress) OPEN=$((OPEN+1)) ;;
    *) UNREC=$((UNREC+1)) ;;
  esac
done
if [ "$TOTAL" -eq 0 ]; then
  fail 03 "code: no story files - nothing was built under this spec"
elif [ "$OPEN" -gt 0 ] || [ "$UNREC" -gt 0 ]; then
  fail 03 "code: stories $DONE/$TOTAL done, $BLOCKED blocked, $OPEN still open, $UNREC unrecognised - the build is not closed"
elif [ "$BLOCKED" -gt 0 ]; then
  if [ -f "$PLAN" ] && awk '/^## Descoped/{f=1;next} /^## /{f=0} f && /^- /{c++} END{exit !(c>0)}' "$PLAN"; then
    ok 03 "code: stories $DONE/$TOTAL done, $BLOCKED blocked with ## Descoped entries on the record"
  else
    fail 03 "code: $BLOCKED blocked and ## Descoped is empty - a story that shrank with no line is a requirement that vanished"
  fi
else
  ok 03 "code: stories $DONE/$TOTAL done"
fi

# 04 Tests - the blind gate's newest verdict, and whether it is about this pack
ACC="memory/stats/acceptance.jsonl"
NOW_P="$(pack_fingerprint "$SPEC")"
LAST=""
[ -f "$ACC" ] && LAST="$(grep -F "\"spec\":\"$SLUG\"" "$ACC" | tail -1)"
if [ -z "$LAST" ]; then
  fail 04 "tests: no acceptance verdict recorded - the blind gate never judged this pack (/vulyk-review)"
else
  AV="$(printf '%s' "$LAST" | sed -n 's/.*"verdict":"\([^"]*\)".*/\1/p')"
  AP="$(printf '%s' "$LAST" | sed -n 's/.*"pack":"\([^"]*\)".*/\1/p')"
  if [ "$AP" != "$NOW_P" ]; then
    fail 04 "tests: acceptance verdict $AV is STALE - given against pack $AP, now $NOW_P; re-dispatch drone-acceptance"
  elif [ "$AV" = ACCEPTED ]; then
    ok 04 "tests: acceptance ACCEPTED, current for this pack"
  elif [ "$AV" = CANNOT_RUN ]; then
    say 04 "weak" "tests: acceptance CANNOT_RUN - nothing observed the asks working; the owner's look at 05 is the only run there is"
  else
    fail 04 "tests: acceptance $AV - the blind gate said the asks do not work"
  fi
fi

# 05 Human - the red box
HUM="memory/stats/human.jsonl"
HLAST=""
[ -f "$HUM" ] && HLAST="$(grep -F "\"spec\":\"$SLUG\"" "$HUM" | tail -1)"
HEAD_NOW="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ -z "$HLAST" ]; then
  fail 05 "human: nobody has looked - no record in memory/stats/human.jsonl (scripts/human-check.sh after the owner answers)"
else
  HV="$(printf '%s' "$HLAST" | sed -n 's/.*"verdict":"\([^"]*\)".*/\1/p')"
  HP="$(printf '%s' "$HLAST" | sed -n 's/.*"pack":"\([^"]*\)".*/\1/p')"
  HH="$(printf '%s' "$HLAST" | sed -n 's/.*"head":"\([^"]*\)".*/\1/p')"
  HB="$(printf '%s' "$HLAST" | sed -n 's/.*"by":"\([^"]*\)".*/\1/p')"
  if [ "$HV" != ACCEPTED ]; then
    fail 05 "human: the owner REJECTED - route what they named into fix stories and look again"
  elif [ "$HP" != "$NOW_P" ]; then
    fail 05 "human: check is STALE (pack) - accepted $HP, now $NOW_P"
  elif [ "$HH" != "$HEAD_NOW" ] && ! paperwork_only "$ROOT" "$HH" "$HEAD_NOW"; then
    fail 05 "human: check is STALE (commit) - $HB looked at $HH, HEAD is $HEAD_NOW with code changed since; what they saw is not what ships"
  else
    ok 05 "human: ACCEPTED by $HB at $HH - this pack, this code (only cycle records landed since, if anything)"
  fi
fi

# 06 Ship - already done?
if [ -f "$PLAN" ] && [ -n "$(marker "$PLAN" Shipped)" ]; then
  say 06 "done" "ship: already recorded - $(marker "$PLAN" Shipped)"
fi

echo ""
if [ "$MISSING" -eq 0 ]; then
  echo "  READY. Every confirmation exists and is about this pack at this commit. /vulyk-ship may proceed."
else
  echo "  NOT READY - $MISSING open confirmation(s). The cycle is not at 06; the lines above say where it is."
fi
exit 0
