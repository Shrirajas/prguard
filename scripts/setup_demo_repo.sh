#!/usr/bin/env bash
# Creates the two throwaway PUBLIC demo repos under the account gh is logged in
# as (the main account) and pushes one branch per demo PR.
#
# It does NOT open the pull requests: a reviewer cannot approve its own PR, so
# they must be opened by a different account. The script prints one link per
# PR; open them IN ORDER while signed in to GitHub as the alternate account.
#
#   ./scripts/setup_demo_repo.sh            # create repos and branches
#   OPEN_PRS=1 ./scripts/setup_demo_repo.sh # also open the PRs as the gh account
set -euo pipefail
cd "$(dirname "$0")/.."
OWNER=$(gh api user --jq .login)
TARGET=${TARGET_NAME:-prguard-demo-target}
OTHER=${OTHER_NAME:-prguard-demo-other}
WORK=$(mktemp -d)
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }
urlencode() { python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1"; }

echo "Creating PUBLIC repos $OWNER/$TARGET and $OWNER/$OTHER. Ctrl-C within 5s to abort."; sleep 5

# target repo: everything in demo_target/ except prs/
rsync -a --exclude prs --exclude __pycache__ demo_target/ "$WORK/target/"
git -C "$WORK/target" init -q -b main
git -C "$WORK/target" add -A
git -C "$WORK/target" commit -q -m "Demo payments app"
gh repo create "$OWNER/$TARGET" --public --source "$WORK/target" --push \
  --description "Throwaway demo target for PRGuard. Contains fake decoy values only."

LINKS=()
for pr in demo_target/prs/*/; do
  branch=$(field "$pr/PR.json" branch); title=$(field "$pr/PR.json" title); body=$(field "$pr/PR.json" body)
  git -C "$WORK/target" checkout -q -b "$branch" main
  rsync -a --exclude PR.json --exclude ci_log.txt --exclude __pycache__ "$pr" "$WORK/target/"
  git -C "$WORK/target" add -A
  git -C "$WORK/target" commit -q -m "$title"
  git -C "$WORK/target" push -q -u origin "$branch"
  if [ "${OPEN_PRS:-0}" = "1" ]; then
    gh pr create --repo "$OWNER/$TARGET" --head "$branch" --base main --title "$title" --body "$body"
  else
    LINKS+=("https://github.com/$OWNER/$TARGET/compare/main...$branch?expand=1&title=$(urlencode "$title")&body=$(urlencode "$body")")
  fi
done

mkdir -p "$WORK/other" && printf '# %s\n\nSecond throwaway repo for the PRGuard cross-repo demo.\n' "$OTHER" > "$WORK/other/README.md"
git -C "$WORK/other" init -q -b main && git -C "$WORK/other" add -A && git -C "$WORK/other" commit -q -m "init"
gh repo create "$OWNER/$OTHER" --public --source "$WORK/other" --push --description "Throwaway repo for PRGuard."

echo
echo "Done. In .env set:  TARGET_REPO=$OWNER/$TARGET   OTHER_REPO=$OWNER/$OTHER"
if [ "${#LINKS[@]}" -gt 0 ]; then
  echo "Now sign in to GitHub as the ALTERNATE account and open these IN ORDER (they become PR #1, #2, #3):"
  for link in "${LINKS[@]}"; do echo "  $link"; done
  echo "Then add that account's login to UNSAFE_ALLOWED_AUTHORS in .env."
fi
