#!/usr/bin/env bash
# VULYK top-model resolver: which model is the king of planning on THIS account, right now.
#
#   scripts/top-model.sh            -> prints one alias: fable | opus
#   scripts/top-model.sh --explain  -> plan, the signal it was read from, and the reason
#   scripts/top-model.sh --apply    -> pins the resolved alias as "model" in .claude/settings.local.json
#                                      so the Queen's own session starts on it (next launch)
#   scripts/top-model.sh --check    -> exit 0 if settings.local.json pins the resolved alias, 1 if not
#
# The rule (v0.10.0): Fable 5.1 is the planning and orchestration model wherever the plan
# includes it, Opus 5 everywhere else. Per Anthropic's plan terms (Sept 2026) that means:
#
#   Max 5x / Max 20x, Team & Enterprise premium seats  -> fable   (up to half the weekly limit
#                                                                  is Fable at no extra cost)
#   Pro, Team standard seats, Enterprise standard seats -> opus    (Fable bills to usage credits
#                                                                  on top of the subscription)
#   API key, unknown, not signed in                     -> opus    (the safe floor; pin to change)
#
# Where the plan is read from: the profile Claude Code caches in ~/.claude.json
# (`oauthAccount.organizationType`, `.organizationRateLimitTier`, `.seatTier`). That file
# is Claude Code's own cache of the signed-in account - no token, no credential, nothing
# this script could misuse - and it is the only local place the plan is written down.
# The credentials file is never opened. VULYK touches configuration, not auth.
#
# Resolution order (first hit wins):
#   1. VULYK_TOP_MODEL=<alias>            env override, this shell only
#   2. `TOP_MODEL = <alias>` in CLAUDE.md  the constitution's pin; `auto` (the default) defers
#   3. the plan, as above
#   4. opus
#
# Fails open, always: any missing file, unreadable JSON or unknown plan resolves to `opus`
# and says why under --explain. A resolver that could break a session start is worse than
# one that picks the cheaper model.
#
#   VULYK_CLAUDE_CONFIG=/path/to/.claude.json   read the profile from here instead (tests, CI)
#   CLAUDE_CONFIG_DIR                            honoured, as Claude Code honours it
set -u

MODE="print"
case "${1:-}" in
  "")            MODE="print" ;;
  --explain)     MODE="explain" ;;
  --apply)       MODE="apply" ;;
  --check)       MODE="check" ;;
  -h|--help)     sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *)             echo "error: unknown flag $1 (try --help)" >&2; exit 2 ;;
esac

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# ---------------------------------------------------------------- the profile

profile_path() {
  if [ -n "${VULYK_CLAUDE_CONFIG:-}" ]; then printf '%s\n' "$VULYK_CLAUDE_CONFIG"; return; fi
  if [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ -f "$CLAUDE_CONFIG_DIR/.claude.json" ]; then
    printf '%s\n' "$CLAUDE_CONFIG_DIR/.claude.json"; return
  fi
  local home="${HOME:-}"
  [ -n "$home" ] && [ -f "$home/.claude.json" ] && { printf '%s\n' "$home/.claude.json"; return; }
  # Windows shells that reach here without HOME (rare, but a hook is not the place to find out)
  if [ -n "${USERPROFILE:-}" ]; then
    local up="${USERPROFILE//\\//}"
    [ -f "$up/.claude.json" ] && { printf '%s\n' "$up/.claude.json"; return; }
  fi
  printf '%s\n' "${home:-.}/.claude.json"
}

# field <file> <key> - the string value of "key": "..." anywhere in the file, or empty.
# grep/sed rather than jq or python: this runs on every session start, on machines where
# neither may be installed, and the fields it wants are flat strings under oauthAccount.
field() {
  grep -o -E "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$1" 2>/dev/null \
    | head -1 | sed -E 's/^"[^"]*"[[:space:]]*:[[:space:]]*"([^"]*)"$/\1/'
}

PROFILE="$(profile_path)"
PLAN="unknown"; SIGNAL=""; DETAIL=""
if [ -f "$PROFILE" ] && grep -q '"oauthAccount"' "$PROFILE" 2>/dev/null; then
  ORG="$(field "$PROFILE" organizationType)"
  TIER="$(field "$PROFILE" organizationRateLimitTier)"
  SEAT="$(field "$PROFILE" seatTier)"
  SIGNAL="organizationType=${ORG:-?}"
  [ -n "$TIER" ] && SIGNAL="$SIGNAL rateLimitTier=$TIER"
  [ -n "$SEAT" ] && SIGNAL="$SIGNAL seatTier=$SEAT"
  case "$ORG" in
    *max*)
      PLAN="max"
      case "$TIER" in
        *20x*) DETAIL="Max 20x" ;;
        *5x*)  DETAIL="Max 5x" ;;
        *)     DETAIL="Max" ;;
      esac ;;
    *pro*)   PLAN="pro";  DETAIL="Pro" ;;
    *team*|*enterprise*)
      # Premium seats carry Fable inside the plan; standard seats bill it to credits.
      case "$SEAT" in
        *premium*) PLAN="premium-seat"; DETAIL="${ORG} premium seat" ;;
        *)         PLAN="standard-seat"; DETAIL="${ORG} standard seat" ;;
      esac ;;
    "")      PLAN="unknown"; DETAIL="signed in, but no organizationType in the profile" ;;
    *)       PLAN="unknown"; DETAIL="unrecognised organizationType '$ORG'" ;;
  esac
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  PLAN="api"; SIGNAL="ANTHROPIC_API_KEY set"; DETAIL="API key - per-token billing, no plan"
else
  PLAN="unknown"; SIGNAL="no profile at $PROFILE"; DETAIL="not signed in, or Claude Code has not cached the account yet"
fi

# ---------------------------------------------------------------- resolution

constitution_pin() {
  local f
  for f in "$ROOT/CLAUDE.md" "$ROOT/CLAUDE.vulyk.md"; do
    [ -f "$f" ] || continue
    # `]` first and `[` inside the bracket so ERE reads them as members, not delimiters -
    # the alias may carry a `[1m]` suffix.
    grep -m1 -o -E 'TOP_MODEL[[:space:]]*=[[:space:]]*`?[A-Za-z0-9][][A-Za-z0-9._-]*' "$f" 2>/dev/null \
      | sed -E 's/.*=[[:space:]]*`?//' | head -1
    return
  done
}

SOURCE=""; MODEL=""; REASON=""
if [ -n "${VULYK_TOP_MODEL:-}" ]; then
  MODEL="$VULYK_TOP_MODEL"; SOURCE="env"; REASON="VULYK_TOP_MODEL is set in this shell"
else
  PIN="$(constitution_pin)"
  if [ -n "$PIN" ] && [ "$PIN" != "auto" ]; then
    MODEL="$PIN"; SOURCE="constitution"; REASON="CLAUDE.md pins TOP_MODEL = $PIN"
  else
    SOURCE="plan"
    case "$PLAN" in
      max|premium-seat)
        MODEL="fable"; REASON="$DETAIL - Fable 5.1 is inside the plan (up to half the weekly limit at no extra cost)" ;;
      pro|standard-seat)
        MODEL="opus";  REASON="$DETAIL - Fable bills to usage credits on top of the subscription; Opus 5 is the frontier model the plan includes" ;;
      api)
        MODEL="opus";  REASON="$DETAIL - Opus 5 is the floor; pin TOP_MODEL = fable in CLAUDE.md to spend on Fable per token" ;;
      *)
        MODEL="opus";  REASON="$DETAIL - defaulting to the safe floor; pin TOP_MODEL in CLAUDE.md or set VULYK_TOP_MODEL" ;;
    esac
  fi
fi

label() { # label <alias> - human name for the brief
  case "$1" in
    fable) echo "Fable 5.1" ;;
    opus)  echo "Opus 5" ;;
    *)     echo "$1" ;;
  esac
}

# The Tier 4 second reviewer must be a DIFFERENT model from lead-review, and one the plan
# carries without credits: the other frontier alias where both are in the plan, sonnet
# where only Opus is.
second_reviewer() {
  case "$1" in
    fable) echo "opus" ;;
    opus)  case "$PLAN" in max|premium-seat) echo "fable" ;; *) echo "sonnet" ;; esac ;;
    *)     echo "opus" ;;
  esac
}

# ---------------------------------------------------------------- the local pin

LOCAL="$ROOT/.claude/settings.local.json"
pinned_model() { [ -f "$LOCAL" ] && field "$LOCAL" model; }

# ---------------------------------------------------------------- modes

case "$MODE" in
  print)
    printf '%s\n' "$MODEL" ;;

  explain)
    echo "top model : $MODEL ($(label "$MODEL"))"
    echo "decided by: $SOURCE - $REASON"
    echo "plan      : $PLAN${DETAIL:+ ($DETAIL)}"
    echo "signal    : ${SIGNAL:-none}"
    echo "profile   : $PROFILE"
    echo "second reviewer (Tier 4): $(second_reviewer "$MODEL")"
    P="$(pinned_model)"
    if [ -z "$P" ]; then
      echo "queen session: not pinned - the session starts on the account default (Sonnet 5 on Pro, Opus 5 on Max). Run: bash scripts/top-model.sh --apply"
    elif [ "$P" = "$MODEL" ]; then
      echo "queen session: pinned $P in .claude/settings.local.json"
    else
      echo "queen session: pinned $P in .claude/settings.local.json, resolver says $MODEL - re-run --apply, or keep the pin deliberately"
    fi ;;

  check)
    P="$(pinned_model)"
    [ "$P" = "$MODEL" ] && exit 0
    exit 1 ;;

  apply)
    P="$(pinned_model)"
    if [ "$P" = "$MODEL" ]; then
      echo "already pinned: model = $MODEL in $LOCAL"
      exit 0
    fi
    mkdir -p "$ROOT/.claude" 2>/dev/null || true
    if [ ! -f "$LOCAL" ]; then
      printf '{\n  "model": "%s"\n}\n' "$MODEL" > "$LOCAL" || { echo "error: cannot write $LOCAL" >&2; exit 1; }
      echo "pinned: model = $MODEL -> created $LOCAL (takes effect on the next launch; /model $MODEL now if this session must have it)"
      exit 0
    fi
    # Existing file: it carries the owner's permissions and standing approvals, so merge one
    # key and touch nothing else. Python is what the framework already depends on for
    # handoff.py; without it, say exactly what to add rather than rewriting JSON with sed.
    PY="$(command -v python3 || command -v python || command -v py || true)"
    if [ -z "$PY" ]; then
      echo "cannot merge without python: add  \"model\": \"$MODEL\"  to $LOCAL by hand"
      exit 1
    fi
    if "$PY" - "$LOCAL" "$MODEL" <<'PYPIN'
import json, sys
path, model = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(4)
if not isinstance(data, dict):
    sys.exit(4)
data["model"] = model
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PYPIN
    then
      echo "pinned: model = $MODEL in $LOCAL (was: ${P:-unset}; takes effect on the next launch)"
    else
      echo "cannot parse $LOCAL as JSON - left untouched; add  \"model\": \"$MODEL\"  by hand"
      exit 1
    fi ;;
esac
exit 0
