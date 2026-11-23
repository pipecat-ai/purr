<h1><div align="center">
 <img alt="pipecat" width="300px" height="auto" src="https://raw.githubusercontent.com/pipecat-ai/pipecat/main/pipecat.png">
</div></h1>

[![Tests](https://github.com/pipecat-ai/purr/actions/workflows/tests.yaml/badge.svg)](https://github.com/pipecat-ai/purr/actions/workflows/tests.yaml) [![Docs](https://img.shields.io/badge/Documentation-blue)](https://docs.pipecat.ai) [![Discord](https://img.shields.io/discord/1239284677165056021)](https://discord.gg/pipecat)

# ᓚᘏᗢ Purr

**Purr** is a human-in-the-loop agentic code reviewer. It watches your GitHub repos, and
Claude reviews the pull requests assigned to you in the background. You read what it found,
ask it questions, and decide what gets posted. **Nothing goes to GitHub until you say so.**

![Purr: a review that needs you, a question to the agent, a comment on a line of the diff, and posting the review](https://raw.githubusercontent.com/pipecat-ai/purr/main/docs/purr.gif)

## ✨ What it does

- **It reviews what's assigned to you** as it comes in, a few at a time. A quick planning
  step decides how deep: small changes get a quick look, risky ones a full review.
- **It knows what's waiting on you.** PRs are grouped: *Needs you* (a review to post, a
  question from the agent, someone replying to you on GitHub), *Reviewing*, *Review
  requested*, *Others reviewing*, *Done*, *Not reviewed*. News from other repos shows at the
  top.
- **Every review is a conversation.** You can keep talking to the agent about the PR ("is that
  race real?", "drop the nit"), and Purr suggests what to ask next.
- **You can comment on lines of the diff, and on the PR.** Yours go to GitHub as you save
  them; the agent's wait for you to approve its review.
- **Posting and merging are a key each.** `a` posts the review as you. `m` shows the checks,
  the other reviewers and any conflicts, then merges what you approved.
- **It follows several repos at once**, and recaps what happened while you were away.
- **It's safe by default.** The agents ask before doing anything risky, can run in a sandbox,
  and follow the repo's rules from its main branch, so a PR can't change what it's reviewed
  against.

## 🚀 Setup

You need a terminal on macOS, Linux or Windows, [uv](https://docs.astral.sh/uv/), the
[GitHub CLI](https://cli.github.com) and git. You don't need Claude Code: Purr runs its
agents with the [Claude Agent SDK](https://docs.claude.com/en/docs/agent-sdk/overview).

```sh
uv tool install pipecat-ai-purr
gh auth login    # GitHub: follow the prompts
purr login       # Claude (skip it if you use Claude Code already)
purr
```

Reviews use your Claude login, so they count against your Claude plan (or your API key, if you
set `ANTHROPIC_API_KEY`). To look around without starting any reviews, run
`purr --no-auto-review`.

## 🧭 How to use it

1. **Pick a repo** at home (`enter`, or its number). Purr lists the repos with PRs assigned to
   you; `+` watches one, to see all its open PRs.
2. **Pick a PR.** On the right, its description, where it changes things, and the
   conversation with the agent. `r` reviews one that isn't yours yet (and assigns you), its pane
   full screen to follow it (`z` or `esc` comes back); `o` opens it on GitHub.
3. **Talk to the agent** (`enter` goes to the prompt): ask, dig in, or change what it
   proposes. `tab` takes the suggestion. `esc` in its pane stops it while it works.
4. **Read the diff** (`d`): it takes the list's place, next to the conversation, the code in its
   language's colors on green and red; `tab` on a change's `@@` line shows the lines around it.
   Select lines (`shift+↑`/`↓`) and press `enter` to ask the agent about them, or `c` to
   comment on them.
   Comments show in boxes under their lines (yours, the agent's and GitHub's), and what's
   said on the PR itself is at the top. Yours go to GitHub when you save them: `enter` on one
   edits it there and `x` deletes it (after asking); `r` on anyone's replies, quoting it.
   `esc` or `d` closes the diff, `z` makes it full screen.
5. **Decide**: `a` posts the agent's review, `x` dismisses it (in the diff, `x` drops one of
   its comments). Once you've approved, `m` merges.

When the agent asks before doing something risky, `a` allows it, `A` always, `x` says no (or
write what to do instead). When someone replies to you on GitHub, the PR is back in
*Needs you*: answer, or `x` if it needs no reply.

### ⌨️ Keys

The footer shows the keys for where you are; `?` lists all of them, including the ones that
work everywhere. The main ones:

| Key | Does |
|---|---|
| `?` | Every key, grouped |
| `enter` / `tab` | Ask the agent / switch between the list and the PR (on a group's heading: fold or open it) |
| `o` | The PR on GitHub, in your browser (at home, the repo) |
| `n` `p` / `/` | Next or previous PR / search |
| `r` / `space` | Review the PR (once reviewed, it asks before starting over) / mark several first (`esc` unmarks them) |
| `a` `x` / `A` | Approve or dismiss what's proposed (or allow or deny what's asked) / always allow |
| `d` / `D` | The diff, to read (`n` `p` from change to change), ask about (`enter`) and comment on (`c`) / in your pager |
| `shift+↑` `shift+↓` | In the diff: select lines (`ctrl+space` sets a mark, as in Emacs) |
| `tab` | In the diff: open or shut a file or a comment; on a change's `@@` line, the lines around it |
| `m` | Merge a PR you approved: once to see where it stands, again to merge |
| `t` / `z` | The agent's reasoning in full / full screen |
| `!` / `c` | A shell in the PR's checkout / its session in Claude Code (`/exit` comes back) |
| `ctrl+p` / `ctrl+n` / `w` | Find any repo or PR / the latest news / what happened while you were away |
| `i` | Settings: your review and code style, permissions, the repo's review skill, Purr's settings and theme |
| `esc` / `q` | Back a step: out of full screen; in the PR's pane, stop the agent while it works; out of the prompt or the diff; a search, cleared; then home / quit |

The mouse works too: the wheel scrolls the list, the PR and the diff, and a click in the diff
puts the cursor on a line (`shift`+click selects up to it).

Wherever you write (the prompt, a comment, a file in Purr's editor), Emacs's keys move and
edit: `ctrl+a` `ctrl+e` `ctrl+b` `ctrl+f` `M-b` `M-f` `ctrl+k` `ctrl+y` …, and `ctrl+space` sets
the mark to select from, along with the usual `shift`+arrows, `ctrl+x` `ctrl+c` `ctrl+v` and
`ctrl+z`. `esc` or `ctrl+g` lets go of a selection first. In the prompt, `enter` sends,
`ctrl+j` starts a new line, `↑` `↓` and `ctrl+r` go through what you asked, and `/` lists
commands: `/help`, `/review [quick|full]`, `/model`, `/cost`, `/theme`, `/compact`, and the
repo's own skills. Reading, `ctrl+v` and `alt+v` page down and up, as in Emacs.

## 🎨 Make it yours

`i` opens the settings. Each file opens in Purr's own editor: `ctrl+s` saves, `esc` goes back to
the settings. Purr checks `settings.toml` and the permissions before saving them, so a typo
never reaches the file.

| File | What it's for |
|---|---|
| `~/.config/purr/review.md` | How you like reviews: what to raise, what to skip, tone |
| `~/.config/purr/code.md` | What good code looks like to you; reviews hold PRs to it |
| `~/.claude/settings.json` | What the agents may run without asking (the file Claude Code uses too) |
| `~/.config/purr/settings.toml` | Models, how often to poll, how many reviews at once, the theme, ... (`purr --help` for flags) |

Instructions and permissions apply from the agent's next turn; settings, when Purr next starts.
Inside a repo, `i` edits that repo's own instructions and permissions, which add to the
general ones (they're in `~/.config/purr/repos/<owner>/<name>/`), and which of its skills its
reviews follow. `t` there (or `/theme`) picks a theme, showing each as you move through the
list; it applies at once.

## 💡 Good to know

- **Purr never posts on its own.** The agents can read GitHub but not change it: they propose,
  and Purr posts what you approve, as you, signed "Sent from Purr." The agent's review goes out
  with `a`; your own comments, replies and edits go out when you save them, and deleting one
  asks first.
- **The agents ask before anything risky.** Reading code and safe commands just run; anything
  else waits for your OK, and `A` keeps a rule so it doesn't ask again. This is the Agent SDK's
  permission system (Claude Code's, underneath), so the rules go in `~/.claude/settings.json`.
  `--sandbox` confines the agents' commands to the PR's checkout, on macOS and Linux (on
  Ubuntu 24.04+, AppArmor has to allow it, so it's off by default).
- **Costs.** `/cost` shows what each PR has cost. Quick reviews run on Sonnet;
  `--max-reviews 1` keeps things small.
- **Where things live**: the database, log and PR checkouts in `~/.local/share/purr`.
- **Copying**: drag over the PR pane. GNOME Terminal needs `wl-clipboard` (or `xclip`) for it.

## 🛠️ Development

```sh
git clone git@github.com:pipecat-ai/purr.git
cd purr
uv sync
uv run purr                            # or: uv tool install --editable .
uv run pytest                          # never calls GitHub or Claude
uv run ruff check && uv run ruff format
uv run python scripts/record_demo.py   # the GIF above, from made-up PRs
```

How Purr works and why: [`docs/purr.md`](https://github.com/pipecat-ai/purr/blob/main/docs/purr.md).

Changes are recorded as [towncrier](https://towncrier.readthedocs.io/) fragments in
`changelog/`: a file per change, `<PR number>.<type>.md` (`added`, `changed`, `deprecated`,
`removed`, `fixed`, `performance`, `security` or `other`), with a bullet saying what changed.
`uv run towncrier build --draft --version X.Y.Z` shows what the next release's notes in
[`CHANGELOG.md`](https://github.com/pipecat-ai/purr/blob/main/CHANGELOG.md) will say.
To release, run the **generate-changelog** workflow with the version: it opens a pull
request with the release's notes. Once it's merged, tag it `vX.Y.Z` (the version comes
from the tag, as Pipecat's does) and run the **publish** workflow with the tag.

## 📄 License

BSD 2-Clause. See [LICENSE](https://github.com/pipecat-ai/purr/blob/main/LICENSE).
