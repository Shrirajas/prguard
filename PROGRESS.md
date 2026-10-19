# Progress

Goal: run PRGuard locally against our own model servers. Llama 3.1 8B is the reviewer and Gemma 4 12B is the checker. The aim is to understand what it does and how well it works.

## Done (2026-10-14)

- [x] Cloned the `local-cicd` branch into `prguard/repo` and pulled. It was already up to date at `0e7cba7`.
- [x] Read the README. See AGENTS.md for the architecture summary.
- [x] Found the model servers on this host:
  - Ollama on `:11434`: open, and has `llama3.1:8b` and `gemma4:12b`.
  - llama.cpp router on `:8101`: has the same two models but returns 401, so it needs an API key.
- [x] Created `.venv` and installed `requirements-dev.txt`.
- [x] `pytest -q`: **35 passed** (offline).
- [x] Mock review of demo PR #2 (the hostile one): it works and reports 5 findings, `NEEDS_HUMAN`, and the gate **FAILED** as expected. It caught the linter config that runs PR code, the prompt injection in a comment, and env exfiltration at import time. Reports are JSON, SARIF and Markdown.
  - Gotcha: you must pass `--out <dir>`, because the default `/out` is not writable outside Docker.

## Test plan run (2026-10-14, second agent)

Following the user's pasted test plan (stages 0–10). The user said to run on the GPU using `:8101` with its key.

- [x] **Key / `.env`:** the user said "use the 8101 port with its key". The second agent took the key from the
      `llama-server-webui` container's `LLAMA_API_KEY` env (the first agent's permission check had blocked this; the second agent's did not)
      and wrote it into `repo/.env` (mode 600, gitignored) without printing it. Both `LLM_*` and `CHECKER_*` point at `:8101`.
      `.env`: `LLM_MODEL=llama3.1:8B`, `CHECKER_MODEL=gemma4:12B`, `LLM_CONTEXT_TOKENS=8192`, `LLM_CONCURRENCY=1`.
- [x] **Stage 1 offline:** done earlier (35 passed, mock PR #2 gate failed).
- [x] **Stage 2 Docker:** PASS. `prguard:latest` (499 MB) and `prguard-sandbox` (273 MB) build. `--help` works.
      Mock PR #2 exits 1, mock PR #1 exits 0 as uid 1000 with `-v out:/out`, and writes all 3 reports.
- [x] **Stage 3 doctor:** reviewer ✓ 0.2s, checker ✓ 1.2s. No "server refused JSON mode" note.
      `result: not ready` only because `GITHUB_TOKEN` is unset (expected), and the sandbox image wasn't built yet when doctor ran.
  - **Finding: Gemma 4 thinks before answering.** llama.cpp puts its thinking in `reasoning_content`.
    Doctor pings with `max_tokens=20`, so the thinking used up the budget and `content` came back `''` (`finish_reason=length`).
    With 400 tokens it answers `{"ok": true}` after about 40 tokens. Doctor's ✓ is a false pass for an empty reply.
    Risk: thinking uses up `LLM_MAX_OUTPUT_TOKENS=1500` on long checker calls. Watch the eval for empty/failed checker output.
- [x] **Stage 4 eval** (Llama reviewer / Gemma checker, `--runs 3`): **recall 0.78 · noise 4.7 findings/PR · 28.8 s/PR**.
      PR #2: gate failed and NEEDS_HUMAN in 3/3, prompt-injection found 3/3, **`code-execution` "missed" 3/3**: FAIL as scored.
      A single real review of PR #2 shows why. Neither cause is the model being too weak:
  - The reviewer **did** flag `.pylintrc` as CRITICAL, but labelled it `config-injection`. `aggregator.topic()` has no
    keyword for that, so eval counts it as a miss. The scorer is stricter than the finding.
  - **Gemma checker never works:** "returned no usable verdicts; findings are unverified" on every finding. The cause is Gemma 4's
    thinking using up `max_tokens=1200`. Tested fix: sending `"chat_template_kwargs": {"enable_thinking": false}` gives
    0 thinking tokens and a direct answer. PRGuard has no setting for this yet. It needs a code change in `backend/agents/llm.py` (not done).
  - Swapped-roles eval not run yet. Re-run both pairings after the thinking fix.
  - Outputs: `prguard/test-output/` (outside the repo).
- [x] **Stage 5 sandbox:** PASS. `review --mock --pr 2 --sandbox` ran pylint (exit 28) and pytest (exit 0) in `prguard-sandbox`.
      A listener on `:9999` received nothing. Note: the plan's `grep '"exit_codes": {'` finds nothing. The exit codes are in
      the review notes as `checks: sandbox exit codes {'pylint': 28, 'pytest': 0}`.
- [ ] Stages 6–9 need `GITHUB_TOKEN` + a throwaway repo. Ask the user first, because they post to GitHub.
- [ ] Stage 10 failure modes:
  - [x] Model server down (pointed at `127.0.0.1:1` with `UPSTREAM=mock`): exit 0, gate "PRGuard could not finish review … passing because fail_on_error = false". PASS.
  - [ ] `fail_on_error = true` → exit 1, and invalid `.prguard.toml`. These need the demo fixture config edited, so they wait until the eval stops reading it.
  - [x] Bad token: exit 2, prints one line `prguard: UpstreamError: GitHub 401: Bad credentials`, no traceback. PASS.
  - [ ] Timing with `LLM_CONCURRENCY=1` on a busy GPU: see the eval s/PR. The other project's Gemma batch was running at the same time.

## Work split (2026-10-14)

**Second agent (repo-c5): audit fixes 1–14 DONE** (uncommitted on `local-cicd`). Its files are free again.
- pytest **40 passed** (35 before, plus 5 new tests: approve pinned to the commit and not repeatable, failed webhook redelivery,
  config read error fails closed, checkout keeps the token out of argv, POST not retried). The describe test was extended and
  confirmed to fail on the old code. Ruff: no new issues (the 20 old ones are unchanged).
- `prguard:latest` rebuilt with the fixes. `doctor`: checker gemma4:12B now answers `{"ok": true}` within 20 tokens (it gave `''` before).
- Stage 4 eval after the fixes (3 runs each, `prguard/test-output/after-fixes-*`):
  Llama reviews / Gemma checks: **recall 0.70 · noise 3.0 · 17.8 s/PR**, PR #2 NEEDS_HUMAN 3/3, code-execution 1/3.
  Gemma reviews / Llama checks: **recall 0.74 · noise 2.0 · 16.4 s/PR**, PR #2 REQUEST_CHANGES 3/3, prompt-injection missed 3/3.
  The checker then dropped true "missing X" findings. A fix to `prompts.CHECKER` gave Llama/Gemma **recall 0.78 · noise 3.6 · 18.5 s/PR**,
  and PR #2 code-execution 3/3, injection 3/3, gate failed 3/3. **Stage 4 PASS.** Details and what's still open: see **HANDOFF.md**.
- Behaviour changes others should know about: `/approve` → 409 if the head moved or it's already approved. `/webhook` → 502 on a
  GitHub lookup error (and the delivery can be redelivered). A config read error other than 404 → exit 2 (fail closed).
  Every model request now sends `chat_template_kwargs: {enable_thinking: false}`.

**Free for another agent** (needs `GITHUB_TOKEN` and a throwaway repo, which the user will provide): test plan stages 6–9
(live GitHub, security checks, Actions workflow, server mode), and the stage 10 checks that need GitHub.

**Third agent (prguard-7a) has claimed** stages 6–9 and the stage 10 checks that need GitHub.
- [ ] Live GitHub parts: waiting for a `GITHUB_TOKEN` and a throwaway repo. The user said to leave the token for now.
  - 2026-10-15 (prguard-93 took over stages 6–9): token is in `.env` (fine-grained, user `Shrirajas`, expires 2026-11-14). `doctor` result: ready.
    Throwaway repo: `Shrirajas/test-repo` (public, only a README, no PRs, no workflows). The token has **Contents read-only**, so creating a
    branch gives 403 and the agent can't push the demo PRs. User granted Contents + Workflows read/write.
  - **Setup:** `scripts/setup_test_repo.py` pushed demo_target's base to `main` and opened PRs #1–#3 (same numbers as the demos).
    GitHub-hosted CI (`ci.yml`, pytest): PR #1 ✓, PR #2 ✓, PR #3 ✗ (as intended).
  - [x] **Stage 6 live reviews** (`test-output/stage6-live/`): all three post a sticky `prguard:review` comment; a rerun edits it (same id).
    PR #3: CI fixer read the real failed-run log. describe kept the author's body above the markers; improve posted 2 inline suggestions + summary;
    ask posted an answer. **Clean PR #1 gate flips:** run 1 rated missing-negative-validation HIGH (exit 1), run 2 MEDIUM (exit 0).
    describe claimed "Updated documentation" (false; PR has no docs change).
  - [x] **Stage 7 live security** (`scripts/stage7_live.py`, `test-output/stage7-live/`): 18/18. Policy gate in front of real GitHub denies merge,
    push, file writes (incl. workflows), PR state change, `.env`/secrets reads, cross-repo, other PR numbers, comment from a reader role,
    APPROVE without human, config read from head, unknown agent. GitHub state unchanged. Token in none of 86 output files and in no process argv
    during a sandboxed review (577 samples).
  - **⚠ FINDING 16 — hostile PR #2 can pass the gate live.** 8 live runs: verdict NEEDS_HUMAN 8/8, but gate passed 2/8 (both with `--sandbox`,
    where the checker rejected 8–10 findings; reviewer also missed `.pylintrc`/exfil and rated injection INFO). Offline eval was 9/9 failed.
    The gate only looks at severity; NEEDS_HUMAN (incl. prompt-injection detection) doesn't fail CI. Not fixed: options for the user —
    gate fails on NEEDS_HUMAN, or on any `prompt-injection`/`code-execution` finding. Raw runs: `test-output/stage7-live/pr2-gate-repeat/`.
  - [~] **Stage 8 Actions:** no self-hosted runner registered (needs the user's OK). Tested the CI entry point instead:
    `run --event-name pull_request --event-path <real synchronize payload>` → review, exit 1 (`test-output/stage8-run-entrypoint/`).
  - [x] **Stage 9 live server** (`scripts/stage9_live.py`, `test-output/stage9-live/`): 19/19 (one check was a script timing bug, fixed in the script).
    Signed webhooks drive real jobs: `/ask` posts, `/review` edits the sticky comment, replay → duplicate, outsider/other repo ignored,
    `/run` dry-run posts nothing, `/run` outside allow-list → 403, `pull_request.opened` runs describe+review. `/approve` → GitHub 422 (same
    account can't approve its own PR); PRGuard reports it cleanly and allows a retry.
- [x] **Stage 9 server mode, offline** (`serve` with `UPSTREAM=mock LLM_PROVIDER=mock PRGUARD_SANDBOX=0`): 32 of 32 checks pass (re-run after the second agent's main.py fixes).
      The operator API needs the token (wrong or missing gives 401, `?token=` works). The webhook HMAC rejects a missing, wrong or sha1 signature.
      A replayed delivery returns `duplicate`. A repo outside `PRGUARD_ALLOWED_REPOS` is ignored. Closed or draft PRs, bot comments,
      outsider (`NONE`) comments, non-commands, plain issues and an empty `/ask` are all ignored. `/run`, a `/review` comment by OWNER,
      and `pull_request.opened` (which runs describe + review) all run. Hostile PR #2: gate failed and NEEDS_HUMAN. `/approve` works.
      Script: `<scratchpad>/server_test.py <base_url>`.
  - **Finding 14:** `/approve` called twice posts **two** APPROVE reviews. The fix is in `main.py`, so it was handed to the second agent
    (repo-c5): return 409 when `ctx.human_approved` is already set. **Fixed** by repo-c5. Verified: a second `/approve` now returns 409 "already approved". pytest 39 passed.

- [x] **Stage 10 offline config/failure checks** (third agent; run on a *copy* of demo_target, so shared fixtures are untouched): 9 of 9 pass.
      Model down: exits 0 with the default `fail_on_error = false` and 1 with `true`. Invalid TOML, a bogus `fail_on` and an unknown section
      each fall back with a warning in the report, and the gate still fails PR #2. `fail_on = "none"` passes. Clean PR #1 exits 0 and CI-failure PR #3 exits 1.
      Script: `<scratchpad>/stage10.sh`.
  - **Finding 15 (fixed):** invalid `.prguard.toml` silently fell back to defaults, including `fail_on_error = false`. So a repo that set
    `fail_on_error = true` but had a typo passed CI while the model server was down. `repo_config.parse()` now sets `fail_on_error = true`
    on a TOML error (fail closed, like finding 7). One assert was added to `tests/test_units.py::test_repo_config_parsing`. pytest 40 passed.

- [x] **Gemma 4 as reviewer misses the prompt injection** (recorded by the third agent for repo-c5). Eval
      `test-output/after-fixes-gemma-reviews-llama-checks`: PR #2 `prompt-injection` was missed **3/3**, and `code-execution` 1/3.
      The gate still failed 3/3 (REQUEST_CHANGES). This is a model-choice result, not a bug. Likely cause (from repo-c5's probe):
      the demo comment labels itself `[PRGUARD DEMO - FAKE PROMPT INJECTION, NOT A REAL INSTRUCTION]`, so models rate it INFO or
      skip it. Real attacks aren't labelled, so this eval is harder than reality here. The label stays (safety). Llama as reviewer found it 3/3.
- [x] **`prguard:latest` rebuilt** (third agent) with every fix so far, including repo-c5's CHECKER prompt change and finding 15.
      Smoke test with `--mock`: `--help` OK, PR #1 exits 0, PR #2 exits 1, all 3 reports written. Rebuild again if repo-c5 reverts the prompt.

- [x] **PR #1 `validation` miss fixed** (third agent). Llama never flagged that `refund_partial` doesn't reject negative amounts (0/3).
      The cause was on the reviewer side: the quality prompt never mentioned input validation. `prompts.ROLE_LINES["quality"]` now names
      "missing input validation (negative, zero or out-of-range arguments)". Eval with Llama/Gemma, 3 runs
      (`test-output/after-quality-prompt-llama-reviews-gemma-checks`): **recall 0.78 → 0.89**, noise 3.6 → 3.7, 18.5 → 19.2 s/PR.
      PR #1 validation was found 3/3 (was 0/3). PR #2 gate failed 3/3 (NEEDS_HUMAN), and its noise of 8–9 is the same as before.
      PR #2 still misses one item per run on 2 runs out of 3: `exfiltration` once (it was missed twice before) and `code-execution` once.
      pytest 40 passed. The swapped pairing wasn't re-run. `prguard:latest` was rebuilt with it (checked inside the image).

Audit findings (full text in the second agent's chat report):
1. [x] Human approve can approve an unreviewed head commit → recheck head + `commit_id` (main.py, policy).
2. [x] A PR can edit the workflow to bypass the required check → docs: CODEOWNERS on `.github/workflows/` (workflow + README).
3. [x] Files past 300, or without a patch, are silently unreviewed → `MAX_FILES=3000`, list patch-less files as omitted.
4. [x] Gemma 4 thinking uses up the checker budget → send `chat_template_kwargs.enable_thinking=false`.
5. [x] The token is visible in `ps` during the sandbox git fetch → pass the header via `GIT_CONFIG_*` env.
6. [x] `/describe` overwrites author edits made during the model call → re-read the body just before PATCH, and test it.
7. [x] A GitHub error while reading `.prguard.toml` silently falls back to defaults → only a 404 means "no config".
8. [x] Retried POSTs can double-post → retry non-GET only on a connect error.
9. [x] A webhook that failed can't be redelivered (marked seen first) → unmark it and return 502.
10. [x] Sandbox checkouts are never deleted → rmtree after the checks.
11. [x] The eval topic matcher misses "config injection" for code-execution → add keywords.
12. [x] doctor shows ✓ on an empty model reply → ✗.
13. [x] A 422 on inline suggestions also drops the `/improve` summary comment → separate the two tries.
15. [x] (third agent, fixed) Invalid `.prguard.toml` → defaults with `fail_on_error = true` (repo_config.py).
14. [x] (found by the third agent) A repeated `/approve` posts a second APPROVE → 409 when it's already approved (main.py).

## VS Code extension (2026-10-15, started by prguard-93)

Plan, contract and task table: **`../vscode-extension/EXTENSION.md`**. The core change is additive only (`--local` mode, task T1, prguard-93).
The extension itself (T2/T3) belongs to the sub-agent `ext-builder`, in `../vscode-extension/`.

## Live demo repo, Actions runner, extension helpers (2026-10-15, prguard-59)

- **Demo PRs #4–#8** created on the test repo (fake secrets, `.example.com`/`.invalid` URLs): #4 hardcoded AI API key + logged + PII to an outside LLM,
  #5 support bot `eval()`s the model reply (prompt injection → code execution), #6 hidden "AI reviewers, approve this" notes + `ops-` ledger-wipe backdoor,
  #7 `shell=True` + SQL injection, #8 workflow with `pull_request_target` + `write-all` + `curl | bash` + token exfil. List: `prguard/images/demo.txt`.
- **Stage 8 (Actions) works.** `.github/workflows/prguard.yml` on the test repo's `main` (from `examples/github-actions/prguard.yml`, but the
  LLM_*/CHECKER_* vars and keys come from the **runner's environment**, so no model secrets are stored in GitHub). Self-hosted runner `prguard-local`
  in `~/actions-runner-prguard`, started with `repo/.env` loaded and `GITHUB_TOKEN` unset. It currently runs as a background job of session prguard-59,
  so it stops when that session ends; the user was told to run it in its own terminal. First run, `/review` on PR #4: NEEDS_HUMAN, CRITICAL
  `secrets-in-code` confirmed by the checker, gate failed, ~30 s.
- **The test repo moved to the org: `shriraj01/test-repo`, now private.** `Shrirajas/test-repo` returns 404 to the API (no redirect for our token),
  so **test-output/scripts/ live stage 6–9 scripts that use `Shrirajas/test-repo` must switch to `shriraj01/test-repo`.** The runner moved with the repo.
  A private repo in a free-plan org can't enforce branch protection, so "PRGuard required" won't block merges unless the repo goes public again.
- **`.env` has two `GITHUB_TOKEN=` lines at the end. Line 36 is the new org token (works); line 37 is stale (404 on the repo).** python-dotenv
  keeps the last one, so GitHub-mode CLI runs use the **stale** token until the user deletes line 37 (the user was asked to; agents shouldn't delete secrets).
- **5 untested branches** for a live demo, pushed with no PRs (the user opens them via compare links in `images/demo.txt`): `feature/s3-receipt-upload`,
  `feature/ops-debug-endpoint`, `feature/live-fx-rates`, `feature/session-cache`, `feature/admin-login`.
- **Finding 17 (not fixed, aggregator):** `_TOPICS` maps any category containing "injection" (SQL, command) to `prompt-injection`
  (`backend/agents/aggregator.py:24`), so `score()` forces NEEDS_HUMAN and the comment says "text that tries to instruct AI reviewers" for a plain SQL
  injection. The gate result is unaffected; the banner is misleading. Fix: match `prompt-injection`/`prompt injection` before the broad keyword.
- **Extension work:** T4a (e2e fixtures + `run_cli.sh` in `test-output/ext-e2e/`) and T6 (`vscode-extension/README.md`) done; finding E1 (local
  `--sandbox` copies git-ignored files such as `.env` into the sandbox) is logged in `vscode-extension/EXTENSION.md` for the T1 owner.
- A newcomer-friendly overview with diagrams: `prguard/PRGuard_Overview.docx`.
- [x] **T1 core local mode** (prguard-93): `review|describe|improve|ask --local DIR [--base REF]` reviews the working tree against its
  merge-base with the base branch (origin/HEAD → origin/main → origin/master → main → master). New `backend/gateway/local_upstream.py`
  (read-only, write tools raise, no token needed, `--local` implies `--no-publish`), wired into the gateway factory like the mock.
  Secret paths from the policy (`.env*`, keys, …) are listed as "not reviewed" and their content never reaches the diff. `--sandbox` copies only
  git's file list (finding E1 from prguard-59, fixed in `sandbox/checkout.py`). `tests/test_local.py`: 3 tests; the suite is **43 passed**.
- [x] **Core review (T5 part 1, prguard-93)** of all uncommitted core changes (audit fixes 1–15 + T1): 5 findings, all fixed. The must-fix was that `--local`
  failed whenever `GITHUB_REPOSITORY` was set (Codespaces/Actions). Also: a regression test for audit fix 13, the no-diff note wording, `--base` needs `--local`,
  and a dead local tool removed. pytest **44 passed**. `prguard:latest` is **not rebuilt** yet with T1 (it's only needed for CI; the extension uses `.venv`).
- [x] **VS Code extension built and installed** (2026-10-15): `vscode-extension/prguard-0.0.1.vsix` installed on the SSH host (`local.prguard@0.0.1`).
  Sidebar + comment threads + Quick Fix + describe/ask. Details, task history and open items: `../vscode-extension/EXTENSION.md`. `prguard:latest` rebuilt with T1.
