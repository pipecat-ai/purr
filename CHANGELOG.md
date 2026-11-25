# Changelog

All notable changes to **Purr** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- towncrier release notes start -->

## [0.3.0] - 2026-11-24

### Added

- The agent can propose a push: ask it to change something and push it, and it
  commits in the PR's worktree. Purr shows which commits go to which branch (a
  contributor's fork too, if they allow edits by maintainers), and `a` pushes
  them, only if nobody pushed there since. The agent still can't push by
  itself.
  (PR [#10](https://github.com/pipecat-ai/purr/pull/10))

- A PR in *Needs you* with nothing proposed (*No verdict yet*, say after you
  dismissed the agent's proposal) can be approved with `a`: the footer says
  *Approve PR*, and Purr posts the approval as it would an approval the agent
  proposed.
  (PR [#15](https://github.com/pipecat-ai/purr/pull/15))

### Changed

- A PR is *Done* only once you approve it or request changes, at its current
  head. Posting a comment, dismissing a proposal, or a follow-up that ends
  without proposing anything leaves it in *Needs you*. Your own PRs are done
  once nothing waits on you, as before.
  (PR [#9](https://github.com/pipecat-ai/purr/pull/9))

- In a repo's list, *Done* comes right after *Review requested*, before *Others
  reviewing*: what you finished lately is with what's yours.
  (PR [#11](https://github.com/pipecat-ai/purr/pull/11))

- `a` says what it'll do, in the footer and the decision box: *Post comment*,
  *Approve PR*, *Request changes* or *Push* for the agent's proposal, *Allow
  once* for its question, *Post comments* for yours. With nothing for it to do,
  it isn't shown.
  (PR [#12](https://github.com/pipecat-ai/purr/pull/12))

- `d` opens the diff full screen. `z` or `esc` shows the PR's conversation next
  to it, and `d` closes it, back to the list and the PR.
  (PR [#13](https://github.com/pipecat-ai/purr/pull/13))

- Each PR in *Needs you* says what it needs you for, after its title: the
  agent's question, its proposal (`Request changes?`, `Approve PR?`, `Push?`),
  your comments, replies, a failed review, or `No verdict yet`. Its mark is ⚠
  (✗ if the review failed) rather than what the review found, which read as if
  only some of them needed you.
  (PR [#14](https://github.com/pipecat-ai/purr/pull/14))

- A verdict you give on GitHub counts: a PR you approved or requested changes
  on there, at its current head, moves to *Done* as if you'd done it through
  Purr, and one you approved can be merged with `m`. What the agent proposed to
  approve it or request changes is then dismissed; its other proposals still
  wait on you.
  (PR [#16](https://github.com/pipecat-ai/purr/pull/16))

### Fixed

- After Purr pushes the agent's commits, they're the PR's head right away: `a`
  next approves what you pushed, not the commit before, and the next poll no
  longer re-reviews the agent's own commits. In the merge box, `x` closes it,
  `m` says so when checking again changes nothing, and Purr polls right away
  when GitHub has commits it hasn't seen yet.
  (PR [#17](https://github.com/pipecat-ai/purr/pull/17))

## [0.2.0] - 2026-11-24

### Added

- What others say on your own PRs (comments, review threads, the text of their
  reviews) needs you, as replies to you do on PRs you reviewed.
  (PR [#4](https://github.com/pipecat-ai/purr/pull/4))

- Your own open PRs are listed too, drafts included: in a *Your PRs* group in
  their repo, and counted at home.
  (PR [#4](https://github.com/pipecat-ai/purr/pull/4))

- When someone comments on one of your PRs, the agent reads it, checks the
  code, and proposes what to change and what to answer, for you to post
  (`auto_answer`, on by default).
  (PR [#4](https://github.com/pipecat-ai/purr/pull/4))

- `s` on an *Up next* PR says it isn't for you: it leaves for good, and the
  lead learns from what you skip and what you take.
  (PR [#5](https://github.com/pipecat-ai/purr/pull/5))

- *Up next*: in each watched repo, the lead suggests a few PRs from the pool
  worth looking at next (small ones, in code you know), with why (`up_next`,
  5).
  (PR [#5](https://github.com/pipecat-ai/purr/pull/5))

### Removed

- Purr no longer adds "Sent from Purr." to what it posts, and the `signature`
  setting is gone: take it out of `settings.toml` if you set it, or Purr won't
  start.
  (PR [#3](https://github.com/pipecat-ai/purr/pull/3))

- The *While you were away* recap is gone, and `w` with it: what happened since
  you last looked at a PR shows as news. Take `away_after` out of
  `settings.toml` if you set it, or Purr won't start.
  (PR [#7](https://github.com/pipecat-ai/purr/pull/7))

### Fixed

- Quitting while a PR's pane was still loading no longer ends with an error.
  (PR [#2](https://github.com/pipecat-ai/purr/pull/2))

## [0.1.0] - 2026-11-23

Initial public release.
