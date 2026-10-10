#!/usr/bin/env bash
# Run one review through the API and print a readable summary.
#   ./scripts/review_once.sh <pr_number> [UNSAFE|GUARDED]
set -euo pipefail
cd "$(dirname "$0")/.."
API=${API:-http://127.0.0.1:8000}
PR=${1:-2}; MODE=${2:-GUARDED}
curl -s -X POST "$API/mode" -H 'Content-Type: application/json' -d "{\"mode\":\"$MODE\"}" >/dev/null
ID=$(curl -s -X POST "$API/review" -H 'Content-Type: application/json' -d "{\"pr_number\":$PR}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["review_id"])')
for _ in $(seq 1 150); do
  grep -q '"type":"review.ready"' "runs/$ID.jsonl" 2>/dev/null && break
  grep -q 'review failed' "runs/$ID.jsonl" 2>/dev/null && break
  sleep 1
done
python3 - "$ID" <<'PY'
import json, sys
rid = sys.argv[1]
ev = [json.loads(l) for l in open(f"runs/{rid}.jsonl")]
t0 = ev[0]["ts"]
print(f"review {rid}  mode={ev[0]['mode']}  took {ev[-1]['ts'] - t0:.1f}s")
cost = 0.0
for e in ev:
    p = e["payload"]
    if e["type"] == "span.end" and p.get("node") and (p.get("usage") or p.get("note")):
        u = p.get("usage") or {}
        cost += u.get("cost_usd") or 0
        print(f"  {e['source']:22} +{e['ts'] - t0:5.1f}s  model={u.get('model','-')}  tokens={u.get('prompt_tokens',0)}/{u.get('completion_tokens',0)}"
              f"  calls={u.get('calls',0)} cached={u.get('cached_calls',0)}  findings={p.get('findings', p.get('kept'))}  {p.get('note') or p.get('notes') or ''}  {' '.join(u.get('notes') or [])}")
    if e["type"] == "decision" and e["source"] not in ("prguard-orchestrator",):
        print(f"  {e['source']:22} {e['decision']:5} {e['tool']} {json.dumps({k: v for k, v in (e['args_redacted'] or {}).items() if k not in ('body', 'repo')})[:90]} {e.get('reason') if e['decision'] == 'DENY' else ''}")
    if e["type"] == "error":
        print(f"  ERROR {e['source']}: {e.get('reason')}")
ready = next((e["payload"] for e in ev if e["type"] == "review.ready"), None)
if not ready:
    sys.exit("  no review produced")
print(f"  verdict={ready['verdict']}  confidence={ready['overall_confidence']}  llm cost=${cost:.5f}")
print(f"  checker: {ready['checker_notes']}")
for f in ready["findings"]:
    print(f"   {f['severity']:8} {f['agent_type']:8} {f['category'][:26]:26} {f['file_path']}:{f['line_start']}  conf={f['confidence']:.2f} [{f['checker_status']}] {f['summary'][:80]}")
PY
