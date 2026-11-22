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

- **Watched repos** (the ones whose pool you see) are added and removed in the UI, and there are none to start with. Running `purr` inside a checkout opens that repo directly (see TUI), so the Claude Code habit of starting inside a repo still works; if the repo isn't watched yet, Purr offers to add it.
- **Purr's own clones.** Purr keeps a blobless clone of each repo with open PRs in its data dir (`repos/<owner>/<name>.git`) and creates review worktrees from it. Your checkouts are never touched (no extra worktrees, refs or fetches in them), and PRs assigned to you in repos you don't have locally work the same way.
- **Open PRs are synced into the clone, so diffs are local.** The `git` worker syncs on start and after every change to the PR list (a poll found new or updated PRs), a second after the last one, in the background:
  1. One `git fetch` for the PRs that are new or have new commits (`refs/pull/N/head` → `refs/purr/pull/N`) and the branches they merge into (`refs/purr/heads/<base>`; the poll now carries each PR's `baseRefName`). PRs no longer open lose their ref.
  2. A commit-graph, so finding where each PR branched off takes ms instead of 50.
  3. The files those diffs touch, in one more fetch: their blob ids come from the trees (`git diff --raw`, no contents needed), and only missing ones are requested.

  Nothing new, no network (bases are refetched every 10 minutes regardless). The diff is then `git diff <base>...<head>` in the clone, the same as GitHub's, with no 300-file or 20,000-line limit. Measured on pipecat from scratch: the clone and first sync of 337 open PRs took 19 s in the background, the clone is 31 MB, and a diff over the bus takes 7 ms on average (23 ms for a 3,000-line one). A PR the clone doesn't have yet (a push since the last sync, a repo still cloning) gets its diff from GitHub instead.
- **A worktree only once a review starts.** Diffs don't need one. Starting a review (`r`, or auto-review) makes the PR's worktree, where the agent, and you through the PR session or `c`, can run the code and change it. It's made from what the sync already brought, so there's no fetch unless the PR moved since. Measured on pipecat: the first worktree takes 3 s (the clone downloads the rest of the repo's files once, ending at 61 MB), the next ones 1.4 s and 0.2 s.
- **Where they are, and using one yourself.** `~/.local/share/purr/worktrees/<owner>/<repo>/<number>` (under the data dir), shown in the PR's header (`Worktree ~/.local/share/purr/worktrees/pipecat-ai/pipecat/6940 · ! opens a shell there`). `!` opens your `$SHELL` there, making the worktree first if the PR has none (not reviewed yet, or removed); exit it to come back to Purr, which keeps running underneath.
- **Your changes are never thrown away.** A review turn on the same head leaves the worktree as it is, with whatever you (or the agent, with your OK) changed. When new commits move it on, uncommitted changes go to a stash first (`git stash list` there: "purr: changes in owner/repo#N at abc1234, before def5678"). A worktree with uncommitted changes is never removed.
- **Worktrees go when they're done.** A PR's worktree is removed when you approve the PR through Purr, and when it's merged or closed; after the first poll on startup, any left from before go too (`GitWorker.sweep`). Never while a review is running in it. Nothing is lost: Claude Code keeps the session under `~/.claude`, not in the worktree, and finds it by path even with the directory gone. A follow-up, a re-review or `c` (`pr.worktree`) makes it again at the same path, and the session carries on.

## PR states

| State | Meaning |
|---|---|
| **Not reviewed** | The pool: open PRs in watched repos that nobody is reviewing (no assignee). Also PRs assigned to you that haven't been reviewed yet (with auto-review off). |
| **Reviewing** | A review session is running. |
| **Needs you** | Review finished (or agent is blocked) and a decision is pending: post comments, request changes, approve, merge, or answer a question. |
| **Done** | Decision made, nothing pending. |

"Needs you" is pinned to the top of the list. **Done** comes before the pool of unreviewed PRs (hundreds, in a big repo), so what you finished lately is at hand: the latest 5, by when they got there (`prs.state_at`, which moves only when the state does), with `/` finding the others; an older one you go to (an alert, `ctrl+p`, a search) shows too while it's selected. PRs someone else is assigned to aren't a state of their own: they sit at the end of the list as **Others reviewing**, with who has them, so you can see what's taken.

## Triggers

Two ways in, same result:

1. **From GitHub**: assign yourself to a PR on the web, in any repo (watched or not). The poller (every ~60s, via the GitHub API or `gh`) detects open PRs assigned to you that have no review yet and dispatches a review job automatically. Polling, not webhooks, because the app runs locally.
2. **From the app**: select a PR to read its description, files and CI first, then press `r` (or mark several and press `r`). The app dispatches review jobs **and assigns you on GitHub**, so teammates see you've taken it. On a PR someone else has, `r` asks first and adds you next to them.

Every PR assigned to you gets reviewed. No team-wide auto-review: only PRs explicitly assigned to you (avoids burning tokens on PRs others handle). How deep each review goes, what it focuses on and what goes first is up to the lead (see The lead); both ways in go through it.

**The pool** ("Not reviewed") is the open PRs in the watched repos that nobody is reviewing. Starting a review assigns you, and teammates assign themselves when they review, so "nobody is reviewing it" simply means "no assignee". A PR leaves the pool as soon as someone is assigned (it moves to "Others reviewing") or it's closed. Drafts and your own PRs aren't listed.

**Polling** uses GitHub's GraphQL search through `gh`, batching every search into one request per page. Each watched repo has one search: its open, non-draft PRs by other people, with author, opened date and assignees, which splits into the pool and "Others reviewing". It stays lean on purpose (pipecat: 337 PRs in 4 pages). Adding descriptions and file lists made a 100-PR page take 5.8 s instead of about 2 s, so those are fetched when you look at a PR (see Repo screen). A search takes 1-2 s regardless of size, and a big repo's pool is several pages (pipecat: 4), so a repo is fully synced only on startup, when added, and every 15 minutes. Polls in between fetch just the PRs updated since the last sync (5 minutes of overlap for search-index lag), which is usually one request of about a second. A newly added repo's pool is shown page by page as it arrives. A search that fails or comes back incomplete never removes anything.

## The lead

An agent that thinks about what needs doing before the review agents start: `LeadWorker` (`workers/lead.py`) on the bus, with the planning itself in `lead.py`. It **never skips a PR**: everything assigned to you gets reviewed, and every review ends by saying plainly whether it found something.

- **What it decides, per PR** (a `ReviewPlan`):
  - *depth*: **quick** for small, low-risk changes (docs, typos, bumps, tests only, small obvious fixes): one agent reads the diff and the code it touches, no subagents, on a cheaper model (`quick_model`, Sonnet by default). **Full** for anything with real risk: the repo's review skill when it has one (pipecat's `code-review`), with subagents for independent checks, on the review model. When in doubt, full. Your own messages in a PR's session always get the review model: that's where you dig in.
  - *priority*: high, normal or low: the order reviews wait in line. High when someone's likely waiting (small fixes, release labels, PRs you asked for); low for drafts, failing CI, big PRs that take long anyway.
  - *focus*: up to 4 points specific to the PR for the review to look at hardest, naming a repo skill when one fits ("docstrings and changelog: pipecat:prose-review").
  - *why*: one line for you, shown under the PR's status.
- **When it runs**: after every poll, for the open PRs assigned to you that were never reviewed (on startup, once the first poll is in), and when a PR gets assigned to you; all at once, so one call plans a poll's worth (batches of 15). **Re-reviews are planned too**: new commits on a reviewed PR assigned to you go to the lead with what changed since the last review (the new commits' subjects and the files they touch, from the clone: commits and trees only, `GitRepos.new_commits`), so a small follow-up ("Address review: guard the race") gets a quick look and a rewrite gets a full one. A quick re-review doesn't follow the repo's review skill; the plan replaces the review's (`reviews.plan` is the latest round's). `r` in the TUI (one PR or several marked) asks it too, as `lead.review {prs}`, and those are always high priority, since you're waiting.
- **How**: one tool-less Agent SDK call with structured output (`lead_model`, Sonnet by default), from what Purr already has: each PR's description, files with +/- lines, CI, labels and earlier review; the repo's skills (only those a review agent can load are offered for focus) and its review skill; your review and code style. Measured on pipecat's 10 most recently updated PRs: 12 s and $0.05 for all ten. It called the 50-line flow-config fix and the Sarvam error fix quick, the 3,000-line Sidechat feature full and low priority, the SyncParallelPipeline frame-routing fix full and high, and the regex fix with failing CI quick and low, with focus points like "can a turn be dropped or the LLM answer twice; hold_secs timer cancellation on interruption".
- **Never in the way**: if it can't plan (offline, a bad answer), or leaves a PR out, the PR gets a full review anyway (`by: default`, with the reason). The PR shows "Planning the review" meanwhile.
- **What stays in code**, not up to the lead: nothing is posted without you, the permission policy, how many reviews run at once, budgets.
- The plan goes to the review worker with `review.start {pr, plan}`, is stored with the review (`reviews.plan`, JSON), rides on `review.started`, and goes into the review agent's system prompt ("How this review should go"), so follow-ups know it too.
- The same planning is what the voice agent will use ("what should I look at first?", "give #3112 a full review"); pipecat 1.16's `LLMWorker` (a bus worker with `@tool` methods) is the shape for that.

**Found something or not.** Every finished review says one thing first. **Only issues count**: suggestions and nits are mentioned, but a review with nothing else found nothing (`found 1 issue, 2 nits`, `nothing found (2 nits)`, `nothing found`). In the PR list, a reviewed PR's mark says it at a glance (✓ green: nothing found; ● red: found issues); the PR's header says it in words (`● Review: found 1 issue · 1 pending action`), with the plan under it (`Full review · high priority · Core pipeline frame routing fix`); alerts and news say `review: nothing found` / `review: found 2 issues`. Finding nothing is a fine outcome, and the review agent is told so.

## Review workers

- One worker task per PR, wrapping a Claude Agent SDK session.
- Each review runs in its **own git worktree** (made from Purr's own clone of the repo, see Repos), checked out at the PR head, so multiple reviews run concurrently without touching each other or the user's working copy.
- Persist the SDK **session ID** per PR in SQLite so any review can be resumed (from the PR session, by voice, or after an app restart). Closing the app pauses running reviews; they resume on reopen.
- The SDK keeps each session's transcript (it's a Claude Code session file); that's what sessions resume from and what the PR pane loads, with `get_session_messages(session_id, directory=worktree)`. Purr also records every conversation in SQLite as it happens (`runs` and `messages`, see SQLite store), with times, for analysis.
- Output of a review:
  - A short summary.
  - A list of findings (with file/line where possible, severity: issue / suggestion / nit).
  - One or more **pending actions** proposed by the agent (e.g. "post 3 comments + request changes", "approve", "merge").
- The agent must **never** post to GitHub or merge directly. All GitHub-visible actions are proposed as pending actions and executed by the app only after user approval. Enforced, not just prompted: a `can_use_tool` / PreToolUse check denies GitHub writes (`gh pr review|comment|merge`, `gh api` with a write method, `git push`), and the agent gets a `propose_action` tool to use instead.
- The user can keep working in a PR's session after the review; see PR session.

How it's built (`src/purr/review/`, `src/purr/workers/reviews.py`):

- The agent reports through two in-process tools: `submit_review {summary, findings}` and `propose_action {kind: request_changes|approve|comment, description, body, comments}`. A new proposal (or a new review) dismisses the previous pending one.
- The session ID is a UUID Purr picks before starting, stored right away, so even a review that dies early can be resumed (`resume=`), or restarted under the same ID if no transcript was written yet.
- **Permissions** are Claude Code's own; see Permissions.
- Only the user's own Claude Code settings are loaded (`setting_sources=["user"]`). A PR's `.claude/settings.json` could define hooks, and hooks run commands.
- The repo's own skills, agents and commands (`.claude/skills`, `.claude/agents`, `.claude/commands`) are loaded from its default branch as a plugin named after the repo, so pipecat's show up as `pipecat:code-review`, `pipecat:docstring`, ... (`plugins=`). Purr builds it from its clone (`git archive` of those directories into `plugins/<owner>/<name>/<key>`, the key coming from their tree hashes), so it's rebuilt only when they change on the default branch. Frontmatter that could get around the review policy is dropped: `hooks`, `allowed-tools`, `permissionMode`, `mcpServers`. The repo's `settings.json` isn't used. A review goes ahead without them if they can't be loaded.
- **The repo's own review skill.** When a repo has a skill for reviewing PRs, Purr's full reviews follow it (quick ones don't) (`review/skills.py`). pipecat's `code-review` is one: its multi-agent process (CLAUDE.md compliance, two bug scans, each issue validated by another agent) and its bar (only high-signal issues) replace Purr's general guidance.
  - *Which skills:* those whose name says "review" or whose description says they review PRs ("code review", "review a pull request"). A skill with both is clearly the one; for pipecat that's `code-review`, while `prose-review` is only a candidate and `cleanup`, `pr-description` aren't.
  - *Choosing:* one clear skill is used without asking, and a notice says so. Otherwise, if there are candidates, Purr asks once (a dialog listing them, plus "Purr's own review"), and the repo's reviews wait up to `permission_wait` (5 minutes) for the answer, then go ahead without it; `esc` means ask again next run. The choice is kept per repo (`review_skills` table: the skill or '' for Purr's own, who chose it, when). `i` then `s` inside a repo changes it. A skill the repo later drops means Purr's own review.
  - *How:* the review starts with the skill as a slash command (`/pipecat:code-review <PR URL>`), the way you'd run it in Claude Code. That works for skills marked `disable-model-invocation` too, which the agent couldn't start on its own. A section in the system prompt fits the skill into Purr: skip its checks on whether to review at all (draft, already commented on by Claude), never post (what it would post goes into `propose_action`; the policy denies `gh pr comment`/`gh pr review` regardless), and finish with `submit_review` and `propose_action`. Re-reviews of new commits follow the skill's process again. Each review records the skill it followed (`reviews.skill`), so reviews with and without a skill can be compared (cost, turns, what got approved).
  - Subagents the skill starts (pipecat's starts about eight) are recorded in `messages` like everything else, tagged with the call that started them; the PR's conversation shows only those calls (`Agent(Scan for obvious bugs)`) and what each came back with.
  - Any other skill can still be run by hand in a PR's session: type `/pipecat:prose-review`.
- PRs assigned to you are reviewed automatically (`auto_review`, on by default), through the lead (see The lead): when a poll reports an assignment, and after every poll for any open PR assigned to you that was never reviewed. On startup that means once the first poll has brought the store up to date (the GitHub worker announces each finished poll with a `github.synced` event), not from what the store remembers, which may be a PR that closed or was reassigned while Purr wasn't running. Reviews interrupted by quitting resume on the next start.
- **A few at a time.** At most `max_reviews` (3) reviews run at once, re-reviews of new commits and resumed reviews included; the rest wait in line by the lead's priority, then in order (`workers/slots.py`), and their PR's pane says where they are (`Queued, next up`, `Queued, 2 ahead`). Your own messages in a PR's session don't wait: you're the one waiting for them.
- A failed review moves the PR to Needs you, with the error.
- The agent reads linked issues (`gh issue view`) to know what the PR is meant to do, and the PR's existing review threads (a `get_review_threads` tool: ids, file and line, resolved or not, comments, with the user's own marked) so it doesn't repeat what's been said. A proposal can carry `replies` to existing threads next to its new inline comments; Purr posts the review, then the replies. A reply failing after the review went up is a warning, not a retry, so the review can't be posted twice.
- **New commits** on a reviewed PR assigned to you: the same session gets a follow-up asking it to look at `git diff <reviewed head> <new head>`, say which earlier findings are resolved or still stand, reply on the threads about them, and propose what to post now. Each review records the commit it last looked at (`reviews.head_sha`). The agent remembers its first review, so there's no interdiff machinery: the conversation is the memory.
- Measured on a 44-line pipecat PR: about 75 s from start to proposed action, including the first blobless clone of pipecat (about 7 s).

## PR session (Claude Code inside a PR)

Opening a PR (`o` or `enter`) puts you in a Claude Code-style terminal for that PR. It's the same Agent SDK session the review ran in, working in the PR's worktree, so you pick up where the review left off: ask questions, ask it to dig deeper, run the tests, or change its proposed action.

- Looks and behaves like Claude Code: streamed assistant messages, tool calls collapsed to one line (`⎿ Read(src/bus.py)`), the prompt at the bottom with history, and the most useful slash commands.
- The prompt takes several lines: `enter` sends, and `ctrl+j` starts a new line (so do `shift+enter`/`alt+enter` in terminals that tell them apart, and a `\` at the end of the line, as in Claude Code). Pasting keeps the lines.
- It works like a shell's: `↑`/`↓` on the first/last line go through what you asked on this PR, `ctrl+r` searches everything you've asked on any PR (type to filter, `enter` puts it in the prompt), and `ctrl+a`/`ctrl+e`/`ctrl+b`/`ctrl+f`/`alt+b`/`alt+f` move around the line (`ctrl+k`/`ctrl+u`/`ctrl+w` delete). `page up`/`page down` scroll the transcript while you type. What you ask is kept in the `prompts` table, so the history survives restarts.
- History comes from the SDK's transcript of the session; new turns stream over the bus as `session.message` events, so the TUI and the voice agent see the same conversation.
- One conversation per PR. Typed and spoken messages go into the same session (the `SendMessage` job) and queue if a turn is in flight.
- **Slash commands** (`tui/commands.py`). Typing `/` lists what matches above the prompt (Purr's commands, Claude Code's useful ones, the repo's skills; after three letters, by what's in the name too, so `/prose` finds `/pipecat:prose-review`): ↑/↓ pick, tab or enter fills it in, enter again runs it.
  - *Purr's own*, handled by Purr, never sent to the agent: `/help` (all of this, with the repo's skills); `/review [quick|full]` (start over with a fresh review, planned by the lead unless you say how deep; `/clear` is the same, so Claude Code's `/clear` can't cut the session off from Purr); `/model [name|default]` (the model for everything in this PR's conversation, stored with the PR, so it sticks, unlike Claude Code's `/model`, which Purr would override on the next turn); `/cost` (each run on this PR: what it was, when, how long, turns, tokens, cost, model, and the total, from the `runs` table); `/shell` (same as `!`); `/claude` (same as `c`). Their output goes in the transcript, just for you.
  - *Passed to Claude Code as typed*: `/compact` and `/context`, the repo's skills (`/pipecat:prose-review`), and anything else after a `/`, your own skills included.
- **Open in Claude Code** (`c`, or `/claude`), in a sentence: the PR's conversation is a real Claude Code session that Purr shows in its own, simpler view; `c` opens that same conversation in Claude Code itself, in the PR's worktree, for what Purr's view doesn't do (rewind, plan mode, pasting images, interrupting with esc, its diffs of edits). Quit it to come back. It's for hands-on work, so the key isn't in the footer.
- Same guardrails as the review: anything GitHub-visible becomes a pending action.
- **Writing while the agent works** goes to it straight away, as in Claude Code: it reads it at its next step (Claude Code absorbs it mid-turn), or as a turn of its own while its background agents are out. Each run has an inbox the review worker puts your messages in, and the run sends them into the live session (`client.query`). That takes knowing when the run is really over, which a result doesn't say (a turn ends, but background agents, or your message, may still be owed a turn): Purr sets `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS`, and the run ends when the CLI reports `idle` after a turn's result, with nothing of yours unsent or not yet taken up. A message sent just as it went idle waits for the CLI to take it up (`running`). From a CLI that reports no state, messages wait in the inbox, and whatever's left when a run ends goes as a follow-up. Claude Code stores what it absorbed mid-turn as an attachment (`queued_command`) that the SDK's session reader leaves out, so reloading reads those from the session file and puts them back after the message they followed. Slash commands still wait until the agent's done.
- **Deciding happens in the conversation, not in per-finding cards.** You keep digging with the agent until you agree on what to post; "drop the nit", "reword the race comment", "make it a comment instead" make it propose again, and the new proposal replaces the old one. Writing while a proposal waits sets it aside straight away (as `x` would), so what's on screen is never a proposal the conversation has moved past; your message carries a note for the agent (a `<purr-note>`, which the transcript leaves out) saying so, and to propose again, changed or as it was, if it still recommends posting something. The thread says so (`◼ Proposal set aside: …`, then your message), also when reloaded, from the note. Slash commands (`/compact`, a skill) aren't replies and leave it be. The transcript shows each proposal in full (verdict, overall comment, every inline comment and thread reply), so `a` posts exactly what you've read.
- Permission prompts: when Claude Code asks about a tool call, the prompt shows inline as in Claude Code (`a` allow once / `A` always / `x` deny, the same keys as approving or dismissing a proposal) and the PR moves to **Needs you**. See Permissions for `A`. Voice can answer too. Writing instead is a no with directions, like Claude Code's "No, and tell Claude what to do differently": the tool call is denied with your message, the agent reads it there and carries on in the same run, and the thread shows it under the call: `⎿ Not allowed: you wrote instead`, then your message. The agent waits up to `permission_wait` (5 minutes) and then carries on without it, saying at the end what it wanted to run and why; the recap mentions it. The wait has to be bounded: Claude Code gives up on an unanswered permission request after about 10 minutes, and after that its session refuses every tool call.
- The agent's reasoning shows in the transcript the way Claude Code shows it: dimmed, ahead of what it leads to, a line per thought (its heading or first sentence: `Checking the lock file…`). In full, even summarized, it's about ten times what the agent says (measured: 137k characters of thinking to 14k of replies), so `t` shows it in full only when you ask, and again a line each on the next `t`. Purr asks for summarized thinking (`thinking={"type": "adaptive", "display": "summarized"}`); the SDK's default returns it empty.
- `z` zooms the session to full screen; `esc` goes home (see Repo screen).
- **While the agent works**, a line above the prompt moves, like Claude Code's: a star that grows and shrinks, a cat verb ("Prowling…", a new one every 20 s), how long it's been (`50s`, `1m 2s`) and how many of its tasks are out in the background (`· 2 in the background`: the run sends a `background` entry whenever that changes, which the transcript doesn't show). It sits outside the scrolling conversation, so it shows even when you've scrolled up, and goes away when the agent's done or waiting on you (a question, a proposal) or for its turn in the queue. The PR's mark in the list spins the same way. A turn that ends with agents still in the background isn't "done": only the run's last result is.
- **What background agents are up to** shows in the PR's header, which stays put: Claude Code reports each one's progress (`task_progress`: its last tool and how many it's used), so the header reads `⟳ Reviewing · In the background: Bug scan · Grep · 14 tool uses`, the latest report winning. It's status, not the conversation: it isn't kept with the run's messages, and neither is the `background` count the working line uses.
- **Background tasks** (agents, long commands) get a line when they start and one when they end, named by what they are (`✓ background done: Bug scan`), not by what they reported: an agent's report is often a note to the main agent ("delivered to you as a message from …"). `◼ stopped` is neutral, since the agent stops what it no longer needs; only `✗ failed` is red.
- **Open in Claude Code** (`c`): Purr suspends its UI and runs the real `claude --resume <session_id>` in the PR's worktree, with GitHub writes disallowed via `--disallowedTools`. When you quit Claude Code, Purr comes back and picks the session up again. The PR's agent must be idle first, so two processes never drive one session. Because the transcript is the SDK's session file, what you did in Claude Code shows up in Purr's view afterwards.

## Permissions: Claude Code's own

Reviews use Claude Code's permission system, not one of Purr's own (`review/agent.py`, `review/rules.py`):

- **Claude Code's auto mode** (`permission_mode`, `--permission-mode`): safe things run, and Claude Code asks before anything it judges risky. Your Claude Code settings (`~/.claude/settings.json`) and their rules apply, as everywhere else.
- **When Claude Code asks**, the question goes to Purr (`can_use_tool`): the PR moves to *Needs you* and the prompt shows in its pane. `a` allows once, `x` denies, `A` keeps Claude Code's own suggested rule (`Bash(uv run pytest:*)`): *everywhere* goes into your Claude Code settings (so it applies in Claude Code itself too); *this repo* goes into Purr's Claude Code settings for the repo (`~/.config/purr/repos/<owner>/<name>/settings.json`), which that repo's reviews load. `i` then `p` opens them (at home, your Claude Code settings).
- **Purr's one rule:** nothing that changes GitHub (reviews, comments, merges, `gh api` writes, `git push`) and no reading your GitHub token. As Claude Code deny rules, plus a check before every tool call (`review/policy.py`) for forms the rules can't see (`bash -c 'gh pr merge 12'`). The agent proposes; Purr posts what you approve.
- **The sandbox, optionally** (`--sandbox`): Claude Code's own, where the operating system confines the agents' commands to the PR's worktree and a few domains (GitHub, PyPI, npm), and they run without asking. Off by default: on Ubuntu 24.04 and later, AppArmor's `bwrap-userns-restrict` profile stops it (Claude Code's sandbox nests a second user namespace, which that profile refuses), so it only works once that's allowed.
- Why not Purr's own rules: an earlier version parsed commands to guess what was harmful. Shell is too flexible for that to be reliable, it asked about plenty of harmless things (`sed -n`, `cd`, `git -C`), and Claude Code already does this, better, with your rules.

## What the agent knows

Each PR's chat is Claude Code: the Agent SDK with Claude Code's own system prompt (`claude_code` preset) and built-in tools, plus what it needs for reviewing, in this order:

1. **Purr's review instructions**: how to review, the `submit_review` / `propose_action` / `get_review_threads` tools, read linked issues, never touch GitHub.
2. **The repo's own guidelines**: its `CLAUDE.md` and `AGENTS.md` (with `@file` imports resolved, as Claude Code does), read from the default branch rather than the PR, so a PR can't rewrite the rules it's reviewed against. For pipecat that's AGENTS.md: docstrings, deprecations, writing for future readers.
3. **The repo's own skills and agents**, also from the default branch (see Reviews), e.g. pipecat's `docstring` and `changelog` skills and its `code-review` workflow. Skills that are only for people (`disable-model-invocation`, like `code-review`) aren't offered to the agent, but a review can start with one: the repo's review skill (see Reviews), or any skill you type as a slash command.
4. **Your instructions**, which take precedence: plain Markdown files, like a CLAUDE.md, each for all repos and optionally per repo:
   - **Review style** (`review.md`): how reviews should go (tone, what to raise, what to skip).
   - **Code style** (`code.md`): what good code looks like to you, which reviews hold the PR to. Yours starts with: prefer a generic solution over special cases, avoid piling up flags to keep state, keep functions short.

   They live in `~/.config/purr/` (`review.md`, `code.md`) and `~/.config/purr/repos/<owner>/<name>/` for one repo. `i` asks which one to edit (`r` review style, `c` code style) and opens it in `$EDITOR`: the repo's inside a repo, the general one at home. `p` there opens your standing permissions, and inside a repo, `s` chooses which of its skills reviews follow. New files start with a short how-to in an HTML comment that isn't sent to the agent. Everything here is sent with every turn, so edits apply to sessions already under way.

Where it differs from Claude Code on purpose: nothing from the PR's own `.claude/` is loaded, since the PR could tamper with it. Skills and agents come from the default branch instead, and the repo's `settings.json` (hooks) isn't used at all. Tool permissions follow Purr's review policy.

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

## Bus messages

Jobs are requests one worker answers. Each is a dataclass in `src/purr/messages.py` that names the worker that handles it and its reply, also a dataclass: `class SendMessage(Request[MessageSent])`. The plumbing is in `src/purr/jobs.py`, once: a worker handles one with a method decorated `@handles(SendMessage)` that takes the request and returns the reply, and any worker (the TUI, the lead, a test) sends one with `request(sender, SendMessage(pr=..., text=...))`, which returns the reply. On the bus they're JSON (pydantic encodes the dataclasses), so they could cross a network bus later. A request that can't be done raises a `PurrError`, which goes back as itself (class and message), so a caller can tell `Busy` from `NotFound` from `Invalid`, `Failed` or `Unanswered` (no answer in time) without reading the text; anything else a handler raises is logged and goes back as a plain `PurrError`, so the asker hears about it at once rather than timing out.

Jobs:
- `PlanReviews {prs, depth}` (lead worker): plan and start reviews you asked for (TUI `r`, voice); answers once they've started, with why any didn't.
- `StartReview {pr, plan}` (reviews worker, sent by the lead; without a plan, a full review)
- `Rereview {pr, plan}` (reviews worker, sent by the lead on new commits)
- `SendMessage {pr, text}` (reviews worker): say something in a PR's conversation. Its reply says what that did besides: answered a permission request (a no, with directions), set proposals aside, or went to the agent at work.
- `AnswerPermission {request_id, allow, remember}` (reviews worker): answers an agent's permission request; `remember` (`repo` or `all`) also keeps its rule as a standing permission.
- `SetModel {pr, model}` (reviews worker): the model for a PR's conversation (`/model`).
- `ListRepoSkills {repo}` / `ListReviewSkills {repo}` (reviews worker): a repo's skills, for `/help` and completion; those that may be for reviewing, and which one its reviews follow.
- `ChooseReviewSkill {repo, skill}` (reviews worker): have its reviews follow `skill` ('' for Purr's own; none: ask again next run). Answers a `review.skill_question` too.
- `MakeRecap {since}` (when you come back, or on demand)
- `WatchRepo {repo}` / `UnwatchRepo {repo}` (GitHub worker; `repo` can be `owner/name` or a GitHub URL)
- `AssignMe {pr}` (GitHub worker): assigns you, when you start a review on a pool PR with `r`.
- `MarkSeen {pr}` (GitHub worker): you looked at a PR that had news; stores when, so it isn't news after a restart.
- `FetchDetails {prs}` (GitHub worker): description, base and head branches, +/- lines, files, labels, review decision and CI state of some PRs, batched 20 to a request.
- `GetDiff {pr}` (git worker): the PR's unified diff, from Purr's clone, or from GitHub if the clone doesn't have the PR's head yet.
- `GetWorktree {pr}` (git worker): the PR's worktree, made again if it was removed (for `c`).
- `ApproveAction {action_id}` / `DismissAction {action_id}` (GitHub worker). Approving claims the action atomically (so keys and voice can't post it twice), posts the GitHub review with its inline comments, and marks the PR Done once nothing is pending. If GitHub won't place an inline comment (line outside the diff), the comments go into the review body. If posting fails, the action goes back to pending for a retry. **What Purr posts says so:** the review's text and each reply in a thread end with `<sub>Sent from Purr.</sub>` (the `signature` setting; empty turns it off). Inline comments don't: they belong to the review, whose text already says it, and one under every comment would be noise. An approval with nothing to say is just the signature. The proposal in the PR's conversation shows it too, so `a` still posts exactly what you read.

Events (broadcast, everyone listens):
- `pr.list_updated` (repos, PRs or their states changed; re-read the store)
- `pr.changed {pr, change, title}` (from the poller: assigned, unassigned, pooled, unpooled, new_commits, closed, merged)
- `review.started {pr, plan}`
- `review.progress {pr, ...}`
- `review.finished {pr, summary, findings}`
- `action.pending {pr, action_id, description}`
- `action.resolved {pr, action_id, approved}`
- `review.failed {pr, error}`
- `session.message {pr, message}` (one streamed SDK message: text, tool call or tool result)
- `session.permission_request {pr, request_id, tool, description, reason}` / `session.permission_resolved {pr, request_id, allowed}`
- `review.skill_question {repo, skills}` (a repo's reviews wait to hear which of its skills to follow) / `review.skill_chosen {repo, skill, chosen_by}`
- `user.active` (from the TUI and the voice agent, throttled)
- `recap.ready {recap_id, headline}`

Events (`PurrEvent`) are `BusDataMessage` subclasses in `src/purr/messages.py`, broadcast (no target), with plain JSON fields (PRs as `owner/repo#123` strings), since everything that listens gets them, and most go into the activity log. Every event is also logged as one line, its values cut to 200 characters: a `session.message` carries whole tool results, which the session's transcript already keeps.

## SQLite store

- `repos`: name (`owner/name`), added_at, synced_at (first complete poll; until then its pool isn't reported as new). The watched repos, managed from the UI.
- `prs`: repo, number, title, author, url, head sha, state, gh_state (open/closed/merged), assigned_to_me, assignees, created_at, updated_at, base, seen_at (when you last looked at it; what happened before isn't news). Descriptions and files aren't stored; the UI caches them in memory.
- `reviews`: pr, session_id, worktree path, status, summary, started_at, finished_at
- `findings`: review, file, line, severity, text
- `pending_actions`: pr, action_id, kind (comment/request_changes/approve/merge), payload, status
- `prompts`: at, repo, number, text. What you asked PR agents, for the prompt's history (`↑`/`↓`, `ctrl+r`).
- `activity`: at, pr (optional), kind, data. Every recorded Purr event; feeds the recaps and each PR's timeline.
- `recaps`: since, until, headline, items, created_at
- `meta`: key/value, e.g. `last_active_at`

- `runs`: one per time an agent works on a PR: kind (`review`, `resume`, `rereview`, or `message` for one of yours), repo, number, review and session ids, queued_at / started_at / finished_at (UTC, ms; queued to started is time waiting for a slot), status (`queued`, `running`, `finished`, `failed`, `interrupted` by a shutdown), error, and what the SDK reports at the end: model, cost_usd, turns, duration_ms, api_duration_ms, input/output/cache read/cache write tokens.
- `messages`: every message of every run, in order: at (UTC, ms), run, repo, number, kind (`prompt`, `thinking`, `text`, `tool_use`, `tool_result`, `permission_request`, `permission_answer`, `result`, `error`), author (`purr`, `you`, `agent`, `tool`) and the message itself as JSON (tool results are capped at 2,000 characters, as in the transcript).

Both are written as things happen, so they're there for analysis even if a worktree or Claude Code's session files go. They're kept by repo and number, like `activity`, so they outlive the PR's row. Sessions continued in Claude Code itself (`c`) aren't recorded, and conversations from before these tables aren't either (Claude Code's session files have them, with timestamps, to import if needed). For example, with `sqlite3 ~/.local/share/purr/purr.db`:

```sql
-- What reviews cost and how long they took, by repo
SELECT repo, count(*), round(sum(cost_usd), 2), round(avg(duration_ms) / 1000) AS avg_s
FROM runs WHERE kind = 'review' AND status = 'finished' GROUP BY repo;

-- Which tools the agent reaches for
SELECT json_extract(data, '$.name') AS tool, count(*) FROM messages
WHERE kind = 'tool_use' GROUP BY tool ORDER BY 2 DESC;

-- How long permission requests waited for you
SELECT q.repo, q.number, (julianday(a.at) - julianday(q.at)) * 86400 AS waited_s
FROM messages q JOIN messages a ON a.run_id = q.run_id AND a.kind = 'permission_answer' AND a.id > q.id
WHERE q.kind = 'permission_request' GROUP BY q.id;
```

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
│ enter open · + add · 1-9 repo · ctrl+p · ctrl+o back · ctrl+n alert · w recap · 🎙     │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

- One row per watched repo, plus any repo where a PR is assigned to you.
- `+` adds a repo: type `owner/name` or paste a GitHub URL, with suggestions from repos you can access. Purr checks that the repo exists and starts polling it right away. `-` removes the selected repo (after confirming): its pool disappears, but PRs assigned to you there keep showing up, because those are reviewed regardless.
- First launch: no repos yet. The list shows only repos with PRs assigned to you, plus a hint to press `+`.
- Rows are sorted alphabetically by `owner/name` (case-insensitive) and numbered in that order, so the order and the `1`–`9` shortcuts only change when a repo is added or removed. Activity shows as counts and a ● marker; rows never move because of it.
- Launching `purr` inside a checkout of a watched repo opens that repo directly (`esc` goes up to this list). Launched anywhere else, Purr starts here.

### Repo screen

Two-pane, lazygit-style layout. Inside a repo, PRs show as `#2841`; everywhere else (alerts, recaps, the switcher) as `whisker#112`, the repo name without its owner unless that's ambiguous.

```
┌─ Purr › 2 pipecat-ai/pipecat ───────────────────────────────────────────── 🔔 1 · whisker#112 needs you ─┐
│   #       TITLE               ASSIGNED   AUTHOR OPENED │ #2841  Add message bus                          │
│ ⚠ NEEDS YOU (1)                                        │ by alice · opened 3d ago · main ← bus · +412 −30 │
│ ● #2841   Add message bus     you        alice      3d │ ● Review: found 2 issues, 1 nit · 1 pending act… │
│                                                        │                                                  │
│ ● REVIEWING (1)                                        │ Adds a message bus so workers can talk without   │
│ ● #2850   TTS context         you        bob        1d │ knowing about each other. …                      │
│                                                        │                                                  │
│ ✓ DONE (1)                                             │ 3 files changed  d full diff                     │
│ ✓ #2833   Frame cleanup       you        erin       6d │ src/pipecat/bus/bus.py                +380 −12   │
│                                                        │ …                                                │
│ ○ NOT REVIEWED (2)                                     │ ⏺ Clean refactor, tests pass. Concerns:          │
│ ○ #2861   Docs typo                      carol      2h │   1. Race in bus.subscribe() on shutdown         │
│ ○ #2855   Evals report                   dave       5h │                                                  │
│                                                        │ ┌──────────────────────────────────────────────┐ │
│ ◌ OTHERS REVIEWING (1)                                 │ │ Post 3 comments and request changes?          │ │
│ ◌ #2790   Krisp filter        grace      frank      2w │ │ [a] approve   [x] dismiss                     │ │
│                                                        │ └──────────────────────────────────────────────┘ │
│                                                        │ > is the race reachable from user code?          │
├────────────────────────────────────────────────────────┴──────────────────────────────────────────────────┤
│ esc home · o open · n/p next/prev · / search · r review · a approve · x dismiss · d diff · c Claude · ! sh │
└───────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Header**: where you are (`Purr › 2 pipecat-ai/pipecat`) and the latest alert from other repos.
- **Left pane**: the repo's PRs grouped by state, "Needs you" first and "Others reviewing" last, newest first within each group. Columns: number (in its own color), title, who's assigned (`you`, a login, `+1` for more), author, and how long ago it was opened (`2h`, `3d`, `5mo`) against the right edge.
- **Right pane**: the selected PR, top to bottom:
  - **Header**, which stays put: its title, then `by alice · opened 3d ago · main ← fix-bus · +22 −4 · ✓ checks pass · labels`, then where its review stands.
  - **What scrolls**: the PR's description, then its diff (colored like `git diff`, a rule per file with its `+/−`; lock files and other generated files collapsed; the first 3000 lines), then the session with the agent (Claude Code style, see PR session). A PR nobody has reviewed yet opens at its description, so you can decide whether to take it; a reviewed one opens at the end of the conversation.
  - **The pending action box and the prompt**, which stay put too. The prompt takes several lines and grows with them (up to 10).
  - `d` opens the full diff in your pager (`gh pr diff`).
- **Details are fetched ahead; diffs are local.** Selecting a PR fetches its details from GitHub (about 0.5 s), plus those of the 10 PRs below it in one request. Its diff comes from Purr's clone (`GetDiff` on the `git` worker, see Repos), in a few ms. Details are cached in memory and refetched when the PR is updated or after 2 minutes, since CI moves on without the PR changing; diffs are kept by head commit (the last 50).
- **Focus**: each pane has a rounded border, and the one with the focus lights up in the accent color, with its title (`PRs`, `PR`). `o` on a PR moves the focus into its pane (`enter` into its prompt); `tab` goes back and forth.
- **Status bar**: key hints for the focused pane, and mic state.
- **Selecting text**: drag over the PR pane (description, diff, conversation) and it's copied, as in a terminal; `ctrl+c` copies it too (Textual's binding). Textual's `RichLog` can't be selected in, so the pane is a `Transcript`, a `RichLog` that marks where each line's text is. Copying asks the terminal (OSC 52, which works over ssh) and also runs the system's copy command if there's one (`wl-copy`, `xclip`, `xsel`, `pbcopy`): GNOME Terminal and the other VTE terminals ignore OSC 52, so there it takes `wl-clipboard` or `xclip`, and Purr says so. `shift`+drag selects with the terminal itself, anywhere.
- **Colors**: Solarized. Textual's `solarized-dark` theme does the widgets; Rich's named colors in Purr's text (`green`, `red`, ...) map to Solarized's terminal palette, and the few colors Purr picks itself (PR numbers, the working line) come from the same sixteen (`tui/theme.py`). Code in descriptions and replies uses Pygments' `solarized-dark`.

### Alerts from other repos

News is something you'd want to know that happened since you last looked at a PR: it was assigned to you, its review finished or failed, or its agent needs your OK. Review progress doesn't count.

- **The top bar says one short thing about other repos**, on every screen, so you know to go and look: the latest news there if there's any (`🔔 2 · whisker#112 review: found 2 issues  (ctrl+n)`), otherwise how many PRs need you there (`⚠ 3 need you in other repos  (0)`). `0` goes to the repo list, which says where: a ● after each repo with news, and its NEEDS YOU count.
- **In a repo, PRs with news have their title in bold**, like unread mail, until you look at them. News about the PR you're looking at isn't news.
- `ctrl+n` jumps to the newest news in another repo (or in this one if there's none elsewhere); pressing it again goes through the rest.
- **It survives restarts.** Looking at a PR stores when (`prs.seen_at`, through a `MarkSeen` job), and on startup the news comes from the activity log: what happened to open PRs since you last looked at them.

### Moving around

Global keys work everywhere, even while typing in a PR prompt:
- `ctrl+p` quick switcher: fuzzy search over repos and PRs (number, title, author), most recent first. Built on Textual's command palette.
- `ctrl+o` back to where you were before the last jump (repo and PR); press it again to return, like `cd -`.
- `ctrl+n` jump to the newest alert from another repo.
- push-to-talk key (hold) for voice.

List and pane keys (letters and numbers act on lists and panes; while a prompt has focus, typing goes to the agent, as in Claude Code):
- `0` repo list, `1`–`9` repo by number
- `+` add a repo, `-` remove one (repo list)
- `o` or `enter` open: a repo, or a PR (focus moves into its pane). In a PR's pane, `enter` goes to its prompt.
- `tab` (or `shift+tab`) switches between the PR list and the PR's pane, from its prompt too (the prompt never takes a tab).
- `n` / `p` next / previous: repo, or PR. They only change which PR the PR's pane shows, never the focus, so from the pane you read PRs one after another.
- `/` search the PR list as you type (number, title, author, assignees; every word has to match). `enter` goes back to the list, keeping the results. The search stays (shown above the list, per repo, also after leaving it) until you press `/` and delete it.
- `esc` goes home in one step, from the list, the search box, the PR or its prompt. Nothing's lost: the search and anything half-typed are there when you're back. Only the `/` suggestions close first.
- `page up` / `page down` scroll a page, in either pane (the transcript while typing too)
- `space` mark/unmark PR
- `r` start review on marked PRs (or the selected one), assigning you
- `d` the selected PR's full diff in your pager
- `a` / `x` answer the box under the transcript: approve or dismiss a pending action, allow or deny a permission request
- `z` zoom the PR session to full screen
- `c` open the PR's session in Claude Code
- `i` edit your review style or code style (this repo's inside a repo, the general ones at home)
- `w` open the latest "while you were away" recap

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
2. GitHub poller: watched repos table, the pool (unassigned open PRs) plus PRs assigned to you in any repo, PR list in SQLite, activity log. (Done.)
3. TUI shell: repo list (add/remove repos), repo screen with the PR list, navigation keys, live updates from bus events. Comes early because repos are only added from the UI, and it makes the poller visible. (Done. Measured with pipecat's 331 PRs: UI up with data in 0.5 s, opening the repo 35 ms, back home 16 ms, reopening 7 ms.)
4. Review worker: Purr-managed clone and worktree, Agent SDK session with the GitHub guardrails, store session ID, summary, findings, pending actions, stream `session.message` events. (Done, except interactive permission prompts, which come with the PR session UI.)
5. Pending action execution (post comments / review verdict / merge) gated on approval. (Done for reviews: approve / request changes / comment. Merging isn't proposed yet. Tested against a fake GitHub; not yet run against a real PR.)
6. Rest of the TUI: Claude Code-style PR session (transcript, prompt, permission prompts, zoom), alerts from other repos, quick switcher. (Done. Transcripts come from the SDK's session file plus live `session.message` events. Alerts survive restarts: `prs.seen_at` plus the activity log.)
7. "Start from app" also assigns you on GitHub. (Done: `r` on a pool PR starts the review and assigns you.)
8. While you were away: activity tracking, recap agent, recap panel. (Done. The TUI decides you're back: the first key after `away_after` (15 min) of nothing, counting time Purr was closed, since the last-active time is kept in `meta`. It opens the panel with the raw events, and asks the recap worker (`MakeRecap {since}`) for the summary: a tool-less Agent SDK query with structured output, about 5 s, falling back to a plain list if it fails. `w` reopens the latest.)
9. Open a PR's session in Claude Code (`c`). (Done. `claude --resume` keeps the session ID; `--fork-session` is what would make a new one. It runs with `--setting-sources user`, GitHub writes disallowed, and a note that Purr's own tools aren't available there. Purr keeps running underneath, and reloads the transcript afterwards.)
10. The lead: plans every review (quick or full, priority, focus) before it starts, never skips one, and every review says whether it found something. (Done; see The lead.)
11. Voice agent with push-to-talk, on the same planning.

## Open questions

- Exact review prompt and output schema for findings/pending actions.
- How to map findings to GitHub review comments (inline vs. general).
- STT/LLM/TTS choices for the voice agent.
- How long without activity counts as "away" (default 15 minutes?).
