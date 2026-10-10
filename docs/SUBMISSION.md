# PRGuard: hackathon submission

Copy these into the Devfolio project form.

## Project name

PRGuard

## Tagline

An AI code reviewer that stays safe even when a pull request turns hostile.

## The problem it solves

AI review bots read text written by strangers (the pull request) while holding
write access to the repository. In January 2025, Kudelski Security showed that
one crafted PR made CodeRabbit run attacker code on its server, where a single
GitHub App key gave write access to about a million repositories.

PRGuard is a working AI pull-request reviewer with a CI fixer, built so that a
hijacked reviewer can do almost nothing. Every GitHub action an agent takes
passes through an AgntID runtime, which allows or denies it at execution time
for this one task: this repository, this pull request, read and comment only.

## What it does

- Five specialist agents review each PR in parallel (security, quality, tests,
  docs, CI fixer). An aggregator merges their findings and an independent model
  from a different family re-checks them before anything is posted.
- The CI fixer reads the failed GitHub Actions log for the PR's commit and
  explains the root cause. It can re-run jobs but can never edit the pipeline.
- PR code (linters, tests) runs in a locked Docker sandbox: no network, no
  secrets, read-only filesystem.
- Agents never hold a GitHub token. It lives only in PRGuard's own 13-tool
  GitHub MCP server, which the AgntID runtime fronts.
- A live dashboard shows every agent, every allow and deny, and the review.

## How AgntID is used

| AgntID control | How PRGuard uses it |
|---|---|
| Customer-hosted runtime | The only path from any agent to GitHub |
| Tool level (protection profile) | Push, delete and re-run tools are hidden from agents |
| Parameter-level policies | Every tool pinned to one owner and one repository |
| Custom Rego | No secret file paths on reads; reviews limited to COMMENT or REQUEST_CHANGES; merge and file writes always denied, each with a readable reason |
| Task and delegation | One task per review; one delegation per specialist with its own agent id, intent and tool allowlist |
| Audit | Every allowed and denied call is recorded by the runtime |

## Before and after (measured on the real repository)

Eight attack steps, replayed as if one agent had been hijacked:

| Attack step | Without AgntID | With AgntID |
|---|---|---|
| Read the PR's files | allowed | allowed (legitimate) |
| Read `.env` | went through | denied: secret path |
| Approve the PR | reached GitHub | denied: agents cannot approve |
| Merge the PR | really merged | denied: never permitted |
| Push a file to `main` | really pushed | denied: tool not exposed |
| Read another repository | went through | denied: wrong repository |
| Comment on a different PR | went through | not stopped (see limits) |
| Edit the CI workflow | reached GitHub | denied: never permitted |

## Honest limits

- One step is not stopped: a comment on a different PR in the same repository.
  The policies pin the repository, not the PR number, and the runtime's intent
  check reported "skipped" on every call during the event.
- Human approval is not wired through AgntID: the APPROVE rule denies agents
  unconditionally.
- AgntID has no GitHub credential type yet (GitHub App is "coming soon"), so
  the token is held by PRGuard's MCP server rather than brokered per call.

## Challenges we ran into

- Naming the attached server `github` made the runtime demand a GitHub App
  credential and fail every call at credential resolution; renaming it fixed it.
- The runtime kept an expired session to our MCP server after a policy sync;
  making the server stateless fixed it.
- Cheap models name the same issue differently, so duplicate findings are
  merged by file, line range and topic rather than by category name.

## Technologies

Python, FastAPI, LangGraph, AgntID runtime and Python SDK, Model Context
Protocol (FastMCP), Open Policy Agent Rego, Docker, React, React Flow,
Tailwind, OpenRouter (gpt-oss-120b reviewers, Gemini 2.5 Flash Lite checker).

## Links

- Code: https://github.com/kazuma761/prguard
- Demo repository with the reviewed pull requests: https://github.com/kazuma761/prguard-demo-target

## Credits

Review architecture adapted from Antern's "Designing an AI Pull-Request Review
Agent" (Ayush Singh). Threat model from Kudelski Security's research on
CodeRabbit. Access control by AgntID.

## Three-minute video script

1. (20 s) The CodeRabbit story: one PR, a million repositories. "The bot reads
   a stranger's text and holds your keys."
2. (30 s) Dashboard in GUARDED mode. Run a review. Point at the feed: every
   read is allowed by AgntID, under each agent's own identity. Open the review
   that was posted on GitHub.
3. (50 s) Assume-breach replay. Read the denials aloud: secrets, approve,
   merge, push, other repository, CI workflow. Show one policy in the AgntID
   portal (the Rego for file reads).
4. (40 s) The "before": the same replay with no AgntID. Show the merged PR and
   the pushed commit on GitHub. Then run the reset script.
5. (30 s) The three layers (sandbox, no keys in agents, per-action rules), then
   the honest limits and what would close them.
