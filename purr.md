# Purr

Purr is a single-process terminal app for reviewing GitHub pull requests with Claude. Reviews run as Claude Agent SDK sessions, one per PR, in the background. You drive everything from a keyboard TUI or by voice (Pipecat), and nothing is posted to GitHub or merged without your explicit approval.

Command: `purr`. Python package: `purr` (check availability on PyPI before publishing).

## Problem

Juggling several PR reviews inside the Claude Code terminal gets messy. It's hard to see which reviews are in flight, find the one you were working on, and resume it (`/resume` doesn't solve this well). Purr makes PRs the primary unit: every PR has its own review session, state and findings, and getting back to any review is one keypress. Stepping away makes it worse: when you come back, there's no quick way to see what finished, what failed and what's waiting on you.

Purr should feel like Claude Code, but organized around PRs: your repos and their PRs are the home screen, and inside a PR you're in a Claude Code-style session for that PR.

## Goals

- See all relevant PRs in one place, grouped by review state, across all the repos you work on.
- Start reviews with no typing: mark PRs and press one key, or assign yourself on GitHub.
- Run many reviews concurrently without disturbing the user's working copy.
- Resume any PR's review session at any time, including after restarting the app.
- Inside a PR, work as you would in Claude Code: a full conversational session in the PR's worktree.
- Catch up after being away: an agent-written recap of what happened, with what needs you first.
- Move between repos and PRs instantly, and notice when something happens in another repo. Everything should feel fast.
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
5. **Recap agent** (writes the "while you were away" summary)
6. **SQLite store**

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
│                                                     ├── Recap agent        │
│                         SQLite store ◄──────────────┘                      │
└────────────────────────────────────────────────────────────────────────────┘
```

## Repos

One Purr instance covers several repos; you don't run one per repo:

- PRs assigned to you can be in any repo, and Purr reviews all of them (see Triggers).
- One process means one voice agent, one push-to-talk key and one recap. A Purr per repo would compete for the microphone and split "what happened" across windows.
- It's the first goal: all relevant PRs in one place.

Two things keep this cheap:

- **Watched repos** (the ones whose pool you see) are listed in `~/.config/purr/config.toml` (`repos = ["pipecat-ai/pipecat", ...]`). Running `purr` inside a checkout opens that repo directly (see TUI), so the Claude Code habit of starting inside a repo still works; if the repo isn't watched yet, Purr offers to add it.
- **Purr's own clones.** Purr keeps a blobless clone of each repo it reviews in its data dir (`repos/<owner>/<name>.git`) and creates review worktrees from it. Your checkouts are never touched (no extra worktrees, refs or fetches in them), and PRs assigned to you in repos you don't have locally work the same way.

## PR states

| State | Meaning |
|---|---|
| **Not reviewed** | The pool: open PRs in watched repos that nobody is reviewing (no assignee). |
| **Reviewing** | A review session is running. |
| **Needs you** | Review finished (or agent is blocked) and a decision is pending: post comments, request changes, approve, merge, or answer a question. |
| **Done** | Decision made, nothing pending. |

"Needs you" is pinned to the top of the list.

## Triggers

Two ways in, same result:

1. **From GitHub**: assign yourself to a PR on the web, in any repo (watched or not). The poller (every ~60s, via the GitHub API or `gh`) detects open PRs assigned to you that have no review yet and dispatches a review job automatically. Polling, not webhooks, because the app runs locally.
2. **From the app**: mark PRs in the "Not reviewed" list and press `r`. The app dispatches review jobs **and assigns you on GitHub**, so teammates see you've taken it.

Every PR assigned to you gets reviewed. No team-wide auto-review: only PRs explicitly assigned to you (avoids burning tokens on PRs others handle).

**The pool** ("Not reviewed") is the open PRs in the watched repos that nobody is reviewing. Starting a review assigns you, and teammates assign themselves when they review, so "nobody is reviewing it" simply means "no assignee". A PR leaves the pool as soon as someone is assigned or it's closed. Drafts and your own PRs stay out of the pool.

## Review workers

- One worker task per PR, wrapping a Claude Agent SDK session.
- Each review runs in its **own git worktree** (made from Purr's own clone of the repo, see Repos), checked out at the PR head, so multiple reviews run concurrently without touching each other or the user's working copy.
- Persist the SDK **session ID** per PR in SQLite so any review can be resumed (from the PR session, by voice, or after an app restart). Closing the app pauses running reviews; they resume on reopen.
- Transcripts are not copied into SQLite. The SDK keeps each session's transcript (it's a Claude Code session file), and Purr reads it with `get_session_messages(session_id, directory=worktree)`.
- Output of a review:
  - A short summary.
  - A list of findings (with file/line where possible, severity: issue / suggestion / nit).
  - One or more **pending actions** proposed by the agent (e.g. "post 3 comments + request changes", "approve", "merge").
- The agent must **never** post to GitHub or merge directly. All GitHub-visible actions are proposed as pending actions and executed by the app only after user approval. Enforced, not just prompted: a `can_use_tool` / PreToolUse check denies GitHub writes (`gh pr review|comment|merge`, `gh api` with a write method, `git push`), and the agent gets a `propose_action` tool to use instead.
- The user can keep working in a PR's session after the review; see PR session.

## PR session (Claude Code inside a PR)

Opening a PR (`enter`) puts you in a Claude Code-style terminal for that PR. It's the same Agent SDK session the review ran in, working in the PR's worktree, so you pick up where the review left off: ask questions, ask it to dig deeper, run the tests, or change its proposed action.

- Looks and behaves like Claude Code: streamed assistant messages, tool calls collapsed to one line (`⎿ Read(src/bus.py)`), the prompt at the bottom with history, and the most useful slash commands.
- History comes from the SDK's transcript of the session; new turns stream over the bus as `session.message` events, so the TUI and the voice agent see the same conversation.
- One conversation per PR. Typed and spoken messages go into the same session (`review.message` job) and queue if a turn is in flight.
- Same guardrails as the review: anything GitHub-visible becomes a pending action.
- Permission prompts: if the agent wants a tool that isn't pre-approved, the prompt shows inline as in Claude Code (allow once / always for this PR / deny). If nobody answers, the PR moves to **Needs you** ("agent blocked"). Voice can answer too.
- `z` zooms the session to full screen; `esc` goes back to the list.
- **Open in Claude Code** (`o`): Purr suspends its UI and runs the real `claude --resume <session_id>` in the PR's worktree, with GitHub writes disallowed via `--disallowedTools`. When you quit Claude Code, Purr comes back and picks the session up again. The PR's agent must be idle first, so two processes never drive one session. Because the transcript is the SDK's session file, what you did in Claude Code shows up in Purr's view afterwards.

## While you were away

If things happen while you're not looking, Purr shows you a recap when you come back, written by an agent and focused on PRs.

- **Activity log.** Every Purr event is appended to an `activity` table: PRs entering the pool or getting assigned to you, new commits on PRs under review, PRs closed or merged, reviews started, finished or failed, actions proposed or resolved, agents blocked on a permission prompt. It doubles as each PR's timeline.
- **Being away.** The TUI (any key) and the voice agent (any turn) report activity on the bus (`user.active`, throttled). The last-active time is persisted. You're away after N minutes without activity (default 15) or while the app was closed. While it was closed nothing ran, but the first poll after startup records what changed on GitHub in the meantime, so that shows up too.
- **Coming back.** On your first key or voice turn after being away, if anything notable happened, the "While you were away" panel opens. The raw activity list is there immediately; the agent's summary streams in on top of it within a few seconds, so you're never waiting on it.
- **The recap agent** is an Agent SDK query with no tools (same Claude Code login as the reviews). Input: the activity since you left plus the current state of the PRs involved. Output: a one-line headline plus one item per PR, ordered by what needs you (decisions, blocked agents, failures), then finished reviews, then new PRs. Items are selectable and jump to the PR.
- Recaps are stored. `w` reopens the latest; by voice, "what did I miss?".

```
┌─ While you were away · 14:02 → 15:47 ─────────────────────────────────┐
│ 3 PRs need you, 1 review failed, 1 new PR is being reviewed.          │
│                                                                       │
│ ▸ ⚠ pipecat#2841  Review done: wants to request changes (race)        │
│   ⚠ pipecat#2850  Review done: ready to approve                       │
│   ⚠ whisker#112   Agent blocked: asks to run the full test suite      │
│   ✗ daily-x#77    Review failed: couldn't check out the PR head       │
│   ● pipecat#2862  Assigned to you, review in progress                 │
│                                                                       │
│ ▾ 14 events  (show the raw activity log)                              │
│                                                                       │
│ enter open PR · esc close                                             │
└───────────────────────────────────────────────────────────────────────┘
```

## Bus messages (initial sketch)

Jobs:
- `review.start {pr}` (dispatched by TUI, voice or poller)
- `review.message {pr, text}` (send a follow-up into a PR's session)
- `recap.create {since}` (when you come back, or on demand)

Events (broadcast, everyone listens):
- `pr.list_updated`
- `review.started {pr}`
- `review.progress {pr, ...}`
- `review.finished {pr, summary, findings}`
- `action.pending {pr, action_id, description}`
- `action.resolved {pr, action_id, approved}`
- `review.failed {pr, error}`
- `session.message {pr, message}` (one streamed SDK message: text, tool call or tool result)
- `session.permission_request {pr, request_id, tool, input}`
- `user.active` (from the TUI and the voice agent, throttled)
- `recap.ready {recap_id, headline}`

Commands:
- `action.approve {action_id}` / `action.dismiss {action_id}`
- `session.permission_response {request_id, decision}` (allow once / always for this PR / deny)

Implemented in `src/purr/messages.py`: jobs are `@job(name=...)` handlers on the `reviews` worker; events (`PurrEvent`) and commands (`PurrCommand`) are `BusDataMessage` subclasses. Events are broadcast (no target); commands target the worker that executes them. PRs travel as `owner/repo#123` strings.

## SQLite store

- `prs`: repo, number, title, author, url, head sha, state, assigned_to_me, updated_at, last_seen_at (when you last looked at it; clears its alerts)
- `reviews`: pr, session_id, worktree path, status, summary, started_at, finished_at
- `findings`: review, file, line, severity, text
- `pending_actions`: pr, action_id, kind (comment/request_changes/approve/merge), payload, status
- `activity`: at, pr (optional), kind, text, data. Every Purr event; feeds the recaps and each PR's timeline.
- `recaps`: since, until, headline, items, created_at
- `meta`: key/value, e.g. `last_active_at`

Session transcripts are not stored here; they're the SDK's own session files.

## TUI

Three levels, each one keypress apart: **repos** (home) → **a repo** (its PRs plus the selected PR's session) → **a PR session** zoomed to full screen. The recap panel opens on top of whichever screen you're on.

### Repo list (home)

```
┌─ Purr ────────────────────────────────────────────────── 🔔 1 · whisker#112 needs you ─┐
│                                                                                       │
│    REPO                       NEEDS YOU   REVIEWING   NOT REVIEWED   LAST ACTIVITY    │
│  1   daily-co/daily-x             -           1            -          15 min ago      │
│  2 ▸ pipecat-ai/pipecat           2           1            3          2 min ago       │
│  3   pipecat-ai/pipecat-flows     -           -            4          3 h ago         │
│  4   pipecat-ai/whisker           1 ●         -            1          just now        │
│                                                                                       │
│  ● new since you last looked                                                          │
│                                                                                       │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ enter open · 1-9 repo · ctrl+p find · ctrl+o back · ctrl+n next alert · w recap · 🎙   │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

- One row per watched repo, plus any repo where a PR is assigned to you.
- Rows are sorted alphabetically by `owner/name` (case-insensitive) and numbered in that order, so the order and the `1`–`9` shortcuts only change when a repo is added or removed. Activity shows as counts and a ● marker; rows never move because of it.
- Launching `purr` inside a checkout of a watched repo opens that repo directly (`esc` goes up to this list). Launched anywhere else, Purr starts here.

### Repo screen

Two-pane, lazygit-style layout. Inside a repo, PRs show as `#2841`; everywhere else (alerts, recaps, the switcher) as `whisker#112`, the repo name without its owner unless that's ambiguous.

```
┌─ Purr › 2 pipecat-ai/pipecat ─────────────────────────── 🔔 1 · whisker#112 needs you ─┐
├─ PRs ─────────────────────────────┬─ #2841: Add message bus ──────────────────────────┤
│ ⚠ NEEDS YOU (2)                   │ ✓ Review done · 3 findings · 1 pending action     │
│ ▸ #2841  Message bus         ⚠    │                                                   │
│   #2838  Frame serializer    ⚠    │ ⏺ Reviewing #2841 at 3f2a91c                      │
│                                   │   ⎿ Bash(gh pr diff 2841) · 412 lines             │
│ REVIEWING (1)                     │   ⎿ Read(src/pipecat/bus/bus.py)                  │
│ ● #2850  TTS context         ⟳    │   ⎿ Bash(uv run pytest tests/bus) · 48 passed     │
│                                   │                                                   │
│ NOT REVIEWED (3)                  │ ⏺ Clean refactor, tests pass. Concerns:           │
│ ○ #2855  Evals report             │   1. Race in bus.subscribe() on shutdown          │
│ ○ #2860  Sim scenarios            │   2. Missing docstring on publish()               │
│ ○ #2861  Docs typo                │   3. Nit: unused import in frames.py              │
│                                   │                                                   │
│ DONE (1)                          │ ┌ Pending action ──────────────────────────────┐  │
│ ✓ #2833  Frame cleanup            │ │ Post 3 comments + request changes?           │  │
│                                   │ │            [a] approve   [x] dismiss         │  │
│                                   │ └──────────────────────────────────────────────┘  │
│                                   │                                                   │
│                                   │ ╭──────────────────────────────────────────────╮  │
│                                   │ │ > is the race reachable from user code?      │  │
│                                   │ ╰──────────────────────────────────────────────╯  │
├───────────────────────────────────┴───────────────────────────────────────────────────┤
│ space mark · r review · enter open · a approve · x dismiss · esc repos · ctrl+p · 🎙   │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

- **Header**: where you are (`Purr › 2 pipecat-ai/pipecat`) and the latest alert from other repos.
- **Left pane**: the repo's PRs grouped by state, "Needs you" first.
- **Right pane**: the selected PR's session, Claude Code style (see PR session): a status line, the transcript, the pending action box, and the prompt.
- **Status bar**: key hints and mic state.

### Alerts from other repos

When something notable happens in a repo you're not looking at, the header shows it on every screen (`🔔 1 · whisker#112 needs you`) and that repo gets a ● in the repo list. "Notable" is the same filter the recap uses: a review finished or failed, an agent got blocked, a PR was assigned to you, a pending action appeared. Review progress doesn't count.

- `ctrl+n` jumps straight to the newest alert; pressing it again goes through the rest.
- An alert clears once you've looked at that PR.
- Events in the repo you're in just update its list in place.

### Moving around

Global keys work everywhere, even while typing in a PR prompt:
- `ctrl+p` quick switcher: fuzzy search over repos and PRs (number, title, author), most recent first. Built on Textual's command palette.
- `ctrl+o` back to where you were before the last jump (repo and PR); press it again to return, like `cd -`.
- `ctrl+n` jump to the newest alert from another repo.
- push-to-talk key (hold) for voice.

List keys (letters and numbers act on lists; while a prompt has focus, typing goes to the agent, as in Claude Code):
- `0` repo list, `1`–`9` repo by number
- `enter` open a repo, or a PR's session with its prompt focused
- `esc` up one level: PR prompt → PR list → repo list
- `space` mark/unmark PR
- `r` start review on marked PRs (or the selected one)
- `a` approve pending action, `x` dismiss
- `z` zoom the PR session to full screen
- `o` open the PR's session in Claude Code
- `w` open the latest "while you were away" recap
- `/` filter

### Fast by default

- Nothing on screen waits on the network or an agent. Every screen draws from local state, so switching repos or PRs takes effect on the next frame (target: well under 50 ms).
- That state is kept in memory, loaded from SQLite at startup and kept current by bus events.
- Startup: the UI is usable in under a second, drawn from the last saved state. The first GitHub poll, resuming interrupted reviews and loading the voice models all happen in the background after that.
- PR transcripts load on first open, newest messages first, and stay cached.
- Slow work (a poll, an agent turn) shows progress in place, and keys keep working meanwhile.

## Voice

- A Pipecat pipeline running in the same process with a local audio transport, activated by push-to-talk.
- It's a bus participant, so it can do anything the TUI can: list PRs, start reviews, read out summaries/findings, approve or dismiss pending actions, and send follow-ups into a PR's session.
- Proactive prompts: when `action.pending` arrives, the voice agent can ask e.g. "I've reviewed the message bus PR, three findings. Want me to post them and request changes?"
- Typing and talking should feel equivalent; voice is not a separate mode.
- Risky actions (merge, posting comments) require clear confirmation by voice too.
- "What did I miss?" reads the latest recap's headline, then goes through the items on request.
- It can answer the agent's permission prompts ("yes, let it run the tests").
- Navigation works by voice too: "go to whisker", "open the message bus PR", "go back".

## Suggested build order

1. Project skeleton: single asyncio process, Pipecat `WorkerRunner` with local bus, SQLite. (Done.)
2. GitHub poller: watched repos from the config file, the pool (unassigned open PRs) plus PRs assigned to you in any repo, PR list in SQLite, activity log.
3. Review worker: Purr-managed clone and worktree, Agent SDK session with the GitHub guardrails, store session ID, summary, findings, pending actions, stream `session.message` events.
4. Pending action execution (post comments / review verdict / merge) gated on approval.
5. Textual TUI: repo list, repo screen, Claude Code-style PR session (transcript, prompt, permission prompts, zoom), alerts from other repos, quick switcher and jump keys, live updates from bus events.
6. "Start from app" also assigns you on GitHub.
7. While you were away: activity tracking, recap agent, recap panel.
8. Open a PR's session in Claude Code (`o`).
9. Voice agent with push-to-talk.

## Open questions

- Exact review prompt and output schema for findings/pending actions.
- How to map findings to GitHub review comments (inline vs. general).
- Worktree cleanup policy (on Done? after N days?).
- STT/LLM/TTS choices for the voice agent.
- Does `claude --resume <id>` continue the same session ID or write a new one? That decides how Purr picks the session back up after `o`.
- How long without activity counts as "away" (default 15 minutes?).
- Which slash commands the PR session supports at first (`/clear`, `/compact`, `/model`, ...).
