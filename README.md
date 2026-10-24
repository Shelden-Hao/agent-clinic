# Agent 会诊室 · agent-clinic

> 记录工具告诉你**这一次**发生了什么。
> 这里回答另一个问题：**你的 Agent 到底得了什么病、占几成、最短的复现路径是什么。**

在线体验：https://agent-clinic.app.workbuddy.host/?sharecode=HRSKWOV2YB

---

## 为什么做这个

LangSmith / Langfuse 这类工具已经把「记录」做到了极致：span 树、token、耗时、输入输出，应有尽有。
但真实调 Agent 的人都有同一个体感 —— **打开一条 40 步的 trace，你还是得自己一行行读完才能知道错在哪**。

它们回答的是 L1「记录」和 L2「展示」。下面三层是空的：

| 层 | 问题 | 主流 trace 工具 | 本项目 |
|---|---|---|---|
| L1 记录 | 发生了什么 | ✅ 做得好 | 不做 |
| L2 展示 | 树形时间线 | ✅ 有 | 只占很小一部分 |
| **L3 诊断** | **这一次为什么错、错在哪一步** | ❌ 要人读完 | ✅ |
| **L4 归纳** | **这批失败分几类、各占多少** | ❌ eval 要自己写 | ✅ |
| **L5 沉淀** | **能否缩成可复跑的最小用例** | ❌ 数据集要自己备 | ⏳ 进行中 |

本项目只做 L3–L5。它不接 SDK、不做平台、不替换任何 trace 工具 ——
你把已有的轨迹丢进来就行。

---

## 它实际给出了什么

基于本仓库内 **48 条真实轨迹**（12 个任务 × 2 个 prompt 版本 × 2 次重复，模型 `deepseek-v4-flash`），
报告给出了一个我没预料到、但数据确凿的结论：

```
总失败率：v1 62.5%  →  v2 50.0%       （看起来修复生效了）
  修好了：只问不做     4 → 0
          没有动作     2 → 0
  弄坏了：过度转人工   6 → 9           ← 只看总分会把这件事完全藏起来
```

v2 补上了「什么情况必须转人工」的规程，于是 v1 那批「把执行变成反问」的问题消失了；
但因为它**只写了什么时候要转人工，没写什么时候可以直接办**，Agent 学会了过度保守 ——
一批本来一键就能退的订单被推给了人工。

这就是这个工具存在的理由：**修复会引入新病，而总分不会告诉你。**

---

## 三层能力

### 1. 分叉点定位

同一条任务，成功轨迹与失败轨迹逐决策对齐（LCS），指出第一条走岔的决策。

```
            成功 T02-v1-1                        失败 T02-v1-2
第 1 步   get_order(order_id="A1025")     ←→   get_order(order_id="A1025")
第 2 步   search_policy(query="未发货…")  ←→   search_policy(query="未发货…")
...
第 5 步   refund_order(order_id=…, amount=469)   escalate(summary="用户要求…")   ← 分叉
```

### 2. 失败模式归纳

每次失败算一个签名（性质 + 首个出错工具 + 归一化后的证据），按签名聚类。
**只有 1 条样本的碎片会被合并成「零散异常」，不允许出现「8 类失败里 6 类各 1 次」这种自欺欺人的报告。**

目前能识别的性质：

| 性质 | 含义 |
|---|---|
| `tool_error` | 工具抛错 / 业务拒绝，且没有重试恢复 |
| `bad_argument` | 参数缺失、类型错、取值非法 |
| `over_escalation` | 本可自动完成，却甩给人工 |
| `escalation_missed` | 该转人工却自行处理 |
| `asked_instead_of_acted` | 信息已齐备，却把执行变成了反问 |
| `promised_but_not_done` | 声称已办理，实际没有任何对应调用 |
| `hallucinated_fact` | 结论里的订单号/日期在整条轨迹中不存在 |
| `loop` | 三步内以相同参数重复同一动作 |
| `context_lost` | 被别的动作打断后又用相同参数重查 |
| `no_action` | 整条轨迹没有产生任何有效动作 |
| `unknown` | **判不出来就写判不出来**，不编理由 |

### 3. 修复代价对比

按版本统计每类失败的出现密度，分别给出「修好了」和「弄坏了」两组。

---

## 数据是真的，而且可复查

```
被测对象  一个只有 6 个工具的 ReAct Agent（订单/用户/物流/政策检索/退款/转人工）
业务环境  确定性假业务系统：12 笔订单、8 个用户、6 条售后政策
运行方式  在浏览器里真实调用模型，每一步的决策、参数、观测、耗时、token 完整落盘
模型调用  走平台的无密钥通道（WorkBuddy 云服务 LLM API），不需要任何 API Key
分析      全部离线完成 —— 运行时打开报告不需要模型、不需要网络、不会花钱
```

- **没有埋 bug。** 失败只来自模型的决策本身，不来自我写死的错误。
- 判定「成功/失败」看的是**工具调用是否匹配预期动作与金额**，不是模型的自述 ——
  模型说「已为您办理」不算数，得看有没有真的调 `refund_order`。
- 想验证：打开 `/lab/collect.html`，自己跑一批，再跑 `analysis/build_report.py`。

---

## 技术栈

```
后端   Python 3.11+ / FastAPI / uvicorn     （单一端口，同时托管前端与此 API）
前端   Vue 3 + TypeScript + Vite
诊断   analysis/  纯函数库，10 个单测覆盖边界与「判不出来」的情形
采集   server/static/lab/  浏览器端 ReAct 循环，SDK 本地托管（不依赖 CDN）
发布   WorkBuddy Sites（Python HTTP 服务）
```

运行时零外部依赖：不需要数据库、不需要 Redis、不需要模型密钥。

---

## 本地跑起来

```bash
# 后端
uv venv .venv && uv pip install -r requirements.txt
.venv/Scripts/python.exe main.py          # Windows；macOS/Linux: .venv/bin/python main.py

# 前端（另开一个终端）
cd web && npm install && npm run build    # 产物直接落到 server/static/

# 重新生成世界与报告
python collector/build_world.py
python analysis/build_report.py
python -m unittest discover -s tests
```

---

## 这份报告不声称什么

- 它只解释**已有轨迹**，不会替你重跑模型。要验证修复，就再采一批。
- 归因是规则式的，「置信度」是规则的证据强度，不是统计概率。
- 样本 48 条。低于 30 条时，占比只应看趋势，不应当结论。
- 目前只覆盖「工具调用型 Agent」；纯对话型 Agent 的失败归因不在范围内。

---

## 目录

```
analysis/          诊断引擎（trace 模型 / 归因 / 对齐 / 聚类 / 报告编译）
collector/         被测世界与任务集的构造脚本
data/              世界数据、任务集、prompt 版本、真实轨迹快照、报告快照
server/            FastAPI 应用与静态资源
server/static/lab/ 浏览器端真实采集台
web/               Vue 3 + TS 前端源码
tests/             诊断引擎单元测试
tools/             采集驱动与截图脚本
```
