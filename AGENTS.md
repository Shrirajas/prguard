# AGENTS.md

Notes for AI agents working in this repo. Current status and next steps are in [PROGRESS.md](PROGRESS.md).

## What this is

PRGuard is a self-hosted AI pull-request reviewer for CI/CD. It talks to any OpenAI-compatible
model server and to GitHub's REST API. CLI entry point: `python -m backend.cli`, with the commands
`review | describe | improve | ask | run | serve | doctor | eval`.

Review pipeline (`backend/orchestrator/graph.py`):
1. Build the context: PR, numbered diff fitted to the model's context, and optional check output.
2. Specialists (`backend/agents/specialists.py`): security, quality, tests and docs, plus a CI fixer. They run on the **reviewer model**.
3. The aggregator merges duplicate findings and scores them (plain code).
4. The checker (`backend/agents/checker.py`) confirms, downgrades or rejects each finding. It runs on a **second model**, ideally from another family.
5. The gate (plain code) fails the job on HIGH+ findings with confidence ≥ `min_confidence`.
6. Publish. Every GitHub call goes through the policy gate (`backend/gateway/policy_gateway.py`, rules in `backend/policies/prguard.policy.yaml`), which denies by default.

Exit codes: 0 means passed, 1 means the gate failed, 2 means a usage or setup error.

## Layout

- `backend/agents/`: LLM agents. Prompts are in `prompts.py` and the client is in `llm.py`.
- `backend/gateway/`: GitHub client, mock upstream and policy engine.
- `backend/sandbox/`: locked container for running the PR's linters and tests.
- `demo_target/prs/`: 3 labelled demo PRs (01_clean, 02_hostile, 03_ci_failure), used by `eval` and `--mock`.
- `tests/`: offline tests that use a mock GitHub and a mock model.
- `frontend/`: dashboard for `serve` mode.

## Local setup on this machine

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                       # 40 tests, offline
.venv/bin/python -m backend.cli review --mock --pr 2 --no-publish --out <dir>
```

- **Always pass `--out <writable dir>`.** The default is `/out`, which is only writable inside Docker. Without it you get a PermissionError.
- Model servers on this host:
  - **Ollama** `http://127.0.0.1:11434/v1`: no auth. Serves `llama3.1:8b` and `gemma4:12b`, among others.
  - **llama.cpp router** (docker `llama-server-webui`) `http://127.0.0.1:8101/v1`: **needs an API key** and returns 401 without one. Its aliases are `llama3.1:8B` (ctx 24576, parallel 8) and `gemma4:12B` (ctx 16384, parallel 4), both with `--kv-unified`. **This is the server the user wants used.** The key is already in `repo/.env` as `LLM_API_KEY` and `CHECKER_API_KEY`. Never print it or copy it elsewhere.
  - **Gemma 4 is a thinking model.** Its thinking goes to `reasoning_content` and uses up `max_tokens`. Tiny budgets (doctor uses 20) give an empty `content`.
    `backend/agents/llm.py` now sends `"chat_template_kwargs": {"enable_thinking": false}` on every request. Without it the
    checker returned no verdicts and every finding stayed UNVERIFIED. Don't remove it.
- Test-plan outputs (reports, eval JSON, event logs) are saved in `prguard/test-output/`, outside the repo.
- **Several agents work in this repo** (sessions `repo-c5`, `prguard-7a`, `prguard-c9`; see `/list-agents`). Before editing, read
  "Work split" in PROGRESS.md and HANDOFF.md, and don't touch files another agent has claimed. Coordinate by cross-session message.
- **Keep PROGRESS.md updated** with what you are doing and what you've done, as you go. The user asked for this.
- The GPU is shared with other projects, so keep `LLM_CONCURRENCY` at 1–2.

Env for a real-model run (reviewer is Llama, checker is Gemma):

```bash
# llama.cpp :8101 (LLM_API_KEY comes from .env; the checker uses the same server and key)
export LLM_BASE_URL=http://127.0.0.1:8101/v1 LLM_MODEL=llama3.1:8B \
       CHECKER_MODEL=gemma4:12B LLM_CONTEXT_TOKENS=8192 LLM_CONCURRENCY=1
.venv/bin/python -m backend.cli doctor
.venv/bin/python -m backend.cli eval --runs 1     # offline: scores the demo PRs, posts nothing
```

## Current state (2026-10-14)

Details are in PROGRESS.md and HANDOFF.md. **Nothing is committed.** All fixes are uncommitted edits on `local-cicd`. Ask the user before committing.

- **Test plan:** stages 1–5 pass. Stage 9 server mode and stage 10 pass **offline**. Stages 6–9 against **live GitHub** are not done:
  they need a `GITHUB_TOKEN` and a throwaway repo from the user, who said to leave this for now. `gh` is not installed.
- **Audit findings 1–15 fixed** (see the list in PROGRESS.md). Behaviour you should know about:
  - `/approve` is pinned to the reviewed commit (sends `commit_id`). It returns 409 if the head moved or it was already approved.
  - A `.prguard.toml` read error other than a 404 exits 2. Invalid TOML falls back to defaults **with `fail_on_error = true`** (fail closed).
  - `/webhook` returns 502 and forgets the delivery id when the GitHub lookup fails, so the delivery can be redelivered. GitHub POSTs are not retried after a 5xx or a timeout.
  - `doctor` now shows ✗ when a model's reply is empty.
- **Prompts** (`backend/agents/prompts.py`): REJECTED in CHECKER means "the claim is false". Missing tests or validation must be
  CONFIRMED or DOWNGRADED, not rejected. The quality reviewer is told to look for missing input validation (negative, zero or out-of-range arguments).
- **Eval, Llama reviewer / Gemma checker, 3 runs:** recall **0.89**, noise 3.7 findings/PR, 19.2 s/PR, and the PR #2 gate failed 3/3
  (`test-output/after-quality-prompt-llama-reviews-gemma-checks`). It was 0.78 before the prompt fixes.
  The swapped pairing (Gemma reviewer) was last measured before the validation-prompt change: recall 0.74, and it **missed the prompt
  injection 3/3**. The demo comment labels itself as fake, so models rate it INFO. That's a model-choice result, not a bug.
- **Docker:** `prguard:latest` and `prguard-sandbox` are built with all of the above. Rebuild after code changes:
  `docker build -t prguard:latest .`. A mock smoke test is `review --mock --pr 1` (exit 0) and `--pr 2` (exit 1).
- **Reusable offline checks** (no GPU, no GitHub), in `prguard/test-output/scripts/`:
  - `server_test.py <base_url>`: 32 checks covering auth, webhook HMAC and replay, event filtering, jobs and approve. Run it against
    `UPSTREAM=mock LLM_PROVIDER=mock PRGUARD_API_TOKEN=t0k GITHUB_WEBHOOK_SECRET=s3cret PRGUARD_SANDBOX=0 PRGUARD_ALLOWED_REPOS=demo/prguard-demo-target .venv/bin/python -m backend.cli serve --port <p>`.
  - `stage10.sh`: 9 config and failure-mode checks (model down, fail_on_error, bad TOML, fail_on values, all 3 demo PRs).
    It runs on a *copy* of `demo_target`, so the shared fixtures are never edited.
- **Next, if asked:** live GitHub stages 6–9 (token plus throwaway repo); re-run the swapped-roles eval with the current prompts;
  add real past PRs to `demo_target/prs/` so prompt tuning isn't fitted to 3 demo PRs.

## Rules

- Get the user's OK before running long model jobs (`eval`, real reviews). They use the shared GPU.
- Secrets go only in `.env`, which is gitignored. Never commit them or copy them into other files.
- Keep verdicts and the gate in plain code. Don't let model output decide pass or fail directly.
- Don't edit the shared `demo_target/` fixtures for a one-off test. Copy them and point `settings.demo_target_dir` at the copy, as `stage10.sh` does.
