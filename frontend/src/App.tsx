import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import FlowGraph from "./FlowGraph";
import { getState, latestReview, latestRun, post, useEvents, type Ev, type Review, type ServerState } from "./store";

const time = (ts: number) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false });

function argSummary(e: Ev): string {
  const a = e.args_redacted ?? {};
  const repo = typeof a.repo === "string" ? a.repo.split("/")[1] : "";
  const parts = [repo && `repo=${repo}${a.pull_number ? `#${a.pull_number}` : ""}`, a.path && `path=${a.path}`, a.event && `event=${a.event}`, a.branch && `branch=${a.branch}`];
  return parts.filter(Boolean).join(" ");
}

function Panel({ title, right, children, className = "" }: { title: string; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={`flex min-h-0 flex-col border border-line bg-panel/70 ${className}`}>
      <header className="flex shrink-0 items-center justify-between gap-2 border-b border-line px-3 py-1.5">
        <h2 className="mr-2 truncate font-mono text-[10.5px] uppercase tracking-[0.18em] text-dim">{title}</h2>
        {right}
      </header>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
  );
}

function DecisionFeed({ events }: { events: Ev[] }) {
  const [filter, setFilter] = useState<"ALL" | "ALLOW" | "DENY">("ALL");
  const rows = useMemo(
    () => events.filter((e) => (e.type === "decision" && (filter === "ALL" || e.decision === filter)) || (e.type === "error" && e.tool && filter === "ALL")),
    [events, filter],
  );
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [rows.length]);
  const allow = events.filter((e) => e.type === "decision" && e.decision === "ALLOW").length;
  const deny = events.filter((e) => e.type === "decision" && e.decision === "DENY").length;

  return (
    <Panel
      className="h-[340px] lg:h-auto"
      title="Decision feed"
      right={
        <div className="flex gap-1 font-mono text-[10px]">
          {(["ALL", "ALLOW", "DENY"] as const).map((f) => (
            <button key={f} onClick={() => setFilter(f)}
              className={`border px-1.5 py-0.5 ${filter === f ? "border-slate-400 text-slate-100" : "border-line text-dim"}`}>
              {f === "ALL" ? `All ${allow + deny}` : f === "ALLOW" ? `Allow ${allow}` : `Deny ${deny}`}
            </button>
          ))}
        </div>
      }
    >
      <div className="font-mono text-[11px] leading-relaxed">
        {rows.length === 0 && <p className="p-3 text-dim">No tool calls yet. Run a review or the assume-breach replay.</p>}
        {rows.map((e, i) => e.type === "error" ? (
          <div key={i} className="type-in border-b border-line/50 px-3 py-1">
            <span className="text-dim">{time(e.ts)}</span> <span className="font-bold text-warn">ERROR</span>{" "}
            <span className="text-sky-300">{e.source}</span> <span className="text-slate-100">{e.tool}</span>{" "}
            <span className="text-dim">{argSummary(e)}</span>
            <div className="pl-[4.6rem] text-[10px] text-warn/90">— {e.reason} <span className="text-dim">· not an access-control decision</span></div>
          </div>
        ) : (
          <div key={i} className="type-in border-b border-line/50 px-3 py-1">
            <span className="text-dim">{time(e.ts)}</span>{" "}
            <span className={`font-bold ${e.decision === "DENY" ? "text-deny" : "text-allow"}`}>{e.decision === "DENY" ? "DENY " : "ALLOW"}</span>{" "}
            <span className="text-sky-300">{e.source}</span> <span className="text-slate-100">{e.tool}</span>{" "}
            <span className="text-dim">{argSummary(e)}</span>
            <div className={`pl-[4.6rem] text-[10px] ${e.decision === "DENY" ? "text-deny/80" : "text-dim"}`}>
              {e.decision === "DENY" ? `— ${e.reason}` : e.enforcer === "NONE" ? "— no access control" : "— allowed by policy"}
              <span className="text-dim"> · {e.enforcer === "LOCAL_POLICY" ? "LOCAL POLICY (stand-in)" : e.enforcer}{e.decision === "DENY" && e.payload.policy ? ` · ${e.payload.policy}` : ""}</span>
            </div>
          </div>
        ))}
        <div ref={end} />
      </div>
    </Panel>
  );
}

function CanaryPanel({ run }: { run: Ev[] }) {
  const sandbox = [...run].reverse().find((e) => e.type === "sandbox");
  const hits = run.filter((e) => e.type === "canary.received");
  const names: string[] = hits.length ? hits[hits.length - 1].payload.env_names ?? [] : [];
  const canary = hits.length ? hits[hits.length - 1].payload.canary : null;
  const guarded = sandbox?.payload.isolation === "docker";
  const notRun = sandbox?.payload.status === "unavailable" || sandbox?.payload.status === "refused";

  return (
    <Panel className="h-[230px] lg:h-auto" title="What the PR code could reach" right={<span className={`font-mono text-[10px] ${hits.length ? "text-deny" : "text-allow"}`}>{hits.length} received</span>}>
      <div className="p-3 font-mono text-[11px]">
        {!sandbox && <p className="text-dim">Waiting for a review to run the PR's linter and tests.</p>}
        {notRun && <p className="text-warn">PR code was not run: {sandbox?.payload.reason}</p>}
        {sandbox && !notRun && hits.length === 0 && (
          <div className={guarded ? "text-allow" : "text-dim"}>
            <div className="text-lg font-bold">0 received</div>
            <div className="mt-1 text-[10.5px]">
              {guarded
                ? "network: none · host env vars passed: 0 · filesystem: read-only"
                : "host subprocess · no canary has reported yet"}
            </div>
          </div>
        )}
        {hits.length > 0 && (
          <div>
            <div className="mb-2 text-deny">PR code ran inside the reviewer and could read {names.length} environment variables:</div>
            <div className="flex flex-wrap gap-1.5">
              {names.map((n, i) => (
                <span key={n} className="type-in border border-deny/50 bg-deny/10 px-1.5 py-0.5 text-deny" style={{ animationDelay: `${i * 140}ms` }}>{n}</span>
              ))}
            </div>
            {canary && <div className="type-in mt-2 text-warn" style={{ animationDelay: `${names.length * 140}ms` }}>planted marker read back: PRGUARD_CANARY={canary}</div>}
            <div className="mt-2 text-[10px] text-dim">Names only; values are never sent. All values here are fake decoys.</div>
          </div>
        )}
      </div>
    </Panel>
  );
}

const SEV: Record<string, string> = {
  CRITICAL: "bg-deny text-black", HIGH: "bg-deny/25 text-deny", MEDIUM: "bg-warn/25 text-warn", LOW: "bg-slate-700 text-slate-200", INFO: "bg-slate-800 text-dim",
};

function ReviewPanel({ review, events, blocked }: { review: Review | null; events: Ev[]; blocked: number }) {
  const waiting = events.some((e) => e.type === "hitl.waiting");
  const approved = events.some((e) => e.type === "hitl.approved");
  const posted = events.filter((e) => e.type === "review.posted");
  const live = !review ? events.filter((e) => e.type === "finding").map((e) => e.payload as Review["findings"][number]) : review.findings;
  const top = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"].find((s) => live.some((f) => f.severity === s)) ?? "NONE";
  const byAgent = live.reduce<Record<string, typeof live>>((acc, f) => ({ ...acc, [f.agent_type]: [...(acc[f.agent_type] ?? []), f] }), {});

  return (
    <Panel className="h-[340px] lg:h-auto" title="Review" right={review && <span className={`font-mono text-[10px] ${review.verdict === "NEEDS_HUMAN" ? "text-warn" : "text-slate-300"}`}>{review.verdict} · conf {review.overall_confidence.toFixed(2)}</span>}>
      <div className="p-3 text-[12px]">
        {live.length === 0 && !review && <p className="font-mono text-[11px] text-dim">No review yet.</p>}
        {(review || live.length > 0) && (
          <div className={`mb-2 border px-2 py-1 font-mono text-[11px] ${top === "CRITICAL" || top === "HIGH" ? "border-deny/60 text-deny" : "border-line text-slate-200"}`}>
            Risk: {top} · {live.filter((f) => f.severity === "CRITICAL").length} critical · {blocked} blocked action{blocked === 1 ? "" : "s"}
          </div>
        )}
        {review?.triage?.risk !== undefined && (
          <div className={`mb-2 border px-2 py-1 font-mono text-[10.5px] ${review.triage.needs_human ? "border-warn/60 text-warn" : "border-line text-dim"}`}>
            Triage (Jev, {review.triage.ms} ms): risk {review.triage.risk.toFixed(1)}/4 {review.triage.risk_label} · {review.triage.kind} · route: {review.triage.route}
            {review.triage.injection?.yes && " · flagged: reviewer manipulation"}{review.triage.executes_pr_code?.yes && " · flagged: executes PR code"}
          </div>
        )}
        {waiting && !approved && <div className="mb-2 border border-warn/60 px-2 py-1 font-mono text-[11px] text-warn">Waiting for a human. APPROVE is locked for this PR.</div>}
        {approved && <div className="mb-2 border border-allow/60 px-2 py-1 font-mono text-[11px] text-allow">Human approved. APPROVE unlocked for this PR only.</div>}
        {Object.entries(byAgent).map(([agent, list]) => (
          <div key={agent} className="mb-2">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-widest text-dim">{agent}</div>
            {list.map((f, i) => (
              <div key={i} className="type-in mb-1.5 border-l-2 border-line pl-2">
                <div>
                  <span className={`mr-1.5 px-1 py-px font-mono text-[9.5px] font-bold ${SEV[f.severity]}`}>{f.severity}</span>
                  <span className="text-slate-100">{f.summary}</span>
                </div>
                <div className="font-mono text-[10px] text-dim">
                  {f.file_path}:{f.line_start} · {f.category} · conf {f.confidence.toFixed(2)} ·{" "}
                  <span className={f.checker_status === "CONFIRMED" ? "text-allow" : f.checker_status === "DOWNGRADED" ? "text-warn" : "text-dim"}>checker: {f.checker_status}</span>
                </div>
              </div>
            ))}
          </div>
        ))}
        {review?.checker_notes && <p className="font-mono text-[10px] text-dim">Checker: {review.checker_notes}</p>}
        {posted.map((p, i) => (
          <p key={i} className="mt-1 font-mono text-[10.5px] text-allow">
            Posted {p.payload.event} review{p.payload.url && <> · <a className="underline" href={p.payload.url} target="_blank" rel="noreferrer">open on GitHub</a></>}
          </p>
        ))}
      </div>
    </Panel>
  );
}

export default function App() {
  const { events, connected } = useEvents();
  const [server, setServer] = useState<ServerState | null>(null);
  const [pr, setPr] = useState(2);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(() => getState().then(setServer).catch(() => setServer(null)), []);
  useEffect(() => { refresh(); }, [refresh, connected]);
  useEffect(() => {
    if (server?.prs.length && !server.prs.some((p) => p.number === pr)) setPr(server.prs[0].number);
  }, [server, pr]);

  const run = useMemo(() => latestRun(events), [events]);
  const rev = useMemo(() => latestReview(events), [events]);
  const review = useMemo(() => {
    const ready = [...rev.events].reverse().find((e) => e.type === "review.ready");
    return ready ? (ready.payload as Review) : null;
  }, [rev]);
  const reviewRunning = rev.id !== "" && !rev.events.some((e) => e.type === "span.end" && e.source === "pr");
  const blocked = rev.events.filter((e) => e.type === "decision" && e.decision === "DENY").length;
  const canApprove = review?.verdict === "NEEDS_HUMAN" && !rev.events.some((e) => e.type === "hitl.approved");

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label); setError("");
    try { await fn(); } catch (e) { setError(String((e as Error).message)); }
    setBusy(""); refresh();
  };

  const mode = server?.mode ?? "GUARDED";
  const guarded = mode === "GUARDED";
  const btn = "whitespace-nowrap border border-line px-2.5 py-1.5 font-mono text-[10.5px] uppercase tracking-wider text-slate-200 hover:border-slate-400 disabled:opacity-35 lg:px-3 lg:text-[11px]";

  return (
    <div className="flex min-h-full flex-col lg:h-full">
      <header className="flex flex-wrap items-center gap-2 border-b border-line bg-panel px-3 py-2 lg:gap-3 lg:px-4">
        <div className="mr-2 font-mono text-[15px] font-bold tracking-[0.2em] text-slate-100">PR<span className={guarded ? "text-allow" : "text-deny"}>GUARD</span></div>
        <select value={pr} onChange={(e) => setPr(Number(e.target.value))} className="max-w-[230px] truncate border border-line bg-ink px-2 py-1.5 font-mono text-[11px] text-slate-200">
          {(server?.prs ?? [{ number: 1, title: "clean" }, { number: 2, title: "hostile" }]).map((p) => (
            <option key={p.number} value={p.number}>PR #{p.number} · {p.title}</option>
          ))}
        </select>
        <div className="flex border border-line font-mono text-[11px] font-bold">
          {(["UNSAFE", "GUARDED"] as const).map((m) => (
            <button key={m} disabled={!!busy || reviewRunning} onClick={() => act("mode", () => post("/mode", { mode: m }))}
              className={`px-4 py-1.5 tracking-widest ${mode === m ? (m === "UNSAFE" ? "bg-deny text-black" : "bg-allow text-black") : "text-dim hover:text-slate-200"}`}>
              {m}
            </button>
          ))}
        </div>
        <span className={`border px-2 py-1 font-mono text-[10px] tracking-wider ${guarded ? "border-sky-500/60 text-sky-300" : "border-deny/60 text-deny"}`}>
          enforcer: {server?.enforcer ?? "…"}
        </span>
        <div className="hidden flex-1 lg:block" />
        <button className={btn} disabled={!!busy || reviewRunning} onClick={() => act("review", () => post("/review", { pr_number: pr }))}>{reviewRunning ? "Reviewing…" : "Run review"}</button>
        <button className={btn} disabled={!!busy} onClick={() => act("replay", () => post(`/replay?agent=prguard-security&pr=${pr}`))}>{busy === "replay" ? "Replaying…" : "Assume-breach replay"}</button>
        <button className={`${btn} ${canApprove ? "border-warn text-warn" : ""}`} disabled={!canApprove || !!busy} onClick={() => act("approve", () => post(`/approve/${rev.id}`))}>Human approve</button>
        <button className={btn} disabled={!!busy || reviewRunning} onClick={() => act("reset", () => post("/reset"))}>Reset</button>
        <span className={`h-2 w-2 rounded-full ${connected ? "bg-allow" : "bg-deny"}`} title={connected ? "connected" : "disconnected"} />
      </header>

      {(error || !connected || server) && (
        <div className="flex flex-wrap gap-x-4 gap-y-0.5 border-b border-line px-3 py-1 font-mono text-[10px] text-dim lg:px-4">
          {!connected && <span className="text-deny">backend not reachable, retrying…</span>}
          {error && <span className="text-deny">{error}</span>}
          {server && <>
            <span>repo: {server.target_repo}</span>
            <span className={server.upstream === "mock" ? "text-warn" : ""}>github: {server.upstream === "mock" ? "MOCK upstream" : "GitHub MCP"}</span>
            <span className={server.llm === "MOCK LLM" ? "text-warn" : ""}>reviewers: {server.llm}</span>
            <span>checker: {server.checker}</span>
            <span className={server.sandbox === "ready" ? "" : "text-warn"}>sandbox: {server.sandbox}</span>
          </>}
        </div>
      )}

      <main className="grid flex-1 grid-cols-1 gap-px bg-line lg:min-h-0 lg:grid-cols-[minmax(0,1fr)_430px] lg:grid-rows-[minmax(0,1fr)_260px]">
        <section className="relative h-[330px] min-h-0 bg-ink lg:h-auto">
          <div className="absolute inset-0"><FlowGraph events={run.events} mode={mode} enforcer={server?.enforcer ?? ""} /></div>
        </section>
        <DecisionFeed events={events} />
        <div className="grid grid-cols-1 gap-px bg-line sm:grid-cols-2 lg:min-h-0">
          <CanaryPanel run={rev.events} />
          <ReviewPanel review={review} events={rev.events} blocked={blocked} />
        </div>
        <Panel title="Sandbox profile" className="h-[230px] lg:h-auto">
          <pre className="whitespace-pre-wrap p-3 font-mono text-[10.5px] text-dim">
            {(() => {
              const s = [...rev.events].reverse().find((e) => e.type === "sandbox");
              if (!s) return guarded ? "docker run --network none --read-only --cap-drop ALL\n  --user nobody  (no -e, no --env-file)" : "subprocess on the reviewer host\n  env = fake decoys + canary";
              const { status, ...rest } = s.payload;
              return `${status}\n` + Object.entries(rest).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : v}`).join("\n");
            })()}
          </pre>
        </Panel>
      </main>
    </div>
  );
}
