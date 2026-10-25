#!/usr/bin/env bash
# VULYK model profile: which family takes each role on THIS machine (model-profiles, ADR-020).
#
#   scripts/model-profile.sh                 -> prints the active profile: opus-builds | sonnet-builds | economy
#   scripts/model-profile.sh --chosen        -> exit 0 if the file holds a known profile, 1 if not; prints nothing
#   scripts/model-profile.sh --set <name>    -> stores <name> in the profile file and prints it
#   scripts/model-profile.sh --role <role>   -> prints one family alias for the role under the active profile
#   scripts/model-profile.sh --recommend     -> prints "<profile> <plan>", from scripts/top-model.sh --plan
#   scripts/model-profile.sh --explain [<n>] -> who does what under the active (or the named) profile
#
# Profiles, named by family only:
#   sonnet-builds  "как сейчас": Sonnet writes, Opus judges, Fable holds the gate where the plan has it (the default)
#   opus-builds    "Opus пишет, Fable судит": Opus writes, Haiku writes routine, Fable plans, judges and holds the gate
#   economy        no Fable: Sonnet writes, Haiku writes routine, Opus judges and holds the gate
#
# Roles: queen planner review seat worker routine gate architect review-second.
# `gate`, `architect` and `review-second` are what scripts/top-model.sh resolves for the active profile, so the env
# and constitution pins still win there.
#
# The choice is one word in ${CLAUDE_CONFIG_DIR:-<home>/.claude}/vulyk-model-profile, one file per machine that no
# installer or upgrade touches. This script is its only reader, and only --set writes it. No file, or a file that
# holds anything but a known name, reads as sonnet-builds (fail open) and --chosen exits 1, so the quiz
# (/vulyk-models) is offered again. Exit 2 on usage errors.
set -u

HERE="$(dirname "$0")"
TOP="$HERE/top-model.sh"
PROFILES="opus-builds sonnet-builds economy"
ROLES="queen planner review seat worker routine gate architect review-second"

usage() { echo "error: $1 (try --help)" >&2; exit 2; }

# <home> falls back from HOME to USERPROFILE, as top-model.sh's profile_path does.
home_dir() {
  local h="${HOME:-}"
  [ -n "$h" ] || h="${USERPROFILE:-}"
  h="${h//\\//}"
  printf '%s' "${h:-.}"
}
CONF_DIR="${CLAUDE_CONFIG_DIR:-$(home_dir)/.claude}"
CONF_DIR="${CONF_DIR//\\//}"
FILE="$CONF_DIR/vulyk-model-profile"

known() { [ -n "$1" ] && case " $PROFILES " in *" $1 "*) return 0 ;; esac; return 1; }

# stored - the known name the file holds (whitespace and CR ignored), else nothing and exit 1
stored() {
  [ -f "$FILE" ] || return 1
  local v
  v="$(tr -d '[:space:]' < "$FILE" 2>/dev/null)"
  known "$v" || return 1
  printf '%s\n' "$v"
}
active() { stored || echo "sonnet-builds"; }

# plan_info - "<plan> <premium|standard>" from top-model.sh --plan; "unknown standard" without it
plan_info() {
  local p=""
  [ -f "$TOP" ] && p="$(bash "$TOP" --plan 2>/dev/null | head -1)"
  [ -n "$p" ] || p="unknown standard"
  printf '%s\n' "$p"
}

# role_of <profile> <role> - the family. The shared roles ask top-model.sh when <profile> is the active one; for
# another profile they show what top-model.sh would resolve with no pin.
role_of() {
  local prof="$1" role="$2"
  case "$role" in
    queen) echo "opus" ;;
    planner|review|seat) [ "$prof" = "opus-builds" ] && echo "fable" || echo "opus" ;;
    worker)  [ "$prof" = "opus-builds" ] && echo "opus" || echo "sonnet" ;;
    routine) [ "$prof" = "sonnet-builds" ] && echo "sonnet" || echo "haiku" ;;
    gate|architect|review-second)
      if [ "$prof" = "$(active)" ] && [ -f "$TOP" ]; then
        if [ "$role" = "review-second" ]; then
          bash "$TOP" --explain 2>/dev/null | sed -n 's/^second reviewer (Tier 4): //p' | head -1
        else
          bash "$TOP" 2>/dev/null | head -1
        fi
        return
      fi
      local tier g
      tier="$(plan_info)"; tier="${tier#* }"
      case "$prof" in
        opus-builds) g="fable" ;;
        economy)     g="opus" ;;
        *)           [ "$tier" = "premium" ] && g="fable" || g="opus" ;;
      esac
      if [ "$role" != "review-second" ]; then echo "$g"
      elif [ "$prof" != "sonnet-builds" ]; then echo "sonnet"
      elif [ "$g" = "fable" ]; then echo "opus"
      else echo "sonnet"
      fi ;;
    *) return 2 ;;
  esac
}

does() { # does <role> - what the role is, for --explain
  case "$1" in
    queen)         echo "the main session: plans, builds Tier 0-2 work, dispatches, integrates" ;;
    planner)       echo "queen-planner on Tier 3 plans" ;;
    review)        echo "lead-review at Tier 1-3: judges the code" ;;
    seat)          echo "the blind council seats (council-opus, council-haiku)" ;;
    worker)        echo "story code: new work and logic" ;;
    routine)       echo "stories the planner marks routine or reusable" ;;
    gate)          echo "the Tier 4 lead-review, a Tier 4 queen-planner, a missed story's retry" ;;
    architect)     echo "lead-architect at Tier 4" ;;
    review-second) echo "the Tier 4 second reviewer" ;;
  esac
}

case "${1:-}" in
  "")
    [ $# -le 1 ] || usage "no argument expected after the profile query"
    active ;;
  --chosen)
    stored >/dev/null && exit 0
    exit 1 ;;
  --set)
    [ $# -eq 2 ] || usage "--set takes one name: $PROFILES"
    known "$2" || usage "unknown profile '$2': $PROFILES"
    mkdir -p "$CONF_DIR" 2>/dev/null || { echo "error: cannot create $CONF_DIR" >&2; exit 1; }
    printf '%s\n' "$2" > "$FILE" || { echo "error: cannot write $FILE" >&2; exit 1; }
    echo "$2" ;;
  --role)
    [ $# -eq 2 ] || usage "--role takes one role: $ROLES"
    case " $ROLES " in *" $2 "*) ;; *) usage "unknown role '$2': $ROLES" ;; esac
    role_of "$(active)" "$2" ;;
  --recommend)
    P="$(plan_info)"
    case "$P" in *" premium") echo "opus-builds ${P%% *}" ;; *) echo "sonnet-builds ${P%% *}" ;; esac ;;
  --explain)
    [ $# -le 2 ] || usage "--explain takes at most one name"
    NAME="${2:-$(active)}"
    known "$NAME" || usage "unknown profile '$NAME': $PROFILES"
    if [ -n "${2:-}" ] && [ "$2" != "$(active)" ]; then STATE="not active; active: $(active)"
    elif stored >/dev/null; then STATE="chosen in $FILE"
    else STATE="not chosen - the default; choose with /vulyk-models"
    fi
    echo "profile : $NAME ($STATE)"
    for r in $ROLES; do
      printf '  %-14s %-7s %s\n' "$r" "$(role_of "$NAME" "$r")" "$(does "$r")"
    done
    [ "$NAME" = "$(active)" ] || echo "  (gate, architect, review-second as resolved with no VULYK_TOP_MODEL or TOP_MODEL pin)" ;;
  -h|--help)
    sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//' ;;
  *)
    usage "unknown flag $1" ;;
esac
exit 0
