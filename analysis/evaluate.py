"""规则评分器：给每条轨迹一个可解释的体检分。

和市面上评测框架的区别：
它们多数要你自己写评测函数、自己准备数据集、自己跑实验；
这里把「Agent 轨迹的通用体检项」做成开箱即用的一组评分器，
每一条都输出**通过/不通过 + 一句人话说清为什么**，而不是一个孤立的小数。

设计原则：
1. 一个评分器只判一件事。判不了就返回 None（skip），不硬给分。
2. 分数只有 0/0.5/1 三档。轨迹质量不是连续可微的，给 0.73 分是假装精确。
3. 总分不是平均分，是**加权**：任务完成度权重最高，效率最低。
   因为「省了 token 但没办成事」不该拿高分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .trace import Run

_ID = re.compile(r"\b([A-Z]{1,4}[-_]?\d{3,})\b")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

_PROMISE = ("已为您", "已经为您", "已办理", "已退款", "已提交", "已转接", "已经退", "已帮您")


@dataclass
class Score:
    key: str
    label: str
    score: float | None  # None = 这一项对它不适用
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "score": self.score,
            "passed": None if self.score is None else self.score >= 0.5,
            "reason": self.reason,
        }


@dataclass
class EvalConfig:
    step_budget: int = 8          # 工具调用步数预算
    token_budget: int = 20000     # 单条轨迹 token 预算


# ── 单个评分器 ──────────────────────────────────────────────────────────────
def _task_completion(run: Run, cfg: EvalConfig) -> Score:
    expect = run.expected or {}
    action = expect.get("action")
    if not action:
        return Score("task_completion", "任务完成", None, "该轨迹没有提供任务期望，无法判定是否完成")
    used = set(run.tools_used)
    if action == "refund":
        if "refund_order" in used:
            return Score("task_completion", "任务完成", 1.0, "调用了 refund_order，要求的退款动作发生了")
        if "escalate" in used:
            return Score("task_completion", "任务完成", 0.0, "要求退款，实际只转人工，任务未完成")
        text = (run.final.content if run.final else "") or ""
        if any(k in text for k in _PROMISE):
            return Score("task_completion", "任务完成", 0.0, "答复声称已退款，但没有 refund_order 调用记录")
        return Score("task_completion", "任务完成", 0.0, "既没有退款，也没有转人工")
    if action == "escalate":
        if "escalate" in used:
            return Score("task_completion", "任务完成", 1.0, "调用了 escalate，正确移交人工")
        return Score("task_completion", "任务完成", 0.0, "要求转人工，但没有 escalate 调用")
    return Score("task_completion", "任务完成", None, f"未知的动作类型 {action}")


def _arg_hygiene(run: Run, cfg: EvalConfig) -> Score:
    calls = run.tool_steps
    if not calls:
        return Score("arg_hygiene", "参数卫生", None, "没有工具调用")
    bad = [s for s in calls if not s.ok]
    if not bad:
        return Score("arg_hygiene", "参数卫生", 1.0, f"{len(calls)} 次工具调用全部被接受")
    return Score(
        "arg_hygiene",
        "参数卫生",
        0.0,
        f"{len(bad)}/{len(calls)} 次调用被拒绝，首个在第 {bad[0].index} 步（{bad[0].tool}）",
    )


def _groundedness(run: Run, cfg: EvalConfig) -> Score:
    final = run.final
    if final is None or not (final.content or "").strip():
        return Score("groundedness", "结论有据", None, "没有最终答复")
    claimed = set(_ID.findall(final.content)) | set(_DATE.findall(final.content))
    if not claimed:
        return Score("groundedness", "结论有据", 1.0, "答复里没有出现需要溯源的标识符")
    grounded = set(_ID.findall(run.task_prompt)) | set(_DATE.findall(run.task_prompt))
    for s in run.steps:
        if s.kind == "final":
            continue
        grounded |= set(_ID.findall(s.observation_text))
        grounded |= set(_ID.findall(s.content or ""))
    missing = sorted(claimed - grounded)
    if missing:
        return Score(
            "groundedness", "结论有据", 0.0, f"答复引用了 {', '.join(missing)}，轨迹观测里从未出现"
        )
    return Score("groundedness", "结论有据", 1.0, "答复里的每个标识符都能在观测里找到出处")


def _no_loop(run: Run, cfg: EvalConfig) -> Score:
    calls = run.tool_steps
    for i in range(2, len(calls)):
        sigs = {calls[i - 2].full_signature, calls[i - 1].full_signature, calls[i].full_signature}
        if len(sigs) == 1:
            return Score("no_loop", "无空转", 0.0, f"第 {calls[i].index} 步之前连续 3 次相同调用，没有引入新信息")
    return Score("no_loop", "无空转", 1.0, "没有出现连续重复调用")


def _escalation_policy(run: Run, cfg: EvalConfig) -> Score:
    action = (run.expected or {}).get("action")
    if action not in ("refund", "escalate"):
        return Score("escalation_policy", "转人工策略", None, "该任务不涉及转人工判定")
    used = set(run.tools_used)
    if action == "refund":
        if "escalate" in used and "refund_order" not in used:
            return Score("escalation_policy", "转人工策略", 0.0, "本可自动办理，却推给了人工")
        return Score("escalation_policy", "转人工策略", 1.0, "没有把可自动完成的动作甩给人工")
    if "escalate" in used:
        return Score("escalation_policy", "转人工策略", 1.0, "该转人工时确实转了")
    return Score("escalation_policy", "转人工策略", 0.0, "该转人工却没转，自行处理了")


def _efficiency(run: Run, cfg: EvalConfig) -> Score:
    n = len(run.tool_steps)
    if n == 0:
        return Score("efficiency", "执行效率", None, "没有工具调用")
    tokens = int(run.usage.get("total_tokens") or 0)
    if n <= cfg.step_budget and (tokens == 0 or tokens <= cfg.token_budget):
        extra = f"，{tokens} tokens" if tokens else ""
        return Score("efficiency", "执行效率", 1.0, f"{n} 步{extra}，在预算内")
    return Score(
        "efficiency",
        "执行效率",
        0.0,
        f"{n} 步超出 {cfg.step_budget} 步预算" + (f"，{tokens} tokens 超出 {cfg.token_budget}" if tokens > cfg.token_budget else ""),
    )


def _recovery(run: Run, cfg: EvalConfig) -> Score:
    """出错之后有没有自己爬起来 —— 这是 Agent 韧性的核心指标。"""
    errors = [s for s in run.tool_steps if not s.ok]
    if not errors:
        return Score("recovery", "错误恢复", None, "没有发生工具错误")
    first = errors[0]
    for s in run.tool_steps:
        if s.index > first.index and s.tool == first.tool and s.ok:
            return Score("recovery", "错误恢复", 1.0, f"第 {first.index} 步出错后，第 {s.index} 步用不同参数重试成功")
    return Score("recovery", "错误恢复", 0.0, f"第 {first.index} 步出错后，没有成功的重试")


EVALUATORS: list[tuple[str, Callable[[Run, EvalConfig], Score]]] = [
    ("task_completion", _task_completion),
    ("arg_hygiene", _arg_hygiene),
    ("groundedness", _groundedness),
    ("no_loop", _no_loop),
    ("escalation_policy", _escalation_policy),
    ("recovery", _recovery),
    ("efficiency", _efficiency),
]

WEIGHTS = {
    "task_completion": 3.0,
    "arg_hygiene": 1.5,
    "groundedness": 1.5,
    "no_loop": 1.0,
    "escalation_policy": 2.0,
    "recovery": 1.0,
    "efficiency": 0.5,
}


def evaluate(run: Run, cfg: EvalConfig | None = None) -> dict[str, Any]:
    cfg = cfg or EvalConfig()
    scores = [fn(run, cfg) for _, fn in EVALUATORS]
    num = den = 0.0
    for s in scores:
        if s.score is None:
            continue
        w = WEIGHTS.get(s.key, 1.0)
        num += s.score * w
        den += w
    overall = round(num / den, 2) if den else None
    # 评分范围：没有任务期望时，只能体检工具层。
    # 这种情况下 overall 会天然偏高（因为最重的「任务完成」被跳过了），
    # 所以必须把范围标出来，否则 1.0 看起来像「满分」，实际是「没考」。
    # 判据直接用 expected 是否为空 —— 不依赖调用方记不记得设 expectation_missing。
    scope = "tool_only" if (run.expectation_missing or not run.expected) else "full"
    return {
        "run_id": run.run_id,
        "task_id": run.task_id,
        "version": run.prompt_version,
        "success": run.success,
        "overall": overall,
        "scope": scope,
        "note": (
            "仅工具层评分：该轨迹没有任务期望，任务完成度与转人工策略两项未参与打分"
            if scope == "tool_only"
            else ""
        ),
        "scores": [s.to_dict() for s in scores],
        "skipped": [s.key for s in scores if s.score is None],
    }


def evaluate_all(runs: list[Run], cfg: EvalConfig | None = None) -> dict[str, Any]:
    rows = [evaluate(r, cfg) for r in runs]
    keys = [k for k, _ in EVALUATORS]
    labels = {s["key"]: s["label"] for s in rows[0]["scores"]} if rows else {}

    def agg(subset: list[dict[str, Any]], key: str) -> float | None:
        vals = [s["score"] for r in subset for s in r["scores"] if s["key"] == key and s["score"] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    by_version: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_version.setdefault(r["version"], []).append(r)

    return {
        "config": {"step_budget": (cfg or EvalConfig()).step_budget, "token_budget": (cfg or EvalConfig()).token_budget},
        "evaluators": [
            {"key": k, "label": labels.get(k, k), "weight": WEIGHTS.get(k, 1.0)} for k in keys
        ],
        "rows": rows,
        # 排名：按总分升序，最差的排前面 —— 报告要让人先看到问题
        "worst": sorted(
            [r for r in rows if r["overall"] is not None], key=lambda r: r["overall"]
        )[:8],
        "by_version": {
            v: {
                "runs": len(subset),
                "overall": round(
                    sum(r["overall"] for r in subset if r["overall"] is not None)
                    / max(1, sum(1 for r in subset if r["overall"] is not None)),
                    3,
                )
                if any(r["overall"] is not None for r in subset)
                else None,
                "per_evaluator": {k: agg(subset, k) for k in keys},
            }
            for v, subset in sorted(by_version.items())
        },
    }
