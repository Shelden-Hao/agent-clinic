"""通用轨迹接入：把别的系统产出的轨迹，归一成本项目的 Run。

为什么这一层是整个项目的分水岭：
在此之前，会诊室只能诊断「我自己采的 48 条轨迹」，那是个演示；
有了这一层，任何人把 LangSmith / OpenTelemetry / 裸 OpenAI messages 的轨迹丢进来，
都能拿到同一套诊断 —— 工具才变成平台。

设计约束（三条，都是为了不撒谎）：
1. **不做猜测式映射**。字段对不上就放进 warnings，明确告诉用户「这部分我没读懂」，
   而不是塞一个看起来合理的默认值，让后面的诊断建立在沙地上。
2. **保留原始格式标记**。每条 Run 都记下 came_from，报告里会显示，
   避免用户误以为这份诊断对它自己的 Agent 有同等效力。
3. **纯函数**。输入 payload，输出 Run 列表，不碰网络、不碰文件系统，可单测。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .trace import Run, Step

# ── 格式清单 ────────────────────────────────────────────────────────────────
FORMATS = {
    "native": "Agent 会诊室原生格式",
    "otlp": "OpenTelemetry / OTLP（GenAI 语义约定）",
    "openai": "OpenAI Chat Completions messages",
    "langsmith": "LangSmith 运行导出",
}

_TAG = re.compile(r"^[A-Z]{1,3}\d{4,}$")


@dataclass
class IngestResult:
    runs: list[Run] = field(default_factory=list)
    fmt: str = "unknown"
    warnings: list[str] = field(default_factory=list)
    origin: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.fmt,
            "format_label": FORMATS.get(self.fmt, self.fmt),
            "origin": self.origin,
            "runs": len(self.runs),
            "warnings": self.warnings,
        }


# ── 格式识别 ────────────────────────────────────────────────────────────────
def detect_format(payload: Any) -> str:
    """按结构特征识别格式。识别不出来返回 unknown，不硬套。"""
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            if "resourceSpans" in payload[0]:
                return "otlp"
            if "run_type" in payload[0] or "child_runs" in payload[0]:
                return "langsmith"
            if "messages" in payload[0]:
                return "openai"
            if "steps" in payload[0] and "run_id" in payload[0]:
                return "native"
        return "unknown"

    if not isinstance(payload, dict):
        return "unknown"

    if "resourceSpans" in payload:
        return "otlp"
    if isinstance(payload.get("runs"), list):
        first = payload["runs"][0] if payload["runs"] else {}
        if isinstance(first, dict) and ("run_type" in first or "child_runs" in first):
            return "langsmith"
    if isinstance(payload.get("messages"), list):
        return "openai"
    if isinstance(payload.get("steps"), list) and "run_id" in payload:
        return "native"
    return "unknown"


# ── 工具函数 ────────────────────────────────────────────────────────────────
def _attr_map(attributes: Iterable[dict] | None) -> dict[str, Any]:
    """把 OTLP 的 [{key, value:{stringValue|intValue|...}}] 拍平成 {key: 值}。"""
    out: dict[str, Any] = {}
    for a in attributes or []:
        if not isinstance(a, dict):
            continue
        key = a.get("key")
        if not key:
            continue
        v = a.get("value") or {}
        if not isinstance(v, dict):
            out[key] = v
            continue
        for vk, vv in v.items():
            out[key] = vv
            break
    return out


def _json_loads(text: Any, fallback: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return fallback
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return fallback


def _ns(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    # OTLP 里时间是纳秒字符串；也容忍毫秒/秒
    if n > 1e17:
        return n / 1e6  # ns -> ms
    if n > 1e14:
        return n / 1e3  # us -> ms
    return n


def _looks_error(text: str) -> bool:
    low = (text or "").lower()
    return any(
        k in low
        for k in ("error", "fail", "invalid", "missing", "required", "拒绝", "失败", "错误", "不存在", "超时")
    )


# ── 各格式适配器 ────────────────────────────────────────────────────────────
def _from_native(payload: Any) -> IngestResult:
    res = IngestResult(fmt="native", origin="native")
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and "runs" in payload:
        items = payload.get("runs") or []
    elif isinstance(payload, dict) and "steps" in payload:
        # 单条轨迹：用户从自己的代码里 print 了一条，直接贴进来是最常见的用法
        items = [payload]
    else:
        items = []
    for i, d in enumerate(items):
        if not isinstance(d, dict) or "steps" not in d:
            res.warnings.append(f"第 {i + 1} 条缺少 steps 字段，已跳过")
            continue
        d = {**d}
        d.setdefault("run_id", f"imported-{i + 1}")
        d.setdefault("task_id", d["run_id"])
        steps = []
        for j, s in enumerate(d.get("steps") or []):
            s = dict(s) if isinstance(s, dict) else {}
            s.setdefault("i", j + 1)
            s.setdefault("kind", "tool_call")
            steps.append(s)
        d["steps"] = steps
        d["imported_from"] = "native"
        res.runs.append(_run(d))
    return res


def _run(d: dict[str, Any]) -> Run:
    from .trace import run_from_dict

    return run_from_dict(d)


def _from_openai(payload: Any) -> IngestResult:
    """把一段 OpenAI messages 对话还原成轨迹。

    只有 assistant 的 tool_calls 与 tool 回执能构成「动作」，
    纯文本 assistant 消息视为思考，最后一条视为最终答复。
    """
    res = IngestResult(fmt="openai", origin="openai")
    items = payload if isinstance(payload, list) else [payload]

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        msgs = item.get("messages")
        if not isinstance(msgs, list):
            res.warnings.append(f"第 {idx + 1} 条没有 messages 数组，已跳过")
            continue

        steps: list[Step] = []
        pending: dict[str, Step] = {}
        task_prompt = ""
        model = item.get("model") or "unknown"
        total_tokens = 0

        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):  # 多模态内容，取文本片段
                content = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                )
            if role == "user" and not task_prompt:
                task_prompt = content or ""
                continue
            if role == "tool":
                call_id = m.get("tool_call_id")
                step = pending.get(call_id or "")
                if step is not None:
                    step.observation = _json_loads(content, content)
                    ok = not _looks_error(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
                    step.ok = ok
                    if not ok:
                        step.error = (content if isinstance(content, str) else str(content))[:300]
                continue
            if role == "assistant":
                calls = m.get("tool_calls") or []
                if not calls:
                    if content:
                        steps.append(Step(index=len(steps) + 1, kind="final", content=str(content)))
                    continue
                if content:
                    steps.append(Step(index=len(steps) + 1, kind="plan", content=str(content)[:600]))
                for c in calls:
                    fn = (c or {}).get("function") or {}
                    args = _json_loads(fn.get("arguments"), {})
                    step = Step(
                        index=len(steps) + 1,
                        kind="tool_call",
                        tool=fn.get("name") or "unknown",
                        args=args if isinstance(args, dict) else {"_raw": args},
                    )
                    steps.append(step)
                    if c.get("id"):
                        pending[c["id"]] = step
                usage = m.get("usage") or {}
                if isinstance(usage, dict):
                    total_tokens += int(usage.get("total_tokens") or 0)

        if not any(s.kind == "final" for s in steps):
            res.warnings.append(f"第 {idx + 1} 条没有任何 assistant 文本答复，无法判定最终结果")
            steps.append(Step(index=len(steps) + 1, kind="final", content=""))

        res.runs.append(
            Run(
                run_id=item.get("run_id") or f"openai-{idx + 1}",
                task_id=item.get("task_id") or item.get("run_id") or f"openai-{idx + 1}",
                prompt_version=item.get("prompt_version", "imported"),
                task_prompt=task_prompt,
                steps=steps,
                # OpenAI 格式本身没有「成功/失败」字段，只能靠任务期望判定；
                # 拿不到期望时一律记为 False 并说明，绝不默认成功。
                success=bool(item.get("success")) if "success" in item else False,
                success_known="success" in item,
                expected=item.get("expected") or {},
                fail_reason=item.get("fail_reason") or ("未提供期望结果，无法判定成败" if "success" not in item else None),
                model=model,
                started_at=item.get("started_at", ""),
                usage={"total_tokens": total_tokens} if total_tokens else {},
                imported_from="openai",
                expectation_missing=not bool(item.get("expected")),
            )
        )
    return res


def _from_otlp(payload: Any) -> IngestResult:
    """OTLP traces JSON -> Run。

    按 GenAI 语义约定取属性；这是能对接 LangChain / OpenAI Agents SDK /
    LlamaIndex 等一大批框架的标准入口。
    """
    res = IngestResult(fmt="otlp", origin="otlp")
    spans_all: list[dict[str, Any]] = []
    resources = payload.get("resourceSpans") if isinstance(payload, dict) else None
    if resources is None and isinstance(payload, list):
        resources = payload
    for rs in resources or []:
        svc = _attr_map((rs.get("resource") or {}).get("attributes")).get("service.name", "")
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans") or []:
                spans_all.append({**sp, "_service": svc})

    if not spans_all:
        res.warnings.append("没有解析到任何 span，请确认是 OTLP traces 的 JSON 导出")
        return res

    traces: dict[str, list[dict[str, Any]]] = {}
    for sp in spans_all:
        traces.setdefault(sp.get("traceId") or "no-trace", []).append(sp)

    for trace_id, spans in traces.items():
        spans.sort(key=lambda s: _ns(s.get("startTimeUnixNano")))
        steps: list[Step] = []
        model, tokens = "unknown", 0
        for sp in spans:
            a = _attr_map(sp.get("attributes"))
            op = a.get("gen_ai.operation.name") or sp.get("name") or ""
            model = a.get("gen_ai.request.model") or a.get("gen_ai.response.model") or model
            tokens += int(a.get("gen_ai.usage.input_tokens") or 0) + int(
                a.get("gen_ai.usage.output_tokens") or 0
            )
            status = sp.get("status") or {}
            errored = str(status.get("code") or "").upper() in ("STATUS_CODE_ERROR", "ERROR") or bool(
                status.get("message")
            )

            if op == "execute_tool" or a.get("gen_ai.tool.name"):
                tool = a.get("gen_ai.tool.name") or sp.get("name") or "unknown"
                args = _json_loads(a.get("gen_ai.tool.call.arguments"), {})
                obs = _json_loads(a.get("gen_ai.tool.call.result"), a.get("gen_ai.tool.call.result"))
                steps.append(
                    Step(
                        index=len(steps) + 1,
                        kind="tool_call",
                        tool=str(tool),
                        args=args if isinstance(args, dict) else {"_raw": args},
                        observation=obs,
                        ok=not errored,
                        error=(status.get("message") or None) if errored else None,
                        latency_ms=int(_ns(sp.get("endTimeUnixNano")) - _ns(sp.get("startTimeUnixNano"))),
                    )
                )
            elif op == "chat" or "gen_ai.request.model" in a:
                text = a.get("gen_ai.completion") or a.get("gen_ai.output.messages") or ""
                if text:
                    steps.append(Step(index=len(steps) + 1, kind="plan", content=str(text)[:600]))

        if not steps:
            res.warnings.append(f"trace {trace_id[:8]} 里没有可识别为工具调用的 span，已跳过")
            continue

        # OTLP 不带业务语义，最终答复取自最后一个 span 的输出
        last = spans[-1]
        final_text = str(
            _attr_map(last.get("attributes")).get("gen_ai.completion")
            or (last.get("status") or {}).get("message")
            or ""
        )
        steps.append(Step(index=len(steps) + 1, kind="final", content=final_text))

        any_error = any(not s.ok for s in steps if s.kind == "tool_call")
        res.runs.append(
            Run(
                run_id=f"otlp-{trace_id[:12]}",
                task_id=trace_id[:12],
                prompt_version="imported",
                task_prompt="",
                steps=steps,
                success=not any_error,
                success_known=any_error,
                fail_reason="工具 span 报错" if any_error else "未提供任务期望，任务完成度未判定",
                model=model,
                usage={"total_tokens": tokens} if tokens else {},
                imported_from="otlp",
                expectation_missing=True,
            )
        )
        res.warnings.append(
            f"trace {trace_id[:8]}：OTLP 不含「任务应当完成什么」，本次只做了工具层体检，未做任务级判定"
        )
    return res


def _from_langsmith(payload: Any) -> IngestResult:
    """LangSmith runs 导出。

    真实导出有两种形态：带 child_runs 的树，和靠 parent_run_id 相连的扁平数组。
    扁平数组里**没有 trace_id 字段**，必须顺着 parent_run_id 往上爬到根，
    否则每一条 run 都会自成一棵「树」—— 这正是我第一版写错的地方。
    """
    res = IngestResult(fmt="langsmith", origin="langsmith")
    items = payload.get("runs") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        res.warnings.append("不是 LangSmith 的 runs 数组")
        return res

    flat: list[dict[str, Any]] = []

    def flatten(node: Any) -> None:
        if not isinstance(node, dict):
            return
        n = dict(node)
        children = n.pop("child_runs", None) or []
        flat.append(n)
        for ch in children:
            flatten(ch)

    for it in items:
        flatten(it)

    by_id: dict[str, dict[str, Any]] = {}
    for r in flat:
        k = r.get("id") or r.get("run_id")
        if k:
            by_id[str(k)] = r

    def root_of(r: dict[str, Any]) -> str:
        cur = r
        seen: set[str] = set()
        while cur.get("parent_run_id"):
            pid = str(cur["parent_run_id"])
            if pid in seen:
                break
            seen.add(pid)
            nxt = by_id.get(pid)
            if nxt is None:
                return pid  # 父 run 不在导出里，就用父 id 当分组键
            cur = nxt
        return str(cur.get("id") or cur.get("run_id") or "no-root")

    groups: dict[str, list[dict[str, Any]]] = {}
    for r in flat:
        groups.setdefault(root_of(r), []).append(r)

    for root, group in groups.items():
        group.sort(key=lambda r: str(r.get("start_time") or ""))
        steps: list[Step] = []
        tokens = 0
        model = "unknown"
        task_prompt = ""
        root_run = by_id.get(root) or group[0]

        for r in group:
            rt = r.get("run_type")
            if rt == "llm":
                meta = (r.get("extra") or {}).get("metadata") or {}
                model = meta.get("ls_model_name") or model
                usage = (r.get("outputs") or {}).get("usage_metadata") or {}
                if isinstance(usage, dict):
                    tokens += int(usage.get("total_tokens") or 0)
            if rt == "tool":
                steps.append(
                    Step(
                        index=len(steps) + 1,
                        kind="tool_call",
                        tool=r.get("name") or "unknown",
                        args=r.get("inputs") or {},
                        observation=r.get("outputs"),
                        ok=not r.get("error"),
                        error=(str(r.get("error"))[:300] if r.get("error") else None),
                    )
                )
            elif rt == "chain":
                inp = r.get("inputs") or {}
                if isinstance(inp, dict) and not task_prompt:
                    task_prompt = str(
                        inp.get("question") or inp.get("input") or inp.get("query") or inp.get("messages") or ""
                    )
            if rt == "llm" and r.get("outputs"):
                out = r.get("outputs")
                text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                steps.append(Step(index=len(steps) + 1, kind="plan", content=text[:600]))

        if not steps:
            continue

        out = root_run.get("outputs")
        if isinstance(out, dict) and len(out) == 1:
            out = next(iter(out.values()))
        final_text = out if isinstance(out, str) else (json.dumps(out, ensure_ascii=False) if out else "")
        steps.append(Step(index=len(steps) + 1, kind="final", content=final_text[:4000]))

        any_error = any(not s.ok for s in steps if s.kind == "tool_call")
        root_error = root_run.get("error")
        res.runs.append(
            Run(
                run_id=f"ls-{str(root)[:12]}",
                task_id=str(root)[:12],
                prompt_version="imported",
                task_prompt=task_prompt,
                steps=steps,
                # 有 error 才是确定的失败；没有 error 只说明「工具没报错」，不代表任务完成
                success=False if (any_error or root_error) else True,
                success_known=bool(any_error or root_error),
                fail_reason=(
                    str(root_error)[:200]
                    if root_error
                    else ("工具调用报错" if any_error else "未提供任务期望，任务完成度未判定")
                ),
                model=model,
                usage={"total_tokens": tokens} if tokens else {},
                imported_from="langsmith",
                expectation_missing=True,
            )
        )
        res.warnings.append(
            f"run {str(root)[:8]}：LangSmith 的 outputs 是自由结构，最终答复按 JSON 原文保留，未做语义抽取"
        )
    return res


_ADAPTERS = {
    "native": _from_native,
    "openai": _from_openai,
    "otlp": _from_otlp,
    "langsmith": _from_langsmith,
}


def ingest(payload: Any, fmt: str | None = None) -> IngestResult:
    """主入口：自动识别格式并归一化。"""
    detected = fmt or detect_format(payload)
    if detected not in _ADAPTERS:
        return IngestResult(
            fmt="unknown",
            warnings=[
                "无法识别格式。支持："
                + "、".join(f"{k}（{v}）" for k, v in FORMATS.items())
                + "。也可能是 JSON 结构不完整。"
            ],
        )
    res = _ADAPTERS[detected](payload)
    if not res.runs:
        res.warnings.append("解析成功但没有得到任何可用轨迹")
    return res


def ingest_jsonl(text: str, fmt: str | None = None) -> IngestResult:
    """按行读取（每行一条 payload），常用于批量导入。"""
    merged = IngestResult(origin="jsonl")
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError as e:
            merged.warnings.append(f"第 {lineno} 行不是合法 JSON：{e}")
            continue
        one = ingest(payload, fmt)
        merged.fmt = one.fmt
        merged.runs.extend(one.runs)
        for w in one.warnings:
            if w not in merged.warnings:
                merged.warnings.append(w)
    if not merged.fmt:
        merged.fmt = "unknown"
    return merged


def roundtrip_stats(runs: list[Run]) -> dict[str, Any]:
    """给接入结果一份体检：这些轨迹能被诊断引擎读懂多少。"""
    total = len(runs)
    with_tools = sum(1 for r in runs if r.tool_steps)
    with_final = sum(1 for r in runs if r.final and (r.final.content or "").strip())
    with_expect = sum(1 for r in runs if r.expected)
    return {
        "total": total,
        "with_tool_calls": with_tools,
        "with_final_answer": with_final,
        "with_expectation": with_expect,
        # 没有期望结果时，任务级归因（该类占我全部规则的 4 类）无法工作，要如实告知
        "task_level_ready": with_expect > 0,
        "readable_ratio": round((with_tools / total) if total else 0.0, 3),
    }


def _finite(x: float) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
