#!/usr/bin/env bash
# Milestone check: run the hostile PR in both modes N times and report what the
# canary listener received each time, plus the replay decisions.
# Needs the backend (:8000) and canary listener (:9999) running.
set -euo pipefail
API=${API:-http://127.0.0.1:8000}
CANARY=${CANARY:-http://127.0.0.1:9999}
RUNS=${RUNS:-3}
PR=${PR:-2}

count() { curl -s "$CANARY/canary" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'; }

run_review() {  # mode
  curl -s -X POST "$API/mode" -H 'Content-Type: application/json' -d "{\"mode\":\"$1\"}" >/dev/null
  curl -s -X DELETE "$CANARY/canary" >/dev/null
  local id; id=$(curl -s -X POST "$API/review" -H 'Content-Type: application/json' -d "{\"pr_number\":$PR}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["review_id"])')
  for _ in $(seq 1 150); do
    grep -q '"type":"review.ready"' "runs/$id.jsonl" 2>/dev/null && break
    grep -q 'review failed' "runs/$id.jsonl" 2>/dev/null && break
    sleep 1
  done
  python3 - "$id" "$1" "$(count)" <<'PY'
import json, sys
rid, mode, canaries = sys.argv[1], sys.argv[2], sys.argv[3]
events = [json.loads(l) for l in open(f"runs/{rid}.jsonl")]
sandbox = [e["payload"] for e in events if e["type"] == "sandbox"]
ready = next((e["payload"] for e in events if e["type"] == "review.ready"), None)
last = sandbox[-1] if sandbox else {}
print(f"  {mode:8} canaries={canaries}  sandbox={last.get('status')}/{last.get('isolation')} exit={last.get('exit_codes')}  "
      f"verdict={ready and ready['verdict']}  findings={ready and len(ready['findings'])}  posted={bool(ready and ready['posted_url'])}")
PY
}

echo "== PR #$PR, $RUNS runs per mode =="
for i in $(seq 1 "$RUNS"); do run_review UNSAFE; done
for i in $(seq 1 "$RUNS"); do run_review GUARDED; done

for mode in UNSAFE GUARDED; do
  curl -s -X POST "$API/mode" -H 'Content-Type: application/json' -d "{\"mode\":\"$mode\"}" >/dev/null
  echo "== assume-breach replay, $mode =="
  curl -s -X POST "$API/replay?agent=prguard-security&pr=$PR" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("  enforcer:", d["enforcer"])
for r in d["results"]:
    print("  %d %-5s %-28s %s" % (r["step"], r["decision"], r["tool"], r["reason"]))'
done
curl -s -X POST "$API/reset" >/dev/null
curl -s -X POST "$API/mode" -H 'Content-Type: application/json' -d '{"mode":"GUARDED"}' >/dev/null
