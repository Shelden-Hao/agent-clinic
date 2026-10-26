# Agent 会诊室 SDK

把 Agent 的每一步决策实时送到诊断台。零依赖，浏览器与 Node 18+ 通用。

---

## 30 秒接入

```js
import { Clinic } from "https://agent-clinic.app.workbuddy.host/sdk/agent-clinic.js?sharecode=HRSKWOV2YB";

const clinic = new Clinic({ endpoint: "https://agent-clinic.app.workbuddy.host?sharecode=HRSKWOV2YB", key: "你的上报密钥" });

const trace = clinic.trace({
  taskId: "T01",
  promptVersion: "v2",
  taskPrompt: "用户想把订单 A1024 退掉",
  model: "deepseek-chat",
});

// 关键就这一行：把工具包一层，之后每次调用自动上报
const tools = clinic.wrap(trace, { get_order, refund_order, escalate });

await tools.get_order({ order_id: "A1024" });   // 自动记一步

// 成败由你判定，SDK 不猜
trace.end({ success: false, failReason: "没有发起退款" });
```

打开 `/live`，你会看到这条轨迹滚进实时流，结束时当场给出结论。

> **分享码**：线上演示站用 `?sharecode=` 保护。若你的部署也带访问码，
> 请把它**同时**加在 SDK 文件地址和 `endpoint` 上 —— 两处都要，缺一处会被拦成 403。
> `endpoint` 带查询串是安全的：SDK 会把 `/api/stream/*` 插在 `?` 之前，不会拼出畸形地址。
> 自部署无访问码时去掉即可。

---

## 为什么是 `wrap()` 而不是 `step()`

手工埋点意味着**总有人会忘记埋**。漏掉的那几步恰好就是失败发生的几步，于是归因建立在残缺数据上。

```js
// 手工埋点：每个工具都要包一次，且要记得传 latency、处理异常
const out = await get_order(args);
trace.step({ tool: "get_order", args, observation: out, ok: true, latencyMs: 42 });

// wrap：包一次，之后全自动
const tools = clinic.wrap(trace, { get_order, refund_order, escalate });
```

`wrap()` 会自己打时间戳、自己记异常。工具抛错时它照常把异常**抛出去**——SDK 只记录，不改写你程序的控制流。吞掉异常看起来更稳，实际是把 bug 藏起来了。

---

## API

### `new Clinic(options)`

| 选项 | 默认 | 说明 |
|---|---|---|
| `endpoint` | `""` | 诊断台地址。留空则所有上报直接丢弃，不发任何请求 |
| `key` | `"dev"` | 上报密钥，服务端用环境变量 `CLINIC_KEY` 配置 |
| `batchSize` | `1` | 攒够几条冲刷。想实时看到每一步就保持 1 |
| `flushInterval` | `0` | 定时冲刷间隔（毫秒），0 表示不定时 |
| `maxQueue` | `500` | 队列上限，超了丢最老的——可观测性不该把内存吃满 |
| `timeout` | `8000` | 单次上报超时 |
| `retries` | `2` | 失败重试次数 |
| `debug` | `false` | 打到 console.debug |

### `clinic.trace(options) → Trace`

| 选项 | 说明 |
|---|---|
| `taskId` | 任务标识。同一个任务跑多次用同一个 |
| `promptVersion` | 这条轨迹用的是哪个 prompt 版本 |
| `taskPrompt` | 用户原话 |
| `model` | 模型名 |
| `runId` | 不传则自动生成 |

### `trace.step(s)`

```js
trace.step({
  tool: "refund_order",
  args: { order_id: "A1024", amount: 899 },
  observation: { ok: true, refunded_now: 899 },
  ok: true,
  latencyMs: 123,     // 不传则记 0。用 wrap() 就不用管这个
  tokens: 800,        // 可选
});
```

也可以记非工具步骤：

```js
trace.thought("先查一下订单状态", { latencyMs: 900, tokens: 1200 });
trace.step({ kind: "final", content: "已为您退款 899 元" });
```

### `trace.wrap(tools) → tools`

返回一组同名函数，调用时自动记一步。**原函数照常返回值、照常抛异常。**

### `trace.end(result)`

```js
trace.end({
  success: false,        // 必填
  failReason: "没有发起退款",
  actual: { action: "none" },
  expected: { action: "refund", order_id: "A1024" },
  successKnown: true,    // 默认 true
  usage: { total_tokens: 3200 },
});
```

**`success` 必须由你给。** SDK 不会从「工具有没有报错」推断成败——一个工具全绿、什么都没办的轨迹会被判成成功，这是这类工具最容易犯的错。

### `clinic.flush()` / `clinic.close()`

`flush()` 手动冲刷（页面卸载前值得调一次）；`close()` 清掉定时器。

---

## Node 端

```js
import { Clinic } from "./sdk/agent-clinic.js";   // Node 18+ 自带 fetch

const clinic = new Clinic({ endpoint: "http://127.0.0.1:3111", key: "dev", flushInterval: 2000 });

// …跑你的 Agent…

await clinic.flush();
clinic.close();
```

Node 下不发 `keepalive`，但仍然不阻塞业务：所有上报都是 `void this.flush()`。

---

## 三条设计原则

1. **绝不阻塞业务。** 上报失败、网络超时、服务端 500 —— 全部吞掉并重试两次。一个可观测性工具如果能让业务挂掉，它就不配接在业务里。

2. **不替你判断成败。** SDK 只记录。成败、期望、实际动作都由调用方给。混淆「工具没报错」和「任务办成了」，会给你一条全绿却什么都没做的轨迹。

3. **两段上报。** 每一步发一条轻量事件（只带参数键与观测摘要），整条轨迹在 `end()` 时发完整内容。分开是为了让实时流的延迟足够低——每步都传几 KB 的话，长轨迹会把流拖成幻灯片。

---

## 服务端约束

| 项 | 值 |
|---|---|
| 密钥 | 环境变量 `CLINIC_KEY`，默认 `dev` |
| 限流 | 默认 900 条/分钟（`STREAM_RATE_LIMIT`） |
| 缓冲 | 单进程内存，保留最近 400 条事件 |
| 落盘 | `runtime/stream.jsonl`，仅供回看，不进正式轨迹库 |

**上报的轨迹不会写进 `data/corpus.json`。** 那套语料是报告的数据源，混进陌生人的轨迹会让报告的每个数字都失去可比性。要让轨迹进库，请走 `/intake` 或存进回归集。

多副本部署时各副本各看各的——这是「单进程零依赖」的代价，写在 `/api/stream/info` 的 `limits` 里，界面上也原样显示。

---

## 端点

```
POST /api/stream/step    单步上报（数组或单个对象）
POST /api/stream/trace   整条轨迹上报，返回现场诊断
GET  /api/stream/feed    SSE 推送
GET  /api/stream/info    自述：能做什么、不能做什么
POST /api/stream/replay  回放仓库内真实轨迹（演示用）
```

上报时带 `X-Clinic-Key` 头。CORS 已放开，浏览器端可直接调用。
