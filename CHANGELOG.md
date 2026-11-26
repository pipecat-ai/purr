# Changelog

All notable changes to **Purr** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- towncrier release notes start -->

## [0.4.0] - 2026-11-26

### Added

- `a` and `m` show what they started as soon as you press them. The line above
  the prompt says `Pushing…`, `Posting…`, `Approving…`, `Looking at its checks
  and reviews…` or `Merging…`, with a spinner, in place of the decision box,
  and the PR's mark in the list spins. A push or a merge takes a few seconds,
  and before this nothing on screen changed until it was done. Pressing a key
  again meanwhile does nothing.
  (PR [#21](https://github.com/pipecat-ai/purr/pull/21))

- The agent can rebase a PR (or squash or reword its commits), and `a`
  force-pushes it: `Force-push 3 commits, up to e0b9581, to eric7h:fix/units,
  replacing its 2 commits?`. As with any push, only if nobody pushed to the
  branch since. The worktree keeps the rebase until the PR gets new commits on
  GitHub, and a rebase stopped on a conflict stays for the agent to finish.
  (PR [#28](https://github.com/pipecat-ai/purr/pull/28))

- The diff (`d`) shows what the agent committed before you push it, and says
  how many commits aren't pushed yet. It updates when the agent's turn ends,
  and after a push. Comments on lines move with them. Until you push, `c` can't
  comment on lines, since GitHub doesn't have them yet.
  (PR [#32](https://github.com/pipecat-ai/purr/pull/32))

- While CI runs, the merge box (`m`) lists every check, like GitHub's page: a
  spinner on each one still running, how long each has taken, and it updates
  every 5 seconds.
  (PR [#34](https://github.com/pipecat-ai/purr/pull/34))

- PRs you approved that are still open have their own group, *To merge*, under
  *Needs you*, so they no longer get lost in *Done*. The home screen counts
  them per repo.
  (PR [#36](https://github.com/pipecat-ai/purr/pull/36))

- When GitHub holds a PR's workflows for approval (a fork's PR, by a first-time
  contributor), Purr says CI hasn't run instead of "checks pass": the header
  says `⏸ CI needs approval`, and the merge box (`m`) lists the waiting
  workflows and offers `a` to approve and run them, then follows them as they
  run.
  (PR [#44](https://github.com/pipecat-ai/purr/pull/44))

- Once you merge a PR, or request changes on it, the cursor goes on to the next
  PR to merge, or else the next that needs you, and the note says which. If you
  moved to another PR while it merged, it stays where you put it.
  (PR [#60](https://github.com/pipecat-ai/purr/pull/60))

### Changed

- The footer no longer shows `a`, `A`, `x` and `m`. The decision box already
  says them, with what each does (`[a] request changes   [x] dismiss`, `[a]
  allow once   [A] always…`, `[m] merge`), and `A Always allow` showed even
  when nothing was asked. They work as before, and `?` lists them, saying what
  `a` does now.
  (PR [#23](https://github.com/pipecat-ai/purr/pull/23))

- `esc` in the prompt always goes back to the PR's pane, even while the agent
  works; it no longer stops the agent there. To stop it, press `esc` in the
  pane (from the prompt, `esc` twice). Leaving the prompt could cut short what
  the agent was doing, a rebase halfway through, say.
  (PR [#25](https://github.com/pipecat-ai/purr/pull/25))

- The box above the prompt shows only when something needs you. It hides while
  you type, and while the agent works on the PR, and comes back once it's done.
  The agent's request to run something stays up while you type, since what you
  type is the answer.
  (PR [#30](https://github.com/pipecat-ai/purr/pull/30))

- The mouse wheel scrolls a line at a time, so scrolling is smoother.
  (PR [#33](https://github.com/pipecat-ai/purr/pull/33))

- *Review requested* now comes before *Up next* in the PR list: someone asking
  you, usually a teammate, comes before Purr's picks.
  (PR [#37](https://github.com/pipecat-ai/purr/pull/37))

- When GitHub hasn't yet worked out whether a PR can be merged, the merge box
  (`m`) shows a spinner and the checks, and asks GitHub again every 2 seconds
  until it knows, instead of a warning that never goes away.
  (PR [#43](https://github.com/pipecat-ai/purr/pull/43))

- Scrolling with keys is smooth: page up/down, and going to the top or the end,
  glide there in 0.15 s, in the diff and the PR's conversation, instead of
  jumping (in the diff) or taking up to a second (in the conversation). Moving
  a line is still instant.
  (PR [#45](https://github.com/pipecat-ai/purr/pull/45))

- Purr is quicker:
    - Saving to its database no longer waits for the disk after every write,
      about 0.2 ms each instead of 7.
    - A big diff lays out about 6 times faster when you open or close a file, a
      comment or hidden lines.
    - A poll no longer rewrites PRs you gave a verdict on.
    - The PR's header skips reads it doesn't show.
    - Marking a PR redraws only its row.
    - The `/` suggestions no longer reload the PR shown.
  (PR [#46](https://github.com/pipecat-ai/purr/pull/46))

- Starting a review (`r`, `/review`) no longer zooms in on the PR's pane: the
  list stays, to start the next one right away. `z` still zooms.
  (PR [#49](https://github.com/pipecat-ai/purr/pull/49))

- Open PRs you requested changes on are in a new *Waiting on author* group, not
  in Done: the PR isn't done, its author has to answer. A reply to you, or new
  commits, bring it back to Needs you.
    - Done now shows the PRs you reviewed once they're merged or closed, the
      latest first. Before, a merged PR left the list. Merged or closed, a PR
      has nothing left to decide or review.
  (PR [#50](https://github.com/pipecat-ai/purr/pull/50))

- The merge box (`m`) keeps following CI and GitHub when you move to another
  PR, another repo or home. Its PR's mark in the list spins until it's done,
  and when you come back the box is there, up to date.
  (PR [#51](https://github.com/pipecat-ai/purr/pull/51))

- The PR list is quicker: opening a big repo, each poll's update and closing
  the diff no longer stop Purr for half a second (pipecat: 580 ms to 110 ms to
  open, 270 ms to 50 ms for a poll), and moving the cursor costs less than half
  what it did. Rows look the same.
  (PR [#52](https://github.com/pipecat-ai/purr/pull/52))

- A poll that changes little on the PR list redraws only the rows that look
  different, instead of the whole list.
  (PR [#53](https://github.com/pipecat-ai/purr/pull/53))

- Holding an arrow key down in the PR list is lighter: the PR's pane catches up
  every 100 ms and where you stop, instead of on every row. One key shows the
  PR at once, as before. The key bar is drawn again only when its keys change,
  which also makes closing the diff quicker (150 ms to 45 ms on pipecat).
  (PR [#54](https://github.com/pipecat-ai/purr/pull/54))

- Fewer hiccups: Python's garbage collector no longer goes through everything
  Purr loaded at startup on each full pass, which froze Purr for about 100 ms
  now and then (moving the cursor, paging the diff); now about 15 ms.
  (PR [#56](https://github.com/pipecat-ai/purr/pull/56))

### Removed

- Purr no longer lists the PRs you opened, or drafts answers to comments on
  them (the `auto_answer` setting is gone, and ignored if your `settings.toml`
  still has it). Work on your own PRs in Claude Code. The *Your PRs* group and
  the home screen's `YOURS` column are gone too, and so are repos that were
  only listed for a PR of yours.
  (PR [#42](https://github.com/pipecat-ai/purr/pull/42))

### Fixed

- Right after Purr pushes the agent's commits, a poll no longer takes the head
  from before the push for new commits. GitHub's search can lag behind a push,
  so when it says a reviewed PR's head moved, Purr now reads the head from the
  PR itself. Before, the PR was re-reviewed at the old head, and the agent
  proposed again what the push had just fixed (a comment asking for the change,
  say).
  (PR [#19](https://github.com/pipecat-ai/purr/pull/19))

- `r` moves a PR straight to *Reviewing* while the lead plans its review. It
  used to land in *Not reviewed* first (assigning you made it yours, with no
  review yet), and the cursor followed it there and back. You're now assigned
  once the review starts.
  (PR [#20](https://github.com/pipecat-ai/purr/pull/20))

- A PR whose worktree was left halfway through a rebase (the agent stopped
  while it was stopped on a conflict) failed every turn after, with `could not
  write index`. Purr now aborts a rebase, merge, cherry-pick or revert left
  halfway when it has to move the worktree, which puts it back where it began.
  A merge on top of the PR's head is still left for the agent to finish. Git
  errors also say which command failed (`git stash push failed: …`), not the
  worktree's path.
  (PR [#26](https://github.com/pipecat-ai/purr/pull/26))

- A push proposal with a merge of main in it counted and listed main's commits
  too (`Push 40 commits…`). It now lists only the commits made on the PR's
  branch, the merge being one of them.
  (PR [#27](https://github.com/pipecat-ai/purr/pull/27))

- The merge box (after `m`) no longer stays above what's next. It goes when you
  write to the agent, when the agent proposes something, or when the PR has new
  commits.
  (PR [#31](https://github.com/pipecat-ai/purr/pull/31))

- The PR header no longer cuts `✓ checks pass · approved` in two when the line
  is too long: it moves to the next line whole.
  (PR [#39](https://github.com/pipecat-ai/purr/pull/39))

- Purr notices again when PRs are merged or closed. The search for your own PRs
  counted PRs with the same number in different repos (a #1 in each of several)
  as one, looked incomplete on every poll, and was skipped, which stopped Purr
  from checking which PRs were gone.
  (PR [#40](https://github.com/pipecat-ai/purr/pull/40))

- A PR you just merged no longer comes back as open when GitHub's search hasn't
  caught up yet. Purr asks GitHub for the PR's state first, and only a reopened
  PR is open again.
  (PR [#41](https://github.com/pipecat-ai/purr/pull/41))

- Bugs found in a review of the code:
    - A PR assigned to you that you had commented on wasn't followed for
      replies.
    - An approved action got stuck if `gh` couldn't be run.
    - Stopping the agent just as it started left the PR under way until a
      restart.
    - Moving to another PR while the agent or GitHub answered could put your
      message on the wrong PR, or bring back the merge box.
    - Leaving the repo didn't drop the merge box.
    - Files with names that aren't plain ASCII had the wrong path in the diff.
    - Selecting lines downwards didn't highlight the first one.
    - Review threads could land in another PR's diff.
    - Marked PRs were reviewed in no particular order.
    - The overview said "most in  (2)" for files at the repo's top, and "opened
      now ago".
  (PR [#47](https://github.com/pipecat-ai/purr/pull/47))

- The PR's pane fills its width again after the diff had the whole screen: what
  was written to it meanwhile (its details, the agent's progress) was wrapped
  at 20 columns.
  (PR [#48](https://github.com/pipecat-ai/purr/pull/48))

- New commits on a PR you'd approved or requested changes on bring it back to
  Needs you ("New commits since your verdict"). Without auto-review, it used to
  stay in Done.
  (PR [#50](https://github.com/pipecat-ai/purr/pull/50))

- Quitting Purr while a merge box waited on GitHub could end in an error.
  (PR [#55](https://github.com/pipecat-ai/purr/pull/55))

- Quitting Purr right after moving through the PR list quickly could end in an
  error.
  (PR [#57](https://github.com/pipecat-ai/purr/pull/57))

- PRs in Done show Done's ✓, not what their review found: a red ● on a PR you
  approved and merged read as something still wrong.
  (PR [#58](https://github.com/pipecat-ai/purr/pull/58))

- Quitting Purr while a list was being redrawn (on news from GitHub or the
  agent) could end in an error.
  (PR [#61](https://github.com/pipecat-ai/purr/pull/61))

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
