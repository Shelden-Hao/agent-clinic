/**
 * Agent 会诊室 SDK —— 把 Agent 的每一步决策实时送到诊断台。
 *
 * ## 为什么需要一个 SDK
 *
 * 「导出 JSON 再上传」和「实时上报」不是同一个东西。
 * 前者是事后取证，你得先跑完、先导出、先想起来要做这件事；
 * 后者是边跑边看，失败发生的那一刻就已经在台上了。
 *
 * LangSmith / Langfuse 这类工具的价值有大半来自这一层：
 * 接入成本足够低，低到「顺手就接了」，于是数据才是全的。
 * 数据不全，后面所有的归因分析都是在残缺样本上做结论。
 *
 * ## 三条刻意的设计
 *
 * 1. **绝不阻塞业务**。上报失败、网络超时、服务端 500 —— 全部吞掉。
 *    一个可观测性工具如果能让业务挂掉，它就不配接在业务里。
 *
 * 2. **`wrap()` 比 `step()` 重要**。手工埋点意味着有人会忘记埋。
 *    把工具函数包一层自动上报，接入才真的只需要两行。
 *
 * 3. **成败由你判定，不由 SDK 猜**。SDK 只记录，不判断。
 *    「工具没报错」和「任务没办成」是两件事，混淆两者的工具会给你假绿灯。
 *
 * ## 用法
 *
 * ```js
 * import { Clinic } from "./agent-clinic.js";
 *
 * const clinic = new Clinic({ endpoint: "https://your-host", key: "..." });
 *
 * const trace = clinic.trace({
 *   taskId: "T01",
 *   promptVersion: "v2",
 *   taskPrompt: "用户想把订单 A1024 退掉",
 *   model: "deepseek-chat",
 * });
 * const tools = clinic.wrap(trace, { get_order, refund_order, escalate });
 *
 * // 你的 Agent 照常调用 tools.xxx，每一步自动上报
 * await tools.get_order({ order_id: "A1024" });
 *
 * trace.end({ success: false, failReason: "没有发起退款" });
 * ```
 */

const DEFAULT_OPTIONS = {
  endpoint: "",
  key: "dev",
  /** 攒够多少条上报就冲刷。1 = 每条立即发（实时流需要这个） */
  batchSize: 1,
  /** 定时冲刷间隔（毫秒）。0 = 不自动定时 */
  flushInterval: 0,
  /** 队列上限。超过就丢最老的 —— 可观测性不该把内存吃满 */
  maxQueue: 500,
  /** 网络超时（毫秒） */
  timeout: 8000,
  /** 失败重试次数 */
  retries: 2,
  /** 打开后会把上报情况打到 console.debug */
  debug: false,
};

/** 单步上报不带完整 observation（可能有几 KB），只带摘要，供实时流展示 */
function brief(value, limit = 240) {
  if (value == null) return null;
  if (typeof value === "string") return value.slice(0, limit);
  try {
    const s = JSON.stringify(value);
    return s.length > limit ? s.slice(0, limit) + "…" : s;
  } catch {
    return String(value).slice(0, limit);
  }
}

class Trace {
  constructor(clinic, opts) {
    this.clinic = clinic;
    this.runId =
      opts.runId ||
      `${opts.taskId || "run"}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    this.taskId = opts.taskId || this.runId;
    this.promptVersion = opts.promptVersion || "v1";
    this.taskPrompt = opts.taskPrompt || "";
    this.model = opts.model || "unknown";
    this.startedAt = new Date().toISOString();

    this.steps = [];
    this.usage = { total_tokens: 0 };
    this.ended = false;
    this._t0 = now();
  }

  /**
   * 记一步。工具调用与最终答复都用这一个方法，靠 kind 区分。
   *
   * 时间戳与耗时由 SDK 打，不劳调用方记 —— 让人手工传 latency 的结果，
   * 一定是大部分人传 0。
   */
  step(s) {
    const step = {
      i: this.steps.length + 1,
      kind: s.kind || "tool_call",
      tool: s.tool ?? null,
      args: s.args || {},
      observation: s.observation ?? null,
      ok: s.ok !== false,
      error: s.error ?? null,
      content: s.content ?? null,
      latency_ms: Math.round(s.latencyMs ?? 0),
      tokens: s.tokens ?? 0,
    };
    this.steps.push(step);
    if (step.tokens) this.usage.total_tokens += step.tokens;

    // 实时流靠这一条：只带摘要，完整 observation 等 end() 时一起发。
    // 两段分开是为了让流上的延迟足够低 —— 每一步都传几 KB 的话，
    // 长轨迹会把流拖成幻灯片。
    this.clinic._enqueue({
      type: "step",
      run_id: this.runId,
      task_id: this.taskId,
      version: this.promptVersion,
      model: this.model,
      step: {
        i: step.i,
        kind: step.kind,
        tool: step.tool,
        args: step.args,
        ok: step.ok,
        error: step.error,
        content: brief(step.content),
        observation_brief: brief(step.observation),
        latency_ms: step.latency_ms,
      },
      at: new Date().toISOString(),
    });
    return this;
  }

  /** 记一次模型调用（没有工具，只是 token 消耗与耗时） */
  thought(text, opts = {}) {
    return this.step({
      kind: "thought",
      content: text,
      latencyMs: opts.latencyMs,
      tokens: opts.tokens,
    });
  }

  /**
   * 结束这条轨迹并上报完整内容。
   *
   * `success` 必须由调用方给。SDK 不会替你从「工具有没有报错」推断成败 ——
   * 那是错的，而且错得很隐蔽：一个工具全绿、什么都没办的轨迹会被判成成功。
   */
  end(result = {}) {
    if (this.ended) return this;
    this.ended = true;
    const payload = {
      run_id: this.runId,
      task_id: this.taskId,
      task_prompt: this.taskPrompt,
      prompt_version: this.promptVersion,
      model: this.model,
      started_at: this.startedAt,
      steps: this.steps,
      success: Boolean(result.success),
      // 调用方没明确说「成败已判定」时，按「已判定」处理 ——
      // 因为 SDK 的入参里 success 是必填的，给了就是判过了。
      success_known: result.successKnown !== false,
      actual: result.actual || {},
      fail_reason: result.failReason ?? null,
      expected: result.expected || {},
      usage: { ...this.usage, ...(result.usage || {}) },
      imported_from: "sdk",
    };
    this.clinic._enqueue({ type: "trace", ...payload, at: new Date().toISOString() });
    return payload;
  }

  /** 包裹一组工具函数，让每次调用自动上报。
   *
   * 这是整个 SDK 里最值钱的 30 行：它把「记得埋点」变成「不用记得」。
   */
  wrap(tools) {
    const wrapped = {};
    for (const [name, fn] of Object.entries(tools)) {
      if (typeof fn !== "function") {
        wrapped[name] = fn;
        continue;
      }
      wrapped[name] = async (...callArgs) => {
        const t0 = now();
        const args = callArgs[0] ?? {};
        try {
          const out = await fn(...callArgs);
          this.step({ tool: name, args, observation: out, ok: true, latencyMs: now() - t0 });
          return out;
        } catch (e) {
          this.step({
            tool: name,
            args,
            observation: { error: String(e && e.message ? e.message : e) },
            ok: false,
            error: String(e && e.message ? e.message : e),
            latencyMs: now() - t0,
          });
          // 异常照常抛出去。SDK 只记录，不改写你程序的控制流 ——
          // 吞掉异常看起来「更稳」，实际是把 bug 藏起来了。
          throw e;
        }
      };
    }
    return wrapped;
  }
}

function now() {
  if (typeof performance !== "undefined" && performance.now) return performance.now();
  return Date.now();
}

class Clinic {
  constructor(options = {}) {
    this.opt = { ...DEFAULT_OPTIONS, ...options };
    this.opt.endpoint = String(this.opt.endpoint || "").replace(/\/+$/, "");
    this.queue = [];
    this._timer = null;
    this._flushing = false;
    this._stats = { queued: 0, sent: 0, failed: 0, dropped: 0 };

    if (this.opt.flushInterval > 0) {
      this._timer = setInterval(() => this.flush(), this.opt.flushInterval);
      // Node 环境下别让定时器把进程钉住
      if (this._timer.unref) this._timer.unref();
    }
  }

  trace(opts = {}) {
    return new Trace(this, opts);
  }

  get stats() {
    return { ...this._stats, pending: this.queue.length };
  }

  _enqueue(item) {
    if (!this.opt.endpoint) {
      // 没配 endpoint 就直接丢，但不报错 —— 让人能在没网没服务时先跑通自己的代码
      this._stats.dropped++;
      return;
    }
    this.queue.push({ item, tries: 0 });
    this._stats.queued++;
    if (this.queue.length > this.opt.maxQueue) {
      this.queue.shift();
      this._stats.dropped++;
    }
    if (this.queue.length >= this.opt.batchSize) {
      // 不 await：上报绝不能挡住业务
      void this.flush();
    }
  }

  /** 手动冲刷。beforeExit / 页面卸载时值得显式调一次。 */
  async flush() {
    if (this._flushing || !this.queue.length) return;
    this._flushing = true;
    const batch = this.queue.splice(0, Math.max(this.opt.batchSize, this.queue.length));
    try {
      const url = `${this.opt.endpoint}/api/stream/${this._path(batch)}`;
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), this.opt.timeout);
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Clinic-Key": this.opt.key },
        body: JSON.stringify(batch.map((b) => b.item)),
        signal: ctrl.signal,
        // keepalive 让页面关闭时的最后一批还能发出去
        keepalive: true,
      });
      clearTimeout(to);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this._stats.sent += batch.length;
      if (this.opt.debug) console.debug(`[clinic] 上报 ${batch.length} 条`);
    } catch (e) {
      // 失败重试，但最多两次。可观测性数据不值得为它反复打网络。
      for (const b of batch) {
        b.tries++;
        if (b.tries <= this.opt.retries) this.queue.push(b);
        else this._stats.failed++;
      }
      if (this.opt.debug) console.debug(`[clinic] 上报失败：${e && e.message}`);
    } finally {
      this._flushing = false;
    }
  }

  _path(batch) {
    // 一批里可能既有 step 又有 trace，按多数派决定走哪个端点
    const steps = batch.filter((b) => b.item.type === "step").length;
    return steps * 2 >= batch.length ? "step" : "trace";
  }

  /** 关闭定时器。用完记得调，否则 setInterval 会一直在。 */
  close() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
  }
}

export { Clinic, Trace };
export default Clinic;

// 同时挂到全局，让不走模块系统的页面也能 <script src> 直接用
if (typeof globalThis !== "undefined") {
  globalThis.AgentClinic = { Clinic, Trace };
}
