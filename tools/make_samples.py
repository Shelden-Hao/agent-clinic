"""从真实采集到的轨迹，转出三种外部格式的样例文件。

为什么不用手写样例：
手写的样例只会验证「我的解析器能读我的想象」。从真轨迹转出来，
再让 ingest 读回去，诊断结果应当一致 —— 这叫 roundtrip 测试，
它验证的是「格式适配没有丢信息」，这是接入层唯一有意义的正确性标准。

产物：data/samples/{otlp,openai,langsmith}.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.build_report import load_runs  # noqa: E402
from analysis.trace import Run  # noqa: E402

OUT = ROOT / "data" / "samples"


def to_openai(run: Run) -> dict:
    """转成 OpenAI Chat Completions 的 messages 序列。"""
    msgs: list[dict] = []
    task = run.task_prompt or f"（任务 {run.task_id}）"
    msgs.append({"role": "user", "content": task})
    n = 0
    for s in run.steps:
        if s.kind == "plan" and s.content:
            msgs.append({"role": "assistant", "content": s.content})
        elif s.kind == "tool_call":
            n += 1
            msgs.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{n}",
                            "type": "function",
                            "function": {
                                "name": s.tool,
                                "arguments": json.dumps(s.args, ensure_ascii=False),
                            },
                        }
                    ],
                }
            )
            body = s.observation
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
            if not s.ok:
                body = json.dumps({"error": s.error or body}, ensure_ascii=False)
            msgs.append({"role": "tool", "tool_call_id": f"call_{n}", "content": body})
        elif s.kind == "final":
            msgs.append({"role": "assistant", "content": s.content or ""})
    return {
        "run_id": run.run_id,
        "task_id": run.task_id,
        "model": run.model,
        "expected": run.expected,
        "success": run.success,
        "messages": msgs,
    }


def to_otlp(run: Run) -> dict:
    """转成 OTLP traces JSON（GenAI 语义约定）。

    spanId 用确定性哈希，保证同一份输入每次产出可复现的样例。
    """
    import hashlib

    def sid(seed: str) -> str:
        return hashlib.sha256(seed.encode()).hexdigest()[:16]

    trace_id = sid(run.run_id)
    spans = []
    base = 1_760_000_000_000  # 毫秒原点，只为让时间单调
    for i, s in enumerate(run.steps, 1):
        if s.kind not in ("tool_call", "plan"):
            continue
        start = base + i * 1000
        end = start + (s.latency_ms or 800)
        attrs = [
            {"key": "gen_ai.request.model", "value": {"stringValue": run.model}},
        ]
        if s.kind == "tool_call":
            attrs += [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                {"key": "gen_ai.tool.name", "value": {"stringValue": s.tool or "unknown"}},
                {
                    "key": "gen_ai.tool.call.arguments",
                    "value": {"stringValue": json.dumps(s.args, ensure_ascii=False)},
                },
                {
                    "key": "gen_ai.tool.call.result",
                    "value": {
                        "stringValue": (
                            s.observation
                            if isinstance(s.observation, str)
                            else json.dumps(s.observation, ensure_ascii=False)
                        )
                    },
                },
            ]
        else:
            attrs += [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                {"key": "gen_ai.completion", "value": {"stringValue": s.content or ""}},
            ]
        spans.append(
            {
                "traceId": trace_id,
                "spanId": sid(f"{run.run_id}-{i}"),
                "parentSpanId": "",
                "name": (s.tool or "chat") if s.kind == "tool_call" else "chat",
                "kind": 3,
                "startTimeUnixNano": str(int(start * 1e6)),
                "endTimeUnixNano": str(int(end * 1e6)),
                "attributes": attrs,
                "status": (
                    {"code": "STATUS_CODE_ERROR", "message": s.error}
                    if not s.ok
                    else {"code": "STATUS_CODE_UNSET"}
                ),
            }
        )
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "order-support-agent"}}
                    ]
                },
                "scopeSpans": [
                    {"scope": {"name": "agent-clinic.sample"}, "spans": spans}
                ],
            }
        ]
    }


def to_langsmith(run: Run) -> list[dict]:
    """转成 LangSmith 的扁平 runs 数组（含 parent_run_id）。

    注意：LangSmith 的 run.error 表示**执行异常**，不表示「任务没办成」。
    所以这里只有在工具真的抛错时才写 error，任务级失败一律留 None ——
    否则解析器会把「没有 error」误读成「任务成功」，那是样例在骗自己的解析器。
    """
    import hashlib

    def rid(seed: str) -> str:
        return hashlib.sha256(seed.encode()).hexdigest()[:16]

    tool_error = next((s for s in run.steps if s.kind == "tool_call" and not s.ok), None)
    root = rid(f"{run.run_id}-root")
    out: list[dict] = [
        {
            "id": root,
            "name": "order-support-agent",
            "run_type": "chain",
            "parent_run_id": None,
            "inputs": {"question": run.task_prompt},
            "outputs": {"output": run.final.content if run.final else ""},
            "error": (tool_error.error or "tool error") if tool_error else None,
            "start_time": "2026-10-24T10:00:00Z",
        }
    ]
    for i, s in enumerate(run.steps, 1):
        if s.kind == "tool_call":
            out.append(
                {
                    "id": rid(f"{run.run_id}-{i}"),
                    "name": s.tool,
                    "run_type": "tool",
                    "parent_run_id": root,
                    "inputs": s.args,
                    "outputs": (
                        s.observation
                        if isinstance(s.observation, (dict, list))
                        else {"content": s.observation}
                    ),
                    "error": None if s.ok else (s.error or "tool error"),
                    "start_time": f"2026-10-24T10:00:{i:02d}Z",
                }
            )
    return out


def main() -> None:
    runs, _tasks = load_runs()
    # 挑三条：一条过度转人工、一条只问不做、一条成功 —— 覆盖三种结局
    print_input = sys.stdout
    from analysis.diagnose import diagnose

    picked: dict[str, Run] = {}
    for r in runs:
        d = diagnose(r, r.expected)
        if d.kind == "over_escalation" and "escalation" not in picked:
            picked["escalation"] = r
        if d.kind == "asked_instead_of_acted" and "asked" not in picked:
            picked["asked"] = r
        if r.success and "ok" not in picked:
            picked["ok"] = r
    sample_runs = list(picked.values())

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "openai.json").write_text(
        json.dumps([to_openai(r) for r in sample_runs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT / "otlp.json").write_text(
        json.dumps(
            {
                "resourceSpans": sum(
                    (to_otlp(r)["resourceSpans"] for r in sample_runs), []
                )
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT / "langsmith.json").write_text(
        json.dumps(
            {"runs": [x for r in sample_runs for x in to_langsmith(r)]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for name, r in picked.items():
        print_input.write(f"{name}: {r.run_id} ({len(r.tool_steps)} 次工具调用)\n")
    print_input.write(f"samples -> {OUT}\n")


if __name__ == "__main__":
    main()
