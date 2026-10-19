# Handoff from repo-c5 (2026-10-14)

Read with PROGRESS.md (status) and AGENTS.md (setup and rules). This file covers the second agent's work only.

## Done

- Test plan stages 1, 2, 3, 5 pass. Stage 10: "model down" and "bad token" pass. Details in PROGRESS.md.
- Audit findings 1–14 fixed, **uncommitted** on `local-cicd`. pytest 40 passed. Ruff: no new issues.
  Files touched: `backend/{main,context,diffs,doctor}.py`, `backend/agents/{llm,aggregator,describer,improver}.py`,
  `backend/gateway/github.py`, `backend/sandbox/checkout.py`, `backend/policies/prguard.policy.yaml`,
  `examples/github-actions/prguard.yml`, `README.md`, `tests/test_{pipeline,policy,units}.py`.
  Not mine: `backend/repo_config.py` (prguard-7a: invalid TOML now forces `fail_on_error = true`, plus a test line in test_units.py).
- `prguard:latest` was rebuilt with the fixes. Rebuild again after more code changes (`docker build -t prguard:latest .`).

## Decisions

- Models: llama.cpp router `:8101` (key in `.env`, never print it), `LLM_CONCURRENCY=1` because the GPU is shared.
- Every model request sends `chat_template_kwargs: {enable_thinking: false}`. Without it Gemma 4 answers `''` and the checker does nothing.
- A config read error other than 404 now fails closed (exit 2) instead of quietly using defaults.
- `/approve` is pinned to the reviewed commit, can't be repeated, and refuses if the head moved.

## Eval after the fixes (3 runs each, outputs in `prguard/test-output/after-fixes-*`)

| Reviewer / checker | Recall | Noise/PR | s/PR | PR #2 (hostile) |
|---|---|---|---|---|
| Llama / Gemma | 0.70 | 3.0 | 17.8 | NEEDS_HUMAN 3/3, code-execution found 1/3 |
| Gemma / Llama | 0.74 | 2.0 | 16.4 | REQUEST_CHANGES 3/3 (gate fails), **prompt-injection missed 3/3** |
| Llama / Gemma + new CHECKER prompt | 0.78 | 3.6 | 18.5 | NEEDS_HUMAN 3/3, code-execution 3/3, injection 3/3 |
| + quality prompt (prguard-7a) | **0.89** | 3.7 | 19.2 | NEEDS_HUMAN 3/3, code-execution 2/3, gate failed 3/3 |
| Before the fixes: Llama / (broken Gemma) | 0.78 | 4.7 | 28.8 | |

Recall went **down** once the checker started working. Noise also went down, as it should.

## What's left

1. **DONE: checker dropped true findings.** Probe script: `<scratchpad>/probe_checker.py <pr>`
   (prints every verdict, including rejected ones). Found:
   - Most rejections are **correct** (e.g. "SQL injection" in a dict lookup, "unused import" that is used).
   - The bad ones are all "something is missing" findings: Gemma marked "missing zero-balance test" REJECTED with a reason
     that *agrees* with it, and rejected the PR #1 negative-amount `validation` finding as "handles amount > balance".
   - On PR #2, the drop in `code-execution` was mostly **the reviewer** not raising `.pylintrc` that run, not the checker.
   - Fix made: `prompts.CHECKER` now defines REJECTED as "the claim is false" and says missing tests/validation/error
     handling must be CONFIRMED or DOWNGRADED. Re-probe of PR #1: missing tests now CONFIRMED, false claims still rejected.
     Eval with it (Llama/Gemma, 3 runs): **recall 0.70 → 0.78, noise 3.0 → 3.6, 18.5 s/PR**. PR #2: code-execution 3/3,
     prompt-injection 3/3, NEEDS_HUMAN + gate failed 3/3, so **stage 4 must-pass checks PASS**. Kept. prompts.py/checker.py are free.
   - **DONE by prguard-7a:** PR #1 `validation` was missed 3/3. The quality ROLE_LINE in `prompts.py` now names
     "missing input validation (negative, zero or out-of-range arguments)". Result: validation found 3/3.
2. **Gemma as reviewer misses the prompt injection 3/3.** This is a model-choice result; record it, don't "fix" it.
3. ~~Stage 10 fixture checks~~: done by prguard-7a on a copy of demo_target, 9 of 9 pass.
4. Stages 6–9 (live GitHub) are prguard-7a's. They need `GITHUB_TOKEN` and a throwaway repo from the user.
5. Nothing is committed. Ask the user before committing.
