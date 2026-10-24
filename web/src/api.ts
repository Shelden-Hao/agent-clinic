/** 接口层：所有与后端的契约都收敛在这个文件里，视图不直接 fetch。 */

export interface Step {
  step_index: number;
  kind: "tool_call" | "final" | "thought" | "plan";
  tool: string | null;
  args: Record<string, unknown>;
  ok: boolean;
  error: string | null;
  content: string;
  signature?: string;
  /** 最小化给出的角色标注，仅详情页有 */
  mark?: "root_cause" | "required_context" | "consequence" | "droppable" | null;
  keep?: boolean | null;
  why?: string | null;
}

export interface Diagnosis {
  run_id: string;
  kind: string;
  label: string;
  step_index: number;
  evidence: string;
  confidence: number;
  findings?: { kind: string; label: string; step_index: number; evidence: string; severity: number; recovered: boolean }[];
}

export interface ChainNode {
  step_index: number;
  kind: string;
  tool: string | null;
  role: string;
  keep: boolean;
  why: string;
  ok: boolean;
  args: Record<string, unknown>;
  observation_brief: string;
}

export interface Minimal {
  run_id: string;
  kind: string;
  label: string;
  method: "structural";
  verified_by_replay: boolean;
  original_steps: number;
  minimal_steps: number;
  minimal_kind: "chain" | "terminal_only";
  reduction: number;
  chain: ChainNode[];
  case: Record<string, unknown> & { note?: string; case_id: string };
  counterfactual: {
    at_step: number;
    what_if: string;
    predicted: string;
    confidence: string;
    verified: boolean;
  };
  caveat: string;
}

export interface Score {
  key: string;
  label: string;
  score: number | null;
  passed: boolean | null;
  reason: string;
}

export interface Evaluation {
  run_id: string;
  task_id: string;
  version: string;
  success: boolean;
  overall: number | null;
  scope: "full" | "tool_only";
  note: string;
  scores: Score[];
  skipped: string[];
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
  minimal?: Minimal;
}

export interface ForkRow {
  left: Step | null;
  right: Step | null;
  relation: "same" | "only_left" | "only_right";
}

export interface RunRow {
  run_id: string;
  task_id: string;
  version: string;
  model: string;
  success: boolean;
  steps: number;
  tokens: number;
  kind: string;
  label: string;
  confidence: number | null;
  step_index: number | null;
  overall: number | null;
  tools: string[];
}

export interface Report {
  ready: boolean;
  reason?: string;
  version: string;
  report_version?: string;
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
    diagnosis?: Diagnosis;
  } | null;
  diagnoses: Record<string, Diagnosis>;
  runs: RunRow[];
  evaluations: {
    config: { step_budget: number; token_budget: number };
    evaluators: { key: string; label: string; weight: number }[];
    worst: Evaluation[];
    by_version: Record<
      string,
      { runs: number; overall: number | null; per_evaluator: Record<string, number | null> }
    >;
  };
  cost: {
    overall: {
      runs: number;
      tokens: number;
      tokens_avg: number;
      tokens_p90: number;
      steps_avg: number;
      latency_avg_ms: number;
    };
    by_version: Record<string, { runs: number; tokens: number; tokens_avg: number; tokens_p90: number; steps_avg: number; latency_avg_ms: number }>;
    by_kind: { label: string; runs: number; tokens: number; steps: number; tokens_avg: number; steps_avg: number }[];
    steps_histogram: { steps: number; runs: number }[];
    failure_premium: {
      tokens_delta: number;
      tokens_ratio: number | null;
      steps_delta: number;
      steps_ratio: number | null;
      verdict: string;
    } | null;
  };
  catalog: {
    tasks: { task_id: string; prompt: string; expect: Record<string, unknown>; runs: number; failed: number }[];
    versions: string[];
    models: string[];
    kinds: { kind: string; label: string; count: number }[];
    imported: string[];
  };
}

export interface RunDetail {
  run_id: string;
  task_id: string;
  task_prompt: string;
  version: string;
  model: string;
  imported_from: string;
  success: boolean;
  success_known: boolean;
  fail_reason: string | null;
  expected: Record<string, unknown>;
  actual: Record<string, unknown>;
  usage: Record<string, number>;
  steps: (Step & { observation: unknown; latency_ms: number })[];
  diagnosis: Diagnosis;
  minimal: Minimal | null;
  evaluation: Evaluation;
}

export interface IntakeRun {
  run_id: string;
  task_id: string;
  task_prompt: string;
  model: string;
  success: boolean;
  success_known: boolean;
  expectation_missing: boolean;
  steps: number;
  tokens: number;
  tools: string[];
  diagnosis: Diagnosis;
  minimal: Minimal | null;
  evaluation: Evaluation;
}

export interface IntakeResult {
  ok: boolean;
  format: string;
  format_label?: string;
  stats?: {
    total: number;
    with_tool_calls: number;
    with_final_answer: number;
    with_expectation: number;
    task_level_ready: boolean;
    readable_ratio: number;
  };
  warnings: string[];
  runs: IntakeRun[];
  caveat?: string;
}

export interface RegressionCase {
  case_id: string;
  kind: string;
  label: string;
  task_prompt?: string;
  steps?: { i: number; kind: string; tool: string | null; args: Record<string, unknown>; ok: boolean; role: string }[];
  assertion?: string;
  added_at?: string;
  verified_by_replay?: boolean;
  note?: string;
}

async function jget<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`接口异常 ${res.status}：${url}`);
  return res.json();
}

async function jsend<T>(url: string, body: unknown, method = "POST"): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: method === "DELETE" ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* 保持默认 */
    }
    throw new Error(String(detail));
  }
  return res.json();
}

export const api = {
  overview: () => jget<Report>("/api/overview"),
  catalog: () => jget<Report["catalog"]>("/api/catalog"),
  runs: (query: Record<string, string>) => {
    const qs = new URLSearchParams(query).toString();
    return jget<{ total: number; offset: number; limit: number; rows: RunRow[] }>(`/api/runs?${qs}`);
  },
  run: (id: string) => jget<RunDetail>(`/api/runs/${encodeURIComponent(id)}`),
  intake: (text: string, format = "auto", expectations?: Record<string, unknown>) =>
    jsend<IntakeResult>("/api/intake", { text, format, expectations }),
  sample: (fmt: string) => jget<unknown>(`/api/intake/sample/${fmt}`),
  formats: () => jget<{ formats: { key: string; label: string; sample: string }[] }>("/api/intake/formats"),
  regression: () => jget<{ total: number; cases: RegressionCase[] }>("/api/regression"),
  addRegression: (c: RegressionCase) => jsend<{ ok: boolean; total: number }>("/api/regression/add", { case: c }),
  delRegression: (id: string) =>
    jsend<{ ok: boolean; total: number }>(`/api/regression/${encodeURIComponent(id)}`, null, "DELETE"),
};
