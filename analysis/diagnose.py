"""首个出错决策的定位与定性。

这一层是整个项目的立论基础，所以它必须是**规则可读、结论可追溯**的：
每一条判定都要说出「在第几步、看到了什么、所以判成什么」，而不是丢一个分数。
判不出来就明确返回 unknown，不编。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .trace import ErrorKind, Run, Step, kind_label

# 参数类错误的关键词。工具返回的校验文案通常落在这些说法里。
_ARG_HINTS = (
    "missing",
    "required",
    "invalid",
    "must be",
    "不能为空",
    "缺失",
    "必填",
    "参数",
    "类型",
    "格式",
)

_RE_ORDER_ID = re.compile(r"\b([A-Z]{1,3}\d{4,})\b")
_RE_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


@dataclass
class Finding:
    kind: ErrorKind
    step_index: int
    evidence: str
    severity: int  # 3 = 致命且未被挽回，2 = 致命，1 = 可恢复
    recovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": kind_label(self.kind),
            "step_index": self.step_index,
            "evidence": self.evidence,
            "severity": self.severity,
            "recovered": self.recovered,
        }


@dataclass
class Diagnosis:
    run_id: str
    kind: ErrorKind
    step_index: int
    evidence: str
    confidence: float
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "label": kind_label(self.kind),
            "step_index": self.step_index,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 2),
            "findings": [f.to_dict() for f in self.findings],
        }


def _recovered(run: Run, step: Step) -> bool:
    """后续是否用不同参数重试了同一工具并成功 —— 有就算「自己爬起来了」。"""
    for s in run.steps:
        if s.index <= step.index or s.kind != "tool_call":
            continue
        if s.tool == step.tool and s.ok and s.args != step.args:
            return True
    return False


def _looks_like_argument_error(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _ARG_HINTS)


def _detect_tool_errors(run: Run) -> list[Finding]:
    out: list[Finding] = []
    for s in run.tool_steps:
        if s.ok:
            continue
        err = (s.error or s.observation_text or "").strip()
        recovered = _recovered(run, s)
        kind: ErrorKind = "bad_argument" if _looks_like_argument_error(err) else "tool_error"
        out.append(
            Finding(
                kind=kind,
                step_index=s.index,
                evidence=f"{s.tool} 返回错误：{err[:120]}",
                severity=1 if recovered else 3,
                recovered=recovered,
            )
        )
    return out


def _detect_loop(run: Run) -> list[Finding]:
    calls = run.tool_steps
    for i in range(2, len(calls)):
        sigs = {calls[i - 2].full_signature, calls[i - 1].full_signature, calls[i].full_signature}
        if len(sigs) == 1:
            s = calls[i]
            return [
                Finding(
                    kind="loop",
                    step_index=s.index,
                    evidence=f"连续 3 次以完全相同的参数调用 {s.tool}，没有引入任何新信息",
                    severity=2,
                )
            ]
    return []


def _entities(text: str) -> set[str]:
    return set(_RE_ORDER_ID.findall(text or "")) | set(_RE_DATE.findall(text or ""))


def _detect_hallucinated_fact(run: Run) -> list[Finding]:
    """答案里出现了「系统里根本没有」的实体。

    只在能确凿取证时才判定：实体必须是订单号/日期这类结构化标识，
    因为模型自己算出来的金额属于合理推导，不能算幻觉。
    """
    final = run.final
    if final is None or not final.content:
        return []
    claimed = _entities(final.content)
    if not claimed:
        return []
    grounded = _entities(run.task_prompt)
    for s in run.steps:
        # 关键：不能把最终答复本身当作事实来源，否则它自己说的话就把自己「洗白」了
        if s.kind == "final":
            continue
        grounded |= _entities(s.observation_text)
        grounded |= _entities(s.content or "")
    missing = sorted(claimed - grounded)
    if not missing:
        return []
    return [
        Finding(
            kind="hallucinated_fact",
            step_index=final.index,
            evidence=f"最终答复引用了 {', '.join(missing)}，但整条轨迹的观测里从未出现过",
            severity=3,
        )
    ]


def _detect_escalation_missed(run: Run, expect: dict[str, Any] | None) -> list[Finding]:
    """任务明确要求转人工，Agent 却自行处理了。"""
    if not expect or expect.get("action") != "escalate":
        return []
    refunded = any(s.kind == "tool_call" and s.tool == "refund_order" and s.ok for s in run.steps)
    if not refunded:
        return []
    step = next(s for s in run.steps if s.kind == "tool_call" and s.tool == "refund_order" and s.ok)
    return [
        Finding(
            kind="escalation_missed",
            step_index=step.index,
            evidence=f"该任务应当转人工（{expect.get('reason', '超出受理权限')}），但 Agent 直接发起了退款",
            severity=3,
        )
    ]


def _detect_missing_required_tool(run: Run, required: list[str]) -> list[Finding]:
    if not required:
        return []
    used = set(run.tools_used)
    missing = [t for t in required if t not in used]
    if not missing:
        return []
    final = run.final
    return [
        Finding(
            kind="wrong_tool",
            step_index=final.index if final else len(run.steps),
            evidence=f"该任务必须调用 {', '.join(missing)}，但整条轨迹里一次都没调用就给出了结论",
            severity=3,
        )
    ]


# 「声称已经办了」的说法 —— 出现在答复里但没有对应工具调用，就是空头支票
_PROMISE = ("已为您", "已经为您", "已办理", "已退款", "已提交", "已转接", "工单号", "已经退", "已帮您")
# 「在问用户」的说法 —— 把执行动作变成了征询
_ASKING = ("吗？", "吗?", "需要我", "是否", "确认一下", "您希望", "要不要", "您看", "请确认")


def _detect_task_level(run: Run, expect: dict[str, Any] | None) -> list[Finding]:
    """任务级失败：该做的动作根本没发生。

    这一类最容易被误读成「模型理解错了」，但真跑下来它其实是三种完全不同的病：
    该自己处理却甩给人工、把执行变成反问、嘴上说已办好其实没调工具。
    分开命名，才有资格谈修复。
    """
    if not expect:
        return []
    action = expect.get("action")
    used = set(run.tools_used)
    final = run.final
    at = final.index if final else len(run.steps)
    text = (final.content or "") if final else ""

    if action == "refund":
        if "refund_order" in used:
            return []
        if "escalate" in used:
            step = next(s for s in run.steps if s.kind == "tool_call" and s.tool == "escalate")
            return [
                Finding(
                    kind="over_escalation",
                    step_index=step.index,
                    evidence="该订单本可以直接退款，Agent 却选择了转人工，把可自动完成的动作变成了人工成本",
                    severity=3,
                )
            ]
        if any(k in text for k in _PROMISE):
            return [
                Finding(
                    kind="promised_but_not_done",
                    step_index=at,
                    evidence="答复里声称已经办理，但整条轨迹里没有一次 refund_order 调用",
                    severity=3,
                )
            ]
        if any(k in text for k in _ASKING):
            return [
                Finding(
                    kind="asked_instead_of_acted",
                    step_index=at,
                    evidence="信息已经齐全，本可以直接执行，Agent 却把动作变成了向用户反问",
                    severity=3,
                )
            ]
        return [
            Finding(kind="no_action", step_index=at, evidence="任务要求退款，但既没退款、也没转人工、也没有追问", severity=3)
        ]

    if action == "escalate" and "escalate" not in used:
        if any(k in text for k in _PROMISE):
            return [
                Finding(kind="promised_but_not_done", step_index=at, evidence="答复里声称已处理，但没有调用 escalate", severity=3)
            ]
        return [Finding(kind="no_action", step_index=at, evidence="任务要求转人工，但没有调用 escalate", severity=3)]

    return []


def _detect_premature_answer(run: Run) -> list[Finding]:
    """在关键信息缺失时直接作答：最终答复里断言了政策/规则，却从未检索过政策库。"""
    final = run.final
    if final is None or not final.content:
        return []
    mentions_policy = any(k in final.content for k in ("政策", "规则", "条款", "规定"))
    if mentions_policy and "search_policy" not in run.tools_used:
        return [
            Finding(
                kind="premature_answer",
                step_index=final.index,
                evidence="答复里引用了政策依据，但整条轨迹从未检索过政策库",
                severity=2,
            )
        ]
    return []


def _detect_redundant_query(run: Run) -> list[Finding]:
    """同样的参数查了第二次，且不是连续重复 —— 说明中间的信息在上下文里丢了。

    连续重复属于「原地绕圈」，由 loop 规则负责；这里只处理被别的动作打断之后又重新查一遍的情况。
    """
    seen: dict[str, Step] = {}
    prev_step: Step | None = None
    for s in run.tool_steps:
        if s.full_signature in seen:
            prev = seen[s.full_signature]
            if prev_step is not None and prev_step.full_signature == s.full_signature:
                prev_step = s
                continue
            return [
                Finding(
                    kind="context_lost",
                    step_index=s.index,
                    evidence=f"第 {prev.index} 步已经用相同参数查过 {s.tool}，第 {s.index} 步又查了一遍",
                    severity=1,
                )
            ]
        seen[s.full_signature] = s
        prev_step = s
    return []


def diagnose(run: Run, expect: dict[str, Any] | None = None) -> Diagnosis:
    """定位首个出错决策。

    排序原则：先比「发生的早晚」，再比「严重程度」。
    早发生的小错往往才是根因，晚发生的致命错常常只是它的后果 ——
    这一点是「会诊」和「看日志」最大的区别。
    """
    if run.success:
        return Diagnosis(
            run_id=run.run_id,
            kind="unknown",
            step_index=-1,
            evidence="该轨迹执行成功，未做归因",
            confidence=1.0,
        )

    findings: list[Finding] = []
    findings += _detect_escalation_missed(run, expect)
    findings += _detect_tool_errors(run)
    findings += _detect_loop(run)
    findings += _detect_hallucinated_fact(run)
    findings += _detect_task_level(run, expect)
    findings += _detect_premature_answer(run)
    findings += _detect_redundant_query(run)

    if not findings:
        return Diagnosis(
            run_id=run.run_id,
            kind="unknown",
            step_index=-1,
            evidence=f"现有规则未能定性；运行器记录的失败原因：{run.fail_reason or '未提供'}",
            confidence=0.0,
        )

    findings.sort(key=lambda f: (f.step_index, -f.severity))
    root = findings[0]

    # 置信度是诚实的：有直接证据（工具真报错）就高，靠推断的就低。
    confidence = {
        "tool_error": 0.95,
        "bad_argument": 0.95,
        "loop": 0.9,
        "escalation_missed": 0.9,
        "over_escalation": 0.85,
        "hallucinated_fact": 0.75,
        "promised_but_not_done": 0.8,
        "asked_instead_of_acted": 0.75,
        "no_action": 0.7,
        "wrong_tool": 0.7,
        "premature_answer": 0.6,
        "context_lost": 0.5,
    }.get(root.kind, 0.4)

    return Diagnosis(
        run_id=run.run_id,
        kind=root.kind,
        step_index=root.step_index,
        evidence=root.evidence,
        confidence=confidence,
        findings=findings,
    )
