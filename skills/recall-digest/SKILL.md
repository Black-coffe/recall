---
name: recall-digest
description: Use when the user wants a Recall brief run now — the weekly digest, dropped commitments, open questions or another configured pipe — sent to its fixed recipient, through the recall MCP tools pipes, get_job_status, automation_status and list_pipe_runs. Triggers — RU: пришли брифинг, запусти дайджест, сводка за неделю, что упустили пришли, запусти пайп; UK: надішли брифінг, запусти дайджест, звід за тиждень, запусти пайп; EN: send the brief, run the digest, weekly digest now, run the pipe.
argument-hint: "[pipe name, default weekly-digest]"
allowed-tools: mcp__recall__automation_status mcp__recall__list_pipe_runs mcp__recall__get_job_status
---

# Recall: run a brief now

`pipes` is a WRITE tool and PAID (a brief may call Claude). It needs Recall running and the `full`
MCP profile. Pipe to run: $ARGUMENTS (default `weekly-digest`).

1. **Check the name.** `automation_status(sections=["pipes"])` lists the configured pipes and their
   last runs. An unknown name: show the list and ask, do not guess.
2. **Run.** `pipes(action="run", name="<pipe>", wait_seconds=25)` answers `{job_id, created}`. The
   brief goes to the pipe's fixed recipient; this tool cannot change the recipient or the schedule.
3. **Follow.** `get_job_status(job_id, wait_seconds=25)` until `state` is `completed` or `failed`
   (stop after about 20 polls and give the `job_id`). Report what was delivered from `result`.
4. **History.** `list_pipe_runs` shows earlier runs when the user asks whether a brief went out.

Refusals: `COOLDOWN` (one run per 10 minutes, `retry_after_sec` says when), `ALREADY_RUNNING`,
`PIPES_DISABLED` (the scheduler is off), `NOT_CONFIGURED` (no delivery bot), `NOT_FOUND`.

The same run as an MCP command: `/mcp__recall__post_digest [pipe]`.
