# Changelog

All notable changes to **Purr** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- towncrier release notes start -->

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
