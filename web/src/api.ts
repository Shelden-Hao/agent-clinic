export interface Step {
  step_index: number;
  kind: "tool_call" | "final" | "thought";
  tool: string | null;
  args: Record<string, unknown>;
  ok: boolean;
  error: string | null;
  content: string;
  signature: string;
}

export interface Pattern {
  id: string;
  kind: string;
  label: string;
  tool: string;
  signature: string;
  count: number;
  share: number;
  tasks: string[];
  versions: Record<string, number>;
  run_ids: string[];
  examples: string[];
  advice: string;
}

export interface ForkRow {
  left: Step | null;
  right: Step | null;
  relation: "same" | "only_left" | "only_right";
}

export interface Report {
  ready: boolean;
  reason?: string;
  version: string;
  generated_at: string;
  summary: {
    total: number;
    failed: number;
    fail_rate: number;
    tasks: number;
    models: string[];
    by_version: Record<string, { total: number; failed: number; fail_rate: number }>;
  };
  versions: { version: string; total: number; failed: number; fail_rate: number; tokens: number; avg_steps: number }[];
  flips: { task_id: string; from_ok: number; to_ok: number; delta: number }[];
  regression: {
    from?: string;
    to?: string;
    net?: number;
    fixed: { id: string; label: string; signature: string; old_n: number; new_n: number; delta: number }[];
    worse: { id: string; label: string; signature: string; old_n: number; new_n: number; delta: number }[];
  };
  patterns: Pattern[];
  fork: {
    task_id: string;
    task_prompt: string;
    good_run: string;
    bad_run: string;
    fork_at: number | null;
    length_good: number;
    length_bad: number;
    rows: ForkRow[];
    diagnosis?: { kind: string; label: string; step_index: number; evidence: string; confidence: number };
  } | null;
  diagnoses: Record<string, { kind: string; label: string; step_index: number; evidence: string; confidence: number }>;
}

export async function fetchReport(): Promise<Report> {
  const res = await fetch("/api/overview");
  if (!res.ok) throw new Error(`接口异常 ${res.status}`);
  return res.json();
}
