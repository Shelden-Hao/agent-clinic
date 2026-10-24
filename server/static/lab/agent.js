/**
 * 被测 Agent：一个只有 6 个工具的真实 ReAct 循环。
 *
 * 这段代码是「数据源」而不是「产品」——它必须诚实：
 * - 工具是确定性实现，报错就是真报错，不做假
 * - 模型每次决策都完整落盘（参数、观测、耗时、token）
 * - 这里不埋 bug。失败如果出现，只可能来自模型的决策本身。
 */

const MAX_STEPS = 8;

export const TOOL_SPECS = [
  {
    type: "function",
    function: {
      name: "get_order",
      description: "按订单号查询订单详情：商品名、实付金额、状态、已退款金额。",
      parameters: {
        type: "object",
        properties: { order_id: { type: "string", description: "订单号，如 A1024" } },
        required: ["order_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "get_user",
      description: "按用户 ID 查询用户信息：会员等级、历史退款次数。",
      parameters: {
        type: "object",
        properties: { user_id: { type: "string", description: "用户 ID，如 U001" } },
        required: ["user_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "get_logistics",
      description: "按订单号查询物流状态。",
      parameters: {
        type: "object",
        properties: { order_id: { type: "string" } },
        required: ["order_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "search_policy",
      description: "检索售后政策库。涉及退款期限、金额规则、转人工条件时必须先检索。",
      parameters: {
        type: "object",
        properties: { query: { type: "string", description: "检索关键词" } },
        required: ["query"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "refund_order",
      description: "对订单发起退款。金额必须是本次可退金额。",
      parameters: {
        type: "object",
        properties: {
          order_id: { type: "string" },
          amount: { type: "number", description: "退款金额，单位元" },
          reason: { type: "string" },
        },
        required: ["order_id", "amount", "reason"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "escalate",
      description: "转人工客服处理。超出权限或涉及争议时必须使用。",
      parameters: {
        type: "object",
        properties: { summary: { type: "string", description: "交接摘要" } },
        required: ["summary"],
      },
    },
  },
];

class ToolError extends Error {}

function makeTools(world) {
  const orders = new Map(world.orders.map((o) => [o.order_id, o]));
  const users = new Map(world.users.map((u) => [u.user_id, u]));
  const ledger = [];

  return {
    ledger,
    get_order({ order_id }) {
      const o = orders.get(order_id);
      if (!o) throw new ToolError(`订单 ${order_id} 不存在`);
      return {
        order_id: o.order_id,
        user_id: o.user_id,
        title: o.title,
        paid: o.paid,
        status: o.status,
        days_since_purchase: o.days_since_purchase,
        refunded: o.refunded,
        refundable: Math.round((o.paid - o.refunded) * 100) / 100,
      };
    },
    get_user({ user_id }) {
      const u = users.get(user_id);
      if (!u) throw new ToolError(`用户 ${user_id} 不存在`);
      return { ...u };
    },
    get_logistics({ order_id }) {
      const o = orders.get(order_id);
      if (!o) throw new ToolError(`订单 ${order_id} 不存在`);
      return { order_id, logistics: o.logistics, status: o.status };
    },
    search_policy({ query }) {
      const q = String(query || "");
      if (!q.trim()) throw new ToolError("检索关键词不能为空");
      const scored = world.policies
        .map((p) => {
          let score = 0;
          for (const k of p.keywords) if (q.includes(k)) score += 2;
          for (const ch of q) if (p.body.includes(ch)) score += 0.05;
          return { p, score };
        })
        .filter((x) => x.score > 0.4)
        .sort((a, b) => b.score - a.score)
        .slice(0, 2);
      if (!scored.length) return { hits: [], note: "未命中任何政策条目" };
      return { hits: scored.map(({ p, score }) => ({ id: p.id, title: p.title, body: p.body, score: Math.round(score * 100) / 100 })) };
    },
    refund_order({ order_id, amount, reason }) {
      const o = orders.get(order_id);
      if (!o) throw new ToolError(`订单 ${order_id} 不存在`);
      if (typeof amount !== "number" || Number.isNaN(amount)) {
        throw new ToolError("参数错误：amount 必须是数字");
      }
      if (!reason || !String(reason).trim()) {
        throw new ToolError("参数错误：reason 不能为空");
      }
      if (amount <= 0) throw new ToolError("参数错误：退款金额必须大于 0");
      const remaining = Math.round((o.paid - o.refunded) * 100) / 100;
      if (amount > remaining) {
        throw new ToolError(
          `退款金额 ${amount} 超过可退余额 ${remaining}（实付 ${o.paid}，已退 ${o.refunded}）`
        );
      }
      o.refunded = Math.round((o.refunded + amount) * 100) / 100;
      ledger.push({ order_id, amount, reason });
      return { ok: true, order_id, refunded_now: amount, refunded_total: o.refunded, remaining: Math.round((o.paid - o.refunded) * 100) / 100 };
    },
    escalate({ summary }) {
      if (!summary || !String(summary).trim()) throw new ToolError("参数错误：summary 不能为空");
      return { ok: true, ticket_id: `H${Math.floor(Math.random() * 9000 + 1000)}`, summary };
    },
  };
}

function safeJson(s) {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

/** 累加一次流式响应的内容与 tool_calls 分片。 */
async function callOnce(cloud, { model, messages, signal }) {
  let content = "";
  let finish = null;
  let usage = null;
  const calls = new Map();

  for await (const chunk of cloud.llm.chat.completions.create({
    model,
    messages,
    tools: TOOL_SPECS,
    stream: true,
    stream_options: { include_usage: true },
    signal,
  })) {
    const choice = chunk.choices?.[0];
    const delta = choice?.delta;
    if (delta?.content) content += delta.content;
    if (delta?.tool_calls) {
      for (const tc of delta.tool_calls) {
        const cur = calls.get(tc.index) ?? { id: "", name: "", arguments: "" };
        if (tc.id) cur.id = tc.id;
        if (tc.function?.name) cur.name = tc.function.name;
        if (tc.function?.arguments) cur.arguments += tc.function.arguments;
        calls.set(tc.index, cur);
      }
    }
    if (choice?.finish_reason) finish = choice.finish_reason;
    if (chunk.usage) usage = chunk.usage;
  }

  return { content, finish, usage, toolCalls: [...calls.entries()].sort((a, b) => a[0] - b[0]).map(([, v]) => v) };
}

/**
 * 跑一个任务，返回一条完整轨迹。
 * 注意：world 每次任务都会重新构建工具实例，避免上一次的退款影响下一次（保证任务独立）。
 */
export async function runTask({ cloud, model, task, world, systemPrompt, promptVersion, runIndex, signal, onStep }) {
  const tools = makeTools(JSON.parse(JSON.stringify(world)));
  const messages = [
    { role: "system", content: systemPrompt },
    { role: "user", content: `${task.prompt}\n（当前登录用户：${task.user_id}）` },
  ];

  const steps = [];
  const startedAt = new Date().toISOString();
  let totalTokens = 0;
  let finalContent = null;
  let lastCall = null;

  for (let i = 1; i <= MAX_STEPS; i++) {
    const t0 = performance.now();
    const res = await callOnce(cloud, { model, messages, signal });
    const latency = Math.round(performance.now() - t0);
    totalTokens += res.usage?.total_tokens ?? 0;

    if (!res.toolCalls.length) {
      finalContent = res.content || "";
      steps.push({ i: steps.length + 1, kind: "final", content: finalContent, latency_ms: latency });
      onStep?.(steps.at(-1));
      break;
    }

    // 只有「还会继续行动」的中间文本才算思考，避免最终答复被重复记录成两步
    if (res.content) {
      steps.push({ i: steps.length + 1, kind: "thought", content: res.content.slice(0, 600), latency_ms: latency, tokens: res.usage?.total_tokens ?? 0 });
    }

    messages.push({
      role: "assistant",
      content: res.content || null,
      tool_calls: res.toolCalls.map((c) => ({ id: c.id || `call_${c.name}`, type: "function", function: { name: c.name, arguments: c.arguments || "{}" } })),
    });

    for (const c of res.toolCalls) {
      const args = safeJson(c.arguments) ?? {};
      const fn = tools[c.name];
      let observation;
      let ok = true;
      let error = null;
      const tTool = performance.now();
      if (!fn) {
        ok = false;
        error = `未定义的工具 ${c.name}`;
        observation = { error };
      } else {
        try {
          observation = fn(args);
        } catch (e) {
          ok = false;
          error = e instanceof Error ? e.message : String(e);
          observation = { error };
        }
      }
      const step = {
        i: steps.length + 1,
        kind: "tool_call",
        tool: c.name,
        args,
        observation,
        ok,
        error,
        latency_ms: Math.round(performance.now() - tTool),
      };
      steps.push(step);
      onStep?.(step);
      lastCall = { tool: c.name, args, ok };
      messages.push({ role: "tool", tool_call_id: c.id || `call_${c.name}`, content: JSON.stringify(observation) });
    }
  }

  return {
    run_id: `${task.task_id}-${promptVersion}-${runIndex}`,
    task_id: task.task_id,
    task_prompt: task.prompt,
    prompt_version: promptVersion,
    model,
    started_at: startedAt,
    user_id: task.user_id,
    steps,
    final_content: finalContent,
    last_call: lastCall,
    usage: { total_tokens: totalTokens },
    ledger: tools.ledger,
  };
}

/** 依据「预期动作」判定成功与否 —— 判定标准是工具调用，不是模型的自述。 */
export function judge(run, task) {
  const expect = task.expect || {};
  const refunds = (run.ledger || []).filter((r) => r.order_id === expect.order_id);
  const escalated = (run.steps || []).some((s) => s.kind === "tool_call" && s.tool === "escalate" && s.ok);
  const anyRefund = (run.ledger || []).length > 0;

  if (expect.action === "escalate") {
    if (escalated && !anyRefund) return { success: true, actual: { action: "escalate" } };
    return {
      success: false,
      actual: { action: anyRefund ? "refund" : "none" },
      fail_reason: anyRefund ? "应当转人工，却直接发起了退款" : "应当转人工，但既没退款也没转人工",
    };
  }

  if (expect.action === "refund") {
    if (!refunds.length) {
      return {
        success: false,
        actual: { action: escalated ? "escalate" : "none" },
        fail_reason: escalated ? "应当退款，却转人工了" : "没有发起任何退款",
      };
    }
    const total = refunds.reduce((s, r) => s + r.amount, 0);
    const ok = Math.abs(total - expect.max_amount) < 0.01;
    return {
      success: ok,
      actual: { action: "refund", amount: Math.round(total * 100) / 100 },
      fail_reason: ok ? null : `退款金额 ${total} 与应退金额 ${expect.max_amount} 不符`,
    };
  }

  return { success: false, actual: {}, fail_reason: "任务缺少预期定义" };
}
