#!/usr/bin/env bash
# Puts the demo back to a clean state after an UNSAFE run.
#
#   ./scripts/reset_demo.sh           clear the dashboard and canary history,
#                                     and restore `main` on the demo repo
#   ./scripts/reset_demo.sh victim    also open a fresh sacrificial PR for the
#                                     UNSAFE replay to attack, and print its number
#
#   ./scripts/reset_demo.sh baseline  record the current `main` as the clean state
#
# Restoring `main` force-pushes it back to the recorded baseline, which undoes
# a merge, a pushed file and an edited CI workflow made by an UNSAFE run.
# It only ever touches the throwaway demo repo named in .env.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(grep -E '^TARGET_REPO=' .env | cut -d= -f2)
case "$REPO" in */prguard-demo-target) ;; *) echo "refusing: TARGET_REPO is '$REPO', not a prguard-demo-target repo"; exit 1 ;; esac

curl -s -X POST http://127.0.0.1:8000/reset >/dev/null && echo "dashboard feed cleared" || echo "backend not running (skipped)"
curl -s -X DELETE http://127.0.0.1:9999/canary >/dev/null && echo "canary history cleared" || echo "canary listener not running (skipped)"

# The clean state is whatever `main` was when you last ran `baseline`, so
# merges you make on purpose are kept. Record it with: ./scripts/reset_demo.sh baseline
if [ "${1:-}" = "baseline" ]; then
  gh api "repos/$REPO/git/ref/heads/main" --jq .object.sha > .demo_baseline
  echo "baseline recorded: $(cat .demo_baseline)"; exit 0
fi
[ -s .demo_baseline ] || { echo "no baseline recorded; run: ./scripts/reset_demo.sh baseline"; exit 1; }
BASE=$(cat .demo_baseline)
HEAD=$(gh api "repos/$REPO/git/ref/heads/main" --jq .object.sha)
if [ "$HEAD" = "$BASE" ]; then
  echo "main is already clean ($BASE)"
else
  gh api -X PATCH "repos/$REPO/git/refs/heads/main" -f sha="$BASE" -F force=true --jq '"main restored to " + .object.sha'
fi

if [ "${1:-}" = "victim" ]; then
  # Build the sacrificial branch from a NEW commit (same files as the hostile
  # PR, on top of main). It must not share a commit with the real hostile PR:
  # merging a shared commit makes GitHub mark that PR as merged too.
  SRC=${HOSTILE_BRANCH:-chore/shared-lint-config}
  TREE=$(gh api "repos/$REPO/git/commits/$(gh api "repos/$REPO/git/ref/heads/$SRC" --jq .object.sha)" --jq .tree.sha)
  MAIN=$(gh api "repos/$REPO/git/ref/heads/main" --jq .object.sha)
  BRANCH="demo/victim-$(date +%H%M%S)"
  COMMIT=$(gh api -X POST "repos/$REPO/git/commits" -f message="Sacrificial copy $BRANCH" -f tree="$TREE" -f "parents[]=$MAIN" --jq .sha)
  gh api -X POST "repos/$REPO/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$COMMIT" >/dev/null
  URL=$(gh pr create --repo "$REPO" --head "$BRANCH" --base main \
        --title "Sacrificial copy of the hostile PR (for the UNSAFE replay)" \
        --body "A disposable copy of the hostile PR. The unguarded replay is allowed to merge this one.")
  echo "sacrificial PR opened: $URL"
  echo "VICTIM_PR=${URL##*/}"
fi
