import { useEffect, useReducer, useState } from "react";

export const API = (import.meta.env.VITE_API as string | undefined) ?? "http://127.0.0.1:8000";

export type Mode = "UNSAFE" | "GUARDED";

export interface Ev {
  ts: number;
  review_id: string;
  mode: Mode;
  source: string;
  type: string;
  tool?: string | null;
  args_redacted?: Record<string, unknown> | null;
  decision?: "ALLOW" | "DENY" | null;
  reason?: string | null;
  enforcer?: string | null;
  payload: Record<string, any>;
}

export interface Finding {
  agent_type: string;
  severity: string;
  category: string;
  summary: string;
  file_path: string;
  line_start: number;
  suggestion: string;
  confidence: number;
  rationale: string;
  checker_status: string;
  agreed_by: string[];
}

export interface Review {
  review_id: string;
  findings: Finding[];
  overall_confidence: number;
  verdict: "COMMENT" | "REQUEST_CHANGES" | "NEEDS_HUMAN";
  checker_notes: string;
  posted_url: string;
  triage?: { error?: string; risk?: number; risk_label?: string; kind?: string; ms?: number; route?: string; needs_human?: boolean; injection?: { yes: boolean }; executes_pr_code?: { yes: boolean } };
}

export interface ServerState {
  mode: Mode;
  enforcer: string;
  upstream: string;
  llm: string;
  checker: string;
  target_repo: string;
  prs: { number: number; title: string }[];
  sandbox: string;
}

interface Store {
  events: Ev[];
}

type Action = { kind: "event"; ev: Ev } | { kind: "clear" };

function reducer(state: Store, action: Action): Store {
  if (action.kind === "clear") return { events: [] };
  return { events: [...state.events.slice(-1500), action.ev] };
}

/** Subscribe to the backend event stream, reconnecting automatically. */
export function useEvents() {
  const [store, dispatch] = useReducer(reducer, { events: [] });
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    // Per-effect flag: a shared ref would let a stale socket reconnect after remount.
    const closed = { current: false };
    let socket: WebSocket | null = null;
    let timer: number | undefined;

    const connect = () => {
      const ws = new WebSocket(API.replace(/^http/, "ws") + "/ws");
      socket = ws;
      ws.onopen = () => {
        if (closed.current) return ws.close();
        dispatch({ kind: "clear" }); // the server replays its history on connect
        setConnected(true);
      };
      ws.onmessage = (m) => {
        if (!closed.current) dispatch({ kind: "event", ev: JSON.parse(m.data) });
      };
      ws.onclose = () => {
        if (closed.current) return;
        setConnected(false);
        timer = window.setTimeout(connect, 1200);
      };
      ws.onerror = () => ws.close();
    };
    connect();
    return () => {
      closed.current = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return { events: store.events, connected, clear: () => dispatch({ kind: "clear" }) };
}

export async function post(path: string, body?: unknown) {
  const res = await fetch(API + path, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ?? `${res.status}`);
  return data;
}

export async function getState(): Promise<ServerState> {
  return (await fetch(API + "/state")).json();
}

/** Events belonging to the most recent run (a review or a replay). */
export function latestRun(events: Ev[]): { id: string; events: Ev[] } {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "span.start" && (e.source === "pr" || e.source === "replay")) {
      return { id: e.review_id, events: events.filter((x) => x.review_id === e.review_id) };
    }
  }
  return { id: "", events: [] };
}

/** The most recent real review (not a replay). */
export function latestReview(events: Ev[]): { id: string; events: Ev[] } {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "span.start" && e.source === "pr") {
      return { id: e.review_id, events: events.filter((x) => x.review_id === e.review_id) };
    }
  }
  return { id: "", events: [] };
}
