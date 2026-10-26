# Claude Code hook recipes for Recall

Copy-paste examples for wiring Claude Code [hooks](https://code.claude.com/docs/en/hooks) to Recall.
Nothing here is installed by Recall: you put a recipe into a `settings.json` yourself (`~/.claude/settings.json`
for every project, `.claude/settings.json` for one).

Checked against the Claude Code docs on 2026-10-26: the `hooks → <Event> → [{matcher, hooks: [handler]}]` layout,
the handler types `command`, `http` and `mcp_tool`, `allowedEnvVars`, the `SessionStart`/`SessionEnd` matchers and
the `UserPromptSubmit` `decision: "block"` output (code.claude.com/docs/en/hooks), and the `ask` rule syntax
(code.claude.com/docs/en/permissions).

## Two rules for every recipe

- **A hook is never a safety gate.** A refused or failed HTTP request, a server that is not connected, or a
  tool that returns an error is a *non-blocking* error in Claude Code: the session goes on. Recall's own checks
  are the gate, not the hook.
- **Destructive actions still raise the Windows dialog.** Recall sends every D action (delete, merge, stop a
  recording, cancel one, disconnect a mailbox, remove a key) to a native Windows dialog on the machine, whoever
  calls it: the UI, MCP, a hook, `curl`. No header, key or hook skips it (ADR-042 §11). A hook that would need one
  waits for you; unattended, it fails closed.

Recall answers only on `127.0.0.1:5050` by default. A request from this machine needs no key; a hook is treated
like any non-UI caller (an agent), so a recording it starts always shows the Recall toast first.

## 1. Start a recording when a session opens (`http`)

For a dedicated folder (say, meeting notes), opening Claude Code there starts a Recall recording.
Put it into that folder's `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:5050/api/recording/start",
            "headers": { "X-Recall-Actor": "hook" },
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

- `matcher: "startup"` keeps it to a new session; without it `resume`, `clear`, `compact` and `fork` would fire
  it too.
- An `http` hook always POSTs the event's own JSON as the body, and that body names no audio source. Recall
  then records **the Windows default microphone only, no system sound**, 180 minutes at most: enough for
  dictation, not for a call, where the other side would be missing. `X-Recall-Actor` only labels the recording
  (`started_by: hook`); it proves nothing.
- Recall shows its toast first. When no toast can be shown, nothing starts (`NOTIFY_UNAVAILABLE`). Each matching
  start opens one more recording, up to Recall's limit of parallel recordings (then `SESSION_LIMIT`).

**For a call, use a `command` hook and name the sources.** The default microphone plus all system sound:

```json
{
  "type": "command",
  "command": "curl -s -X POST http://127.0.0.1:5050/api/recording/start -H \"Content-Type: application/json\" -H \"X-Recall-Actor: hook\" -d \"{\\\"mic\\\": \\\"default\\\", \\\"system_source\\\": {\\\"mode\\\": \\\"all\\\"}, \\\"language\\\": \\\"uk\\\"}\""
}
```

One application's sound only: replace the `system_source` value with `{"mode": "include", "apps": ["zoom.exe"]}`.
A specific microphone: replace `"mic": "default"` with `"mic_device_index": <n>`; the index comes from
`recording_devices(kind="audio")` (MCP) and can change when a device is plugged in again, so the Windows
default is the steadier choice. Stopping is not a hook: type `/recall-record-stop` or press Stop in Recall.

## 2. Send the brief when the session ends (`mcp_tool`)

Runs the `weekly-digest` pipe through the `recall` MCP server when you close the session:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "matcher": "prompt_input_exit|logout|other",
        "hooks": [
          {
            "type": "mcp_tool",
            "server": "recall",
            "tool": "pipes",
            "input": { "action": "run", "name": "weekly-digest" },
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

- `SessionEnd`, not `Stop`: `Stop` fires every time Claude finishes a reply, which would ask for a brief (a PAID
  run) after each answer. Recall's 10-minute cooldown would refuse most of them, but the intent is "when I
  finish". The matcher leaves out `clear` and `resume`, which only switch sessions.
- `SessionEnd` hooks share a 1.5-second budget unless a hook sets a longer `timeout` (up to 60 s). `pipes`
  answers with a `job_id` at once; the run itself goes on in Recall.
- `SessionEnd` is observational: if the `recall` server is not connected at that moment, the hook is skipped
  with a non-blocking error.
- The brief goes to the pipe's fixed recipient. The same run by hand: `/mcp__recall__post_digest`.

## 3. Stop a pasted secret before it reaches the model (`UserPromptSubmit`, optional)

Recall's MCP tools already refuse arguments shaped like an app password or an Anthropic key
(`SECRET_IN_ARGUMENT`). This hook stops such a prompt earlier, before the model sees it. It reuses Recall's
detector (`app/services/secret_shape.py`, standard library only). Replace `<recall-dir>` with your Recall folder:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python -I -c \"import json,sys; sys.path.insert(0, r'<recall-dir>'); from app.services.secret_shape import looks_like_secret; p=json.load(sys.stdin).get('prompt',''); sys.stdout.write(json.dumps({'decision':'block','reason':'This looks like a password or an API key. Enter secrets only on the Recall connect page.'}) if looks_like_secret(p, strict=False) else '')\""
          }
        ]
      }
    ]
  }
}
```

- `strict=False` checks only the grouped app-password shape (`abcd efgh ijkl mnop`) and the `sk-ant-` prefix; the
  bare 16-letter rule would block ordinary long words in a prompt.
- The hook prints nothing for a normal prompt, so it adds nothing to the context.

## 4. Ask before `curl` reaches Recall (`ask` rule)

A permission rule, not a hook: Claude Code asks you before the model runs `curl` against Recall's port.

```json
{
  "permissions": {
    "ask": ["Bash(curl *127.0.0.1:5050*)", "Bash(curl *localhost:5050*)"]
  }
}
```

- This is a seatbelt only. The Claude Code docs warn that argument patterns are fragile: a variable, a redirect,
  a full path to `curl`, `sh -c` or another tool (`wget`, PowerShell) does not match. Recall's own checks still
  apply to whatever gets through.
- It does not affect hooks: hooks run outside the model's tool calls.

## See also

- `docs/MCP_SETUP.md`, section "Registrations": the three MCP connections, the skills and the MCP command.
- ADR-042: the read/write/destructive model and the Windows dialog.
