# PRGuard: working notes for coding agents

PRGuard is an AI pull-request reviewer (a CodeRabbit-style product with a CI
fixer) built to stay safe when a pull request turns hostile. It is our entry to
the AgntID Hackathon. Read `README.md` for how it works and how to run it.

## The five judging criteria (most important; every decision serves these)

From the organizers' kickoff talk. Before adding or changing anything, ask
which of these it improves.

| # | Criterion | What the judges want | How PRGuard answers it |
|---|---|---|---|
| 1 | Real-world agent use case | A practical enterprise application, not a toy | AI code review is a real product category. The threat is a real incident: in January 2025 one crafted PR gave attackers CodeRabbit's GitHub App key, with write access to about a million repositories. |
| 2 | Real-world AgntID relevance | A clear reason the guardrails are needed | A reviewer bot reads untrusted text (the PR) and holds write access to the repository. That is exactly "an agent with broad access deciding its own next step". The fix is just-for-task access: this review, this repo, this PR, this commit. |
| 3 | Depth of AgntID usage | Number and depth of controls used | See the control matrix below. Target: tool level, protection profile, parameter level, intent level, multi-agent delegation, human approval, reporting. |
| 4 | Before vs after | Off the rails without AgntID, contained with it | One toggle, same code. UNSAFE: PR code reads the reviewer's environment and the assume-breach replay merges, pushes and edits CI. GUARDED: canary stays at zero and the same eight calls are stopped, each with a reason. |
| 5 | Demo and communication | Clarity, presentation, recorded walkthrough | Mission Control UI shows every decision live; the demo is deterministic (cached model replies, fixed replay). Record a backup video. |

## AgntID control matrix (criterion 3)

Status key: LIVE = verified against the real runtime. READY = built and tested
against a fake, waiting for the registered runtime. TODO = not built.
Never mark something LIVE, or label a decision "AgntID", unless the real
runtime made that decision.

| AgntID control | PRGuard use | Status |
|---|---|---|
| Customer-hosted runtime as the only path to GitHub | Agents hold no GitHub token; every call goes through the runtime's MCP proxy to PRGuard's own 13-tool GitHub MCP server | READY (server verified against the real repo; not yet attached in the portal) |
| Tool level (visibility) | Disable merge, push, file write/delete, repo admin tools for the runtime so reviewers cannot even list them | TODO (portal). Seen live: an unreviewed tool is refused with code CLASSIFICATION_GATE, which PRGuard shows as an AgntID denial |
| Protection profile | Medium (read and normal write); destructive and high-impact actions excluded | TODO (portal) |
| Parameter-level policies | `owner`/`repo` pinned to the demo repo on every tool; review `event` limited to COMMENT and REQUEST_CHANGES; `path` may not match `.env`, `secrets/`, `*.pem`, `*.key`, `.github/workflows/` | TODO (portal) |
| Intent level | One task per review; the prompt names repo, PR and commit and states the prohibitions as plain sentences. Standard (NLI) first, LLM-based if a model credential is available | READY (prompt), TODO (portal mode) |
| Multi-agent delegation | Root task by `prguard-orchestrator`; one delegation per specialist (security, quality, tests, docs, ci-fixer, aggregator) with its own agent id, delegated intent and tool allowlist | Task open and delegation open accepted by the live runtime (2026-10-10); tool calls not yet, see below |
| Human approval | APPROVE only after a human approves in Mission Control; evidence is bound to the exact tool and arguments. AgntID "review required" outcomes are surfaced as waiting | READY |
| Reporting / audit | Every allowed and denied call appears in AgntID Reporting; show it next to our decision feed | TODO (after first live run) |
| Custom Rego | A rule the visual editor cannot express (for example APPROVE needs approval evidence) | TODO (need to see the portal's Rego input shape) |
| Scoped credentials | AgntID offers AWS STS today; GitHub App tokens are "coming soon". Possible add-on: archive each review to S3 with an STS-scoped credential | TODO (needs an AWS account) |
| Simulator / Inspector | Use Inspector to prove a denial without any model; Simulator needs a model credential in the portal | TODO |

Layers that are PRGuard's own, not AgntID's (say so in the demo): the Docker
sandbox for PR code, the prompt-injection handling in the reviewers, the
independent checker, and the optional Jev triage.

## Live AgntID status (verified 2026-10-10 against the real runtime)

- Our MCP server is attached as Remote HTTP, Server ID and prefix `prguard`
  (NOT `github`: that name makes AgntID demand a GITHUB_APP credential and
  every call fails at credential_resolution). Tools appear as `prguard_*`.
- All 13 tool policies are set. Eight are repo-scoped with structured rules
  (owner = kazuma761, repo = prguard-demo-target). Three use custom Rego:
  `get_file_contents` (repo scope + no secret paths),
  `create_pull_request_review` (repo scope + event in COMMENT/REQUEST_CHANGES),
  and always-deny for merge, push, file write and file delete.
- Proven live: read PR allowed; cross-repo read, `.env` read, APPROVE and merge
  each denied by AgntID with our own readable reason.
- How policies are saved: PUT /api/v1/policies/{id} with either
  `l2pbac_policy.rules` (types: exact, prefix, suffix, contains, length) or a
  `rego_policy` string in AgntID's convention (rule_r_NNN helpers,
  `failed_rules`, `rule_meta` with a `label`, and a `decision` object). The
  label becomes the denial reason. Typing into the portal form from browser
  automation loses characters; do not rely on it.
- Known gaps: `intent_parsing` shows "skipped" on every call so far;
  APPROVE is denied for agents even after human approval (no approval input
  found in Rego yet); `push_files`, `delete_file` and `rerun_failed_jobs`
  show "not active" (hidden by the Medium profile).

## AgntID runtime: handle with care (limited trial quota)

The AgntID org is on a trial plan: **5 runtimes** (all used) and **1,000 tool
calls per day**. The runtime on this machine is registered to a second account
and is working. Losing its registration could leave us with no runtime.

- **Never re-register, reset or wipe the runtime.** Do not run
  `docker compose down -v`, do not delete the `agntid-runtime_*` volumes, and
  do not ask for or use another bootstrap token.
- **Do not restart or recreate the runtime containers unless you are sure it
  is needed and have said so first.** `./scripts/agntid_runtime.sh up` recreates
  the container whenever its settings change (for example after editing
  `agntid/.env`). Registration lives in the volumes and should survive, but
  treat it as a deliberate, announced step, not a casual one.
- **Spend tool calls deliberately.** Every call through the proxy counts,
  including denied ones and each run of `agntid_smoke.py` (about 3 calls),
  each review (about 10) and each replay (8). Develop and test against the
  local stand-in and the fake proxy in `tests/`; use the real runtime only to
  verify something specific, and for the demo and its rehearsal.
- Health checks that do not count: `docker ps` and
  `curl http://localhost:18080/health`.

## Going live with AgntID: who does what

Secrets are entered by the human only. Agents never type a bootstrap token,
GitHub token or model key into any screen, command or file.

1. Runtime: `./scripts/agntid_runtime.sh up`, wait until healthy. DONE 2026-10-10 (UI on port 18080 because 8080 is taken on this machine; MCP proxy on 8082).
   `scripts/agntid_smoke.py` checks the live contract but uses about 3 tool calls; run it sparingly.
2. Human: open http://localhost:18080, paste the bootstrap token, Connect.
3. Human: paste the fine-grained GitHub token into `.env` after
   `GITHUB_PAT_UNSAFE=`. It is read only by PRGuard's own GitHub MCP server
   (`prguard_mcp/server.py`, port 8090), never by the agents, the backend's
   gateways or the AgntID runtime. No runtime restart is needed.
4. Portal, runtime Setup tab: enable MCP Support, review the Medium profile,
   wait for 5 of 5 and "Runtime sync: Applied".
5. Portal, Operations > MCP > Servers > Add MCP: Remote HTTP,
   `http://host.docker.internal:8090/mcp`, Server ID and prefix `github`,
   credentials `none`. Test, save, wait for Applied. (Verified 2026-10-10: a
   container on this machine reaches that address.) The 13 tools are:
   get_pull_request, get_pull_request_files, get_pull_request_diff,
   get_file_contents, search_code, list_workflow_runs, get_job_logs,
   create_pull_request_review, rerun_failed_jobs, merge_pull_request,
   create_or_update_file, push_files, delete_file.
6. Portal, Access Control > Tools: review classifications; disable the tools
   PRGuard never needs. Access Policies: add the parameter limits above.
7. `.venv/bin/python scripts/agntid_check.py` until it prints READY, then set
   `ENFORCER=AGNTID` in `.env` and restart the backend.
8. Run the review and the replay in GUARDED mode; confirm the events in
   AgntID Reporting.

GitHub accounts: `kazuma761` is the main account and owns the demo repo.
`Shrirajas` is the alternate account that opens the pull requests, so the
reviewer is never reviewing its own PR.

## Rules for agents working here

- The attack is a harmless simulation. The hostile PR only reports environment
  variable names and a planted fake marker to localhost. Never add anything
  that reads or sends real values, and never give PR code a real key.
- Be honest in code and UI. Stand-ins and mocks are labelled as such
  (`LOCAL POLICY (stand-in)`, `MOCK upstream`, `MOCK LLM`).
- A deterministic demo beats a clever one. Keep the LLM cache and the fixed
  replay working.
- Use the cheapest workable models; model names live in `.env`, never in code.
- Tests must stay offline (`tests/conftest.py` forces mocks). Run
  `.venv/bin/python -m pytest -q` before saying something works.
- Do not build: vector search, queues, user accounts, a GitHub App install
  flow, multi-repo support, cost dashboards.
- `agntid-docs/` is AgntID's login-gated documentation. Read it, do not commit
  or publish it.
