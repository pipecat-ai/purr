# Purr

Purr is a single-process terminal app for reviewing GitHub pull requests with Claude. Reviews run as Claude Agent SDK sessions, one per PR, in the background. You drive everything from a keyboard TUI or by voice (Pipecat), and nothing is posted to GitHub or merged without your explicit approval.

Command: `purr`. Python package: `purr` (check availability on PyPI before publishing).

## Problem

Juggling several PR reviews inside the Claude Code terminal gets messy. It's hard to see which reviews are in flight, find the one you were working on, and resume it (`/resume` doesn't solve this well). Purr makes PRs the primary unit: every PR has its own review session, state and findings, and getting back to any review is one keypress.

## Goals

- See all relevant PRs in one place, grouped by review state.
- Start reviews with no typing: mark PRs and press one key, or assign yourself on GitHub.
- Run many reviews concurrently without disturbing the user's working copy.
- Resume any PR's review session at any time, including after restarting the app.
- Control everything by keyboard or voice, interchangeably.
- Never take a visible action on GitHub (comments, review verdicts, merges) without approval.

## Non-goals (for now)

- GitLab (design so it can be added later, but GitHub only for v1).
- Android app. Long term, it would talk directly to the voice agent. Not in scope.
- Remote access when the app is closed. Like Claude Code remote control: app not running means nothing to connect to.
- Concurrency limits on reviews. Review everything assigned; add a limit later only if rate limits or machine load become a problem.

## Architecture

One Python asyncio process containing:

1. **TUI** (Textual)
2. **Voice agent** (Pipecat pipeline, local audio transport, push-to-talk)
3. **GitHub poller**
4. **Review workers** (Claude Agent SDK, one session per PR)
5. **SQLite store**

All components are Pipecat **workers** on Pipecat's in-process worker bus (`AsyncQueueBus`), sharing a single `WorkerRunner`. (Pipecat Subagents has been merged into Pipecat core as `pipecat.workers` / `pipecat.bus`, available since `pipecat-ai` 1.16.0; the standalone `pipecat-subagents` repo is archived.) Reviews are dispatched as **jobs** on the bus (Pipecat job coordination) and handled by review worker agents. The TUI and the voice agent are both bus participants, so starting a review is the same path from either: dispatch a job, listen for events.

Why not proxy agents or Redis: we looked at Pipecat proxy agents and they are point-to-point tunnels between two named agents. Only targeted messages of allowed types cross; broadcast messages (like "review finished") are not forwarded. Since everything now lives in one process, the local bus is enough. If components are ever split into separate processes, switch to a shared distributed bus (e.g. Redis), not proxies.

Relevant docs (verify current APIs before building):
- Workers and the runner: https://docs.pipecat.ai/pipecat/learn/your-first-agent
- Multiple workers on one bus: https://docs.pipecat.ai/pipecat/learn/multiple-llm-agents
- Job coordination: https://docs.pipecat.ai/pipecat/learn/job-coordination
- Proxy agents (for background on why we're not using them): https://docs.pipecat.ai/pipecat/learn/proxy-agents
- Claude Agent SDK for Python (`claude-agent-sdk`)

```
┌──────────────────────── single process (asyncio) ─────────────────────────┐
│                                                                            │
│  Textual TUI ──┐                                    ┌── GitHub poller      │
│                │                                    │                      │
│  Voice agent ──┼──────── Pipecat local bus ─────────┼── Review workers ──► Claude Agent SDK
│  (Pipecat)     │        (jobs + events)             │   (1 per PR,          (git worktree per PR)
│                                                     │    in a worktree)    │
│                         SQLite store ◄──────────────┘                      │
└────────────────────────────────────────────────────────────────────────────┘
```

## PR states

| State | Meaning |
|---|---|
| **Not reviewed** | Open PRs you could pick up (pool). No review started. |
| **Reviewing** | A review session is running. |
| **Needs you** | Review finished (or agent is blocked) and a decision is pending: post comments, request changes, approve, merge, or answer a question. |
| **Done** | Decision made, nothing pending. |

"Needs you" is pinned to the top of the list.

## Triggers

Two ways in, same result:

1. **From GitHub**: assign yourself to a PR on the web. The poller (every ~60s, via the GitHub API or `gh`) detects open PRs assigned to you that have no review yet and dispatches a review job automatically. Polling, not webhooks, because the app runs locally.
2. **From the app**: mark PRs in the "Not reviewed" list and press `r`. The app dispatches review jobs **and assigns you on GitHub**, so teammates see you've taken it.

Every PR assigned to you gets reviewed. No team-wide auto-review: only PRs explicitly assigned to you (avoids burning tokens on PRs others handle).

## Review workers

- One worker task per PR, wrapping a Claude Agent SDK session.
- Each review runs in its **own git worktree** of the repo, checked out at the PR head, so multiple reviews run concurrently without touching each other or the user's working copy.
- Persist the SDK **session ID** per PR in SQLite so any review can be resumed (from the TUI chat, by voice, or after an app restart). Closing the app pauses running reviews; they resume on reopen.
- Output of a review:
  - A short summary.
  - A list of findings (with file/line where possible, severity: issue / suggestion / nit).
  - One or more **pending actions** proposed by the agent (e.g. "post 3 comments + request changes", "approve", "merge").
- The agent must **never** post to GitHub or merge directly. All GitHub-visible actions are proposed as pending actions and executed by the app only after user approval.
- The user can keep chatting with a PR's session (ask questions, ask it to dig deeper, change the proposed action).

## Bus messages (initial sketch)

Jobs:
- `review.start {pr}` (dispatched by TUI, voice or poller)
- `review.message {pr, text}` (send a follow-up into a PR's session)

Events (broadcast, everyone listens):
- `pr.list_updated`
- `review.started {pr}`
- `review.progress {pr, ...}`
- `review.finished {pr, summary, findings}`
- `action.pending {pr, action_id, description}`
- `action.resolved {pr, action_id, approved}`
- `review.failed {pr, error}`

Commands:
- `action.approve {action_id}` / `action.dismiss {action_id}`

Implemented in `src/purr/messages.py`: jobs are `@job(name=...)` handlers on the `reviews` worker; events (`PurrEvent`) and commands (`PurrCommand`) are `BusDataMessage` subclasses. Events are broadcast (no target); commands target the worker that executes them. PRs travel as `owner/repo#123` strings.

## SQLite store

- `prs`: repo, number, title, author, url, head sha, state, assigned_to_me, updated_at
- `reviews`: pr, session_id, worktree path, status, summary, started_at, finished_at
- `findings`: review, file, line, severity, text
- `pending_actions`: pr, action_id, kind (comment/request_changes/approve/merge), payload, status

## TUI

Two-pane, lazygit-style layout.

```
┌─ Purr ────────────────────────────┬─ pipecat#2841: Add message bus ────────────────┐
│ ⚠ NEEDS YOU (2)                   │ Status: ✓ Review done · 3 findings             │
│ ▸ pipecat#2841  Message bus  ⚠    │                                                │
│   whisker#112   File format  ⚠    │ Summary                                        │
│                                   │  Clean refactor, tests pass. Two concerns.     │
│ REVIEWING (2)                     │                                                │
│ ● pipecat#2850  TTS context  ⟳    │ Findings                                       │
│ ● daily-x#77    Win CI fix   ⟳    │  1. Race in bus.subscribe() on shutdown        │
│                                   │  2. Missing docstring on publish()             │
│ NOT REVIEWED (3)                  │  3. Nit: unused import in frames.py            │
│ ○ pipecat#2855  Evals report      │                                                │
│ ○ pipecat#2860  Sim scenarios     │ ┌ Pending action ─────────────────────────┐    │
│ ○ whisker#115   Layout tweak      │ │ Post 3 comments + request changes?      │    │
│                                   │ │      [a] approve   [x] dismiss          │    │
│ DONE (1)                          │ └─────────────────────────────────────────┘    │
│ ✓ pipecat#2833  Frame cleanup     │                                                │
│                                   │ > ask the agent about this PR...               │
├───────────────────────────────────┴────────────────────────────────────────────────┤
│ space mark · r review · enter open · a approve · x dismiss · / filter · hold-to-talk 🎙 │
└────────────────────────────────────────────────────────────────────────────────────┘
```

- **Left pane**: PRs grouped by state, "Needs you" first.
- **Right pane**: selected PR: status, summary, findings, pending action box, and a chat input wired to that PR's Agent SDK session.
- **Status bar**: key hints and mic state.

Keys:
- `space` mark/unmark PR
- `r` start review on marked PRs (or the selected one)
- `enter` focus the PR's session chat
- `a` approve pending action, `x` dismiss
- `/` filter
- push-to-talk key (hold) for voice

## Voice

- A Pipecat pipeline running in the same process with a local audio transport, activated by push-to-talk.
- It's a bus participant, so it can do anything the TUI can: list PRs, start reviews, read out summaries/findings, approve or dismiss pending actions, and send follow-ups into a PR's session.
- Proactive prompts: when `action.pending` arrives, the voice agent can ask e.g. "I've reviewed the message bus PR, three findings. Want me to post them and request changes?"
- Typing and talking should feel equivalent; voice is not a separate mode.
- Risky actions (merge, posting comments) require clear confirmation by voice too.

## Suggested build order

1. Project skeleton: single asyncio process, Pipecat `WorkerRunner` with local bus, SQLite.
2. GitHub poller + PR list in SQLite (assigned-to-me + open PRs in the pool).
3. Review worker: worktree setup, Agent SDK session, store session ID, summary, findings, pending actions.
4. Pending action execution (post comments / review verdict / merge) gated on approval.
5. Textual TUI: list, detail pane, keys, live updates from bus events, session chat.
6. "Start from app" also assigns you on GitHub.
7. Voice agent with push-to-talk.

## Open questions

- Exact review prompt and output schema for findings/pending actions.
- How to map findings to GitHub review comments (inline vs. general).
- Worktree cleanup policy (on Done? after N days?).
- Which repos to watch (config file listing repos, or all repos where you're assigned).
- STT/LLM/TTS choices for the voice agent.
