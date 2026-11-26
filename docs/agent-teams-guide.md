# Agent Teams — Master Reference Guide

> Source: <https://code.claude.com/docs/en/agent-teams> (read 2026-11-26).
> Agent teams are **experimental** and off by default. Version-specific details
> below (e.g. `v2.1.257+`) may drift; re-check the source page when something
> behaves differently.

This guide has two halves:

1. **Playbook** — how to decide on, design, prompt, run, and finish a team well.
2. **Reference** — the mechanics: settings, keys, storage, permissions, hooks, limits.

---

## Part 1 — Playbook

### 1.1 Decide: team, subagents, or single session?

| Situation | Use |
| :- | :- |
| Quick focused worker; only the result matters | **Subagent** |
| Workers must share findings, challenge each other, self-coordinate | **Agent team** |
| Sequential steps, same-file edits, heavy inter-task dependencies | **Single session** (or subagents) |
| Routine task | **Single session** — cheapest |
| Passing findings between sessions you run yourself | **Cross-session messaging** |

| | Subagents | Agent teams |
| :- | :- | :- |
| Context | Own window; results return to caller | Own window; fully independent |
| Communication | Return result to caller (named subagents can message each other) | Teammates message each other directly |
| Coordination | Main agent manages all work | Self-coordination via messages + shared task list |
| Best for | Focused tasks where only the result matters | Complex work needing discussion/collaboration |
| Token cost | Lower (results summarized back) | Higher (each teammate is a full Claude instance) |

**Strongest team use cases:**
- **Research & review** — parallel angles, then cross-examination.
- **New modules/features** — each teammate owns a separate piece.
- **Debugging with competing hypotheses** — parallel theories, adversarial disproof; beats anchoring.
- **Cross-layer work** — frontend / backend / tests each owned by one teammate.

**Red flags (don't use a team):** tasks that touch the same files, strict
sequencing, lots of dependencies, or work small enough that coordination
overhead exceeds the gain.

### 1.2 Design the team

**Size.** Start with **3–5 teammates**. Token cost scales linearly; coordination
cost grows faster; returns diminish. For ~15 independent tasks, 3 teammates is a
good start. *"Three focused teammates often outperform five scattered ones."*

**Tasks.** Aim for **5–6 tasks per teammate** — enough to stay busy and let the
lead reassign if someone stalls.
- Too small → overhead > benefit.
- Too large → long stretches without check-ins, wasted effort.
- Right size → self-contained unit with a clear deliverable (a function, a test
  file, a review).

**File ownership.** Give each teammate a **disjoint set of files**. Two
teammates editing one file = overwrites. (Consider git worktrees for
heavier isolation — see related docs.)

**Roles.** Define distinct lenses, not duplicate workers. Good patterns:
- Reviewer split: security / performance / test coverage.
- Design split: UX / architecture / devil's advocate.
- Debug split: one teammate per hypothesis, told to disprove the others.
- Build split: one teammate per layer or module.

**Names.** Tell the lead what to call each teammate — names are message
addresses and are used for shutdown ("Ask the researcher teammate to shut down").

**Models.** Name the model in the spawn prompt ("Use Sonnet for each teammate").
Cheaper models for mechanical/parallel work; the lead's model for judgment-heavy
roles.

**Reusable roles.** Put recurring roles in subagent definitions
(`.claude/agents/*.md`) and spawn by type: *"Spawn a teammate using the
security-reviewer agent type to audit the auth module."* Do **not** try to
author team config files — they're runtime state (see 2.6).

### 1.3 Write the spawn prompt

Teammates get CLAUDE.md, MCP servers, and skills — but **not the lead's
conversation history**. The spawn prompt is everything they know about the task.

Checklist for each teammate's prompt:
- [ ] **Goal & deliverable** — what "done" looks like, in what format.
- [ ] **Scope** — exact paths/modules they own; what's off-limits.
- [ ] **Key context** — facts the lead learned that they'd otherwise rediscover
      (stack, conventions, constraints, prior findings).
- [ ] **Focus areas** — what to look for / prioritize.
- [ ] **Collaboration instructions** — who to message, when, and about what
      (e.g. "challenge the architecture teammate's assumptions").
- [ ] **Reporting** — severity ratings, file:line refs, summary length.

Example from the docs:
```text
Spawn a security reviewer teammate with the prompt: "Review the authentication module
at src/auth/ for security vulnerabilities. Focus on token handling, session
management, and input validation. The app uses JWT tokens stored in
httpOnly cookies. Report any issues with severity ratings."
```

### 1.4 Prompt templates

**Exploration / design**
```text
I'm designing a CLI tool that helps developers track TODO comments across
their codebase. Spawn three teammates to explore this from different angles:
one on UX, one on technical architecture, one playing devil's advocate.
```

**Parallel code review**
```text
Spawn three teammates to review PR #142:
- One focused on security implications
- One checking performance impact
- One validating test coverage
Have them each review and report findings.
```

**Competing-hypotheses debugging**
```text
Users report the app exits after one message instead of staying connected.
Spawn 5 agent teammates to investigate different hypotheses. Have them talk to
each other to try to disprove each other's theories, like a scientific
debate. Update the findings doc with whatever consensus emerges.
```

**Parallel refactor with model choice**
```text
Spawn 4 teammates to refactor these modules in parallel. Use Sonnet for
each teammate.
```

**Plan-first for risky work** (put the lead in plan mode first)
```text
Spawn an architect teammate to refactor the authentication module.
```

**Useful nudges**
- `Wait for your teammates to complete their tasks before proceeding`
- "Split the work into smaller tasks" (if the lead under-decomposes)
- "Keep going — not all tasks are complete" (if the lead stops early)
- "Spawn a replacement for <name>" (if a teammate dies on errors)

> If Claude spawns subagents instead of a team, ask again and **explicitly**
> request an *agent team*. The agent panel shows both, so it alone doesn't
> prove a team formed.

### 1.5 Run the team

1. **Pre-approve permissions** for common operations *before* spawning —
   teammate prompts bubble up to the lead and can swamp you.
2. **Pick risk controls**: plan mode for risky changes; quality-gate hooks
   (`TaskCreated`, `TaskCompleted`, `TeammateIdle`) for enforced standards.
3. **Monitor and steer.** Check the task list (Ctrl+T), open transcripts,
   redirect failing approaches early. Unattended teams waste tokens.
4. **Keep the lead coordinating, not implementing.** If it starts doing the
   work itself, tell it to wait.
5. **Watch for stale task status** — teammates sometimes forget to mark tasks
   complete, which blocks dependents. Verify and fix manually or nudge.
6. **Synthesize** — the lead gathers each teammate's final answer (delivered
   with the idle notification) and reconciles conflicts.

### 1.6 Finish cleanly

- Shut down teammates by name; they may reject with an explanation, and
  shutdown waits for the current request/tool call.
- Check for orphaned tmux sessions (`tmux ls`, `tmux kill-session -t <name>`).
- Remember: `/resume` and `/rewind` do **not** restore in-process teammates.
  Finish team work within one session where possible.

### 1.7 Getting-started advice

Start with **no-code, clear-boundary** tasks: reviewing a PR, researching a
library, investigating a bug. Move to parallel implementation once the
coordination patterns feel predictable.

---

## Part 2 — Reference

### 2.1 Enabling

```json
// settings.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```
Or export `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in the shell.

Side effects:
- While enabled, **any named subagent launches as a teammate** — teams can form
  without being asked for. A teammate launches when the Agent tool is called
  with a `name` (excluding forks and calls passing `isolation`). No confirmation
  prompt.
- Teammates require an **interactive** session. With `-p` / Agent SDK, named
  subagents run as ordinary subagents.

To disable (so named subagents stay subagents), set the value to `"0"`. Saving
the settings file reapplies `env` without restarting. User-level `0` overrides a
shell export, but project/local/`--settings`/managed settings applied later can
re-enable it.

### 2.2 Architecture

| Component | Role |
| :- | :- |
| Team lead | Main session; spawns teammates, coordinates, synthesizes |
| Teammates | Separate Claude Code instances working on tasks |
| Task list | Shared work items that teammates claim and complete |
| Mailbox | Messaging between agents |

### 2.3 Display modes

| Mode | How | Requirement |
| :- | :- | :- |
| In-process (default) | All teammates inside the main terminal | Any terminal |
| Split panes | One pane per teammate | tmux or iTerm2 |

`~/.claude/settings.json`:
```json
{ "teammateMode": "auto" }
```
- `"auto"` — split panes inside tmux/iTerm2, in-process elsewhere.
- `"tmux"` — panes in a separate tmux session (copy the `View teammates`
  command to watch from another terminal).
- `"iterm2"` — panes in iTerm2 (needs `it2` CLI + **iTerm2 > Settings >
  General > Magic > Enable Python API**).
- Per session: `claude --teammate-mode auto` (experimental; hidden from `--help`).
- `tmux -CC` in iTerm2 is the suggested tmux entrypoint.

### 2.4 Keys (in-process agent panel)

| Key | Action |
| :- | :- |
| ↑ / ↓ | Select teammate |
| Enter | Open transcript / message teammate; expands `N idle agents` row |
| Esc | Clear selection; while viewing a teammate, interrupt its turn; collapse idle row |
| x | Stop selected teammate |
| Ctrl+T | Toggle task list |
| Shift+Tab | Cycle permission mode (of the teammate being viewed) |

Panel behavior: idle rows hide 30s after *all* agents are idle (still running,
still addressable). More than three idle → extras collapse into one count row.

While viewing a teammate: plain text and skills go to the teammate; built-in
commands go to the lead. `/compact`, `/clear`, `/rewind` ask for confirmation
(they act on the lead). `/model` and `/fast` don't run (a teammate's model and
fast mode are fixed at spawn).

### 2.5 Tasks & messaging

- States: **pending → in progress → completed**; tasks with unfinished
  dependencies show **blocked**.
- Teammates don't self-claim blocked tasks; the lead or you can still assign.
- After finishing, a teammate self-claims the next unassigned, unblocked task.
- Claiming uses **file locking** to prevent races.
- Completing a task auto-unblocks its dependents.
- Agents without Task tools coordinate via messages only.
- Messages are delivered automatically; **no broadcast** — one message per
  recipient, by name.
- Idle notification to lead includes the teammate's final answer (or the error
  text if its turn ended on an API error).
- A send succeeds only if the write to the recipient's mailbox file succeeds;
  failures return an error to the sender.
- Messaging a teammate that has stopped resumes it in the same session (restores
  its conversation; delivers the message as its next prompt) — but not after
  `/resume` of the whole session.

### 2.6 Storage

Team name = `session-` + first 8 chars of the session ID.

| What | Path | Lifetime |
| :- | :- | :- |
| Team config | `~/.claude/teams/{team-name}/config.json` | Removed when session ends |
| Mailboxes | `~/.claude/teams/{team-name}/inboxes/{agent-name}.json` | With team dir |
| Task list | `~/.claude/tasks/{team-name}/` | Persists; governed by `cleanupPeriodDays` |

- Config holds runtime state (session IDs, tmux pane IDs). **Don't edit or
  pre-author it**; changes get overwritten.
- `members` array: name + agent ID; lead has agent type `team-lead`. Teammates
  can read it to discover each other.
- No project-level team config exists; `.claude/teams/teams.json` in a repo is
  just a file.

### 2.7 Subagent definitions as teammate types

Spawnable from project, user, managed, or plugin agent scopes. How fields apply:

| Field | In-process teammate | Split-pane teammate |
| :- | :- | :- |
| `tools` | Restricted to list, **plus** `SendMessage` and Task tools | Restricted to list |
| `disallowedTools` | Removed (SendMessage/Task tools stay) | — |
| `model` | Used if spawn prompt names none | Same |
| `effort` | Applied | — |
| `autoCompactWindow` | Applied (v2.1.296+) | Ignored |
| Body | **Appended** to default system prompt | **Replaces** default system prompt |
| `skills` | Not applied (loads from project/user settings) | Not applied |
| `mcpServers` | Ignored (loads project/user MCP) | Applied |

Project `.claude/agents/` definitions are re-applied to a resumed teammate only
if that exact folder is trusted.

### 2.8 Model selection (first match wins)

1. Model named in the spawn prompt.
2. Subagent definition's `model` (`inherit` = lead's model).
3. `CLAUDE_CODE_SUBAGENT_MODEL` (if not `inherit`).
4. Lead's current model.

- A mod's `agent.spawn` hook can override #1.
- `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` skips #1 and #2 (v2.1.257+).
- `teammateDefaultModel` was removed (v2.1.234) — ignored if present.
- `availableModels` allowlist: blocked family alias → newest permitted version
  of that family (Anthropic API / Claude Platform on AWS); other blocked values
  → lead's model (trying `CLAUDE_CODE_SUBAGENT_MODEL` first if set).
- Teammates on the lead's model inherit the lead's effort level.

### 2.9 Permissions

- New teammates start in the **lead's mode** (including `bypassPermissions`).
  Lead in `dontAsk` → teammate starts in Manual. No per-teammate mode at spawn.
- While the lead is in `acceptEdits`, `auto`, or `bypassPermissions`, an
  in-process teammate (not in plan mode) runs in the lead's mode even if you set
  it to Manual. To make it ask, switch the **lead** to Manual (Enter on the
  `main` row).
- Teammate permission prompts surface in the lead session.
- Rule propagation:
  - In-process: lead's live allow/deny rules; `--allowedTools`,
    `--disallowedTools`, `--tools` all apply.
  - Split-pane: `--disallowedTools` and `--tools` pass through (v2.1.309+);
    `--allowedTools` does **not**. Pre-approve via
    `claude --settings '{"permissions":{"allow":["Bash(npm *)"]}}'`.
- **Trust boundary:** `SendMessage` content is presented as coming from another
  Claude session, not the user. Teammates can't approve prompts or give consent
  on the user's behalf, and a denied action can't be laundered through another
  teammate. In auto mode the classifier reviews messages before delivery and
  treats relayed approval claims as untrusted.

### 2.10 Plan approval

- Teammate spawned while the lead is in plan mode works in plan mode until its
  plan is ready, then sends a plan-approval request.
- If you haven't changed that teammate's mode since spawn, the plan is
  **auto-approved** on arrival (no lead review).
- If you Shift+Tab a running in-process teammate into plan mode, the **lead**
  reviews that plan (approve or send back). You aren't asked.
- After approval the teammate takes the lead's current mode (Manual if the lead
  is in plan mode).

### 2.11 Quality-gate hooks

| Hook | Fires when | Exit code 2 does |
| :- | :- | :- |
| `TeammateIdle` | Teammate about to go idle | Sends feedback, keeps it working |
| `TaskCreated` | Task being created | Blocks creation, sends feedback |
| `TaskCompleted` | Task being marked complete | Blocks completion, sends feedback |

Use these to enforce "tests pass before complete", "task has a clear
deliverable", "don't stop with unclaimed tasks", etc. Details:
<https://code.claude.com/docs/en/hooks.md>.

### 2.12 Context, cost, caching

- Each teammate loads CLAUDE.md, MCP servers, and skills like a fresh session.
  `--setting-sources` on the lead restricts teammates too.
- Tokens scale with active teammates. Worth it for research, review, and new
  features; not for routine work. See
  <https://code.claude.com/docs/en/costs.md#agent-team-token-costs>.
- In-process teammate prompt cache defaults to 5 min; set
  `subagentPromptCacheTtl` to `1h` for longer (1h cache writes bill higher).

### 2.13 Limitations

1. `/resume` and `/rewind` don't restore in-process teammates — spawn new ones.
2. Task status can lag and block dependents.
3. Shutdown can be slow (finishes current request/tool call).
4. One team per session; can't share across sessions.
5. No nested teams — only the lead spawns teammates.
6. In-process teammates can't run background subagents (`background: true`
   errors; `run_in_background` errors or silently runs in foreground).
7. Lead is fixed for the session's lifetime.
8. No per-teammate permission modes at spawn.
9. Split panes need tmux or iTerm2.

### 2.14 Troubleshooting

| Symptom | Fix |
| :- | :- |
| Teammates not visible | Check agent panel below prompt; hidden idle rows reappear when messaged; confirm task warranted a team; `which tmux`; for iTerm2 verify `it2` + Python API |
| Got teammates, wanted subagents | Set `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` to `"0"` |
| Got subagents, wanted a team | Re-ask explicitly for an "agent team" |
| Too many permission prompts | Pre-approve common ops before spawning |
| Teammate stopped on errors | Inspect transcript; give instructions or spawn replacement; a message wakes a teammate waiting on API retry |
| Lead quit early | Tell it to keep going |
| Lead implementing instead of coordinating | "Wait for your teammates to complete their tasks before proceeding" |
| Orphaned tmux session | `tmux ls` → `tmux kill-session -t <name>` |

---

## Appendix — Related docs

- Subagents: <https://code.claude.com/docs/en/sub-agents.md>
- Cross-session messaging: <https://code.claude.com/docs/en/cross-session-messaging.md>
- Git worktrees: <https://code.claude.com/docs/en/worktrees.md>
- Hooks: <https://code.claude.com/docs/en/hooks.md>
- Permissions / modes: <https://code.claude.com/docs/en/permissions.md>, <https://code.claude.com/docs/en/permission-modes.md>
- Settings: <https://code.claude.com/docs/en/settings.md>
- Model config: <https://code.claude.com/docs/en/model-config.md>
- Costs: <https://code.claude.com/docs/en/costs.md>
- Tools reference: <https://code.claude.com/docs/en/tools-reference.md>
- Errors: <https://code.claude.com/docs/en/errors.md>
- Docs index: <https://code.claude.com/docs/llms.txt>
