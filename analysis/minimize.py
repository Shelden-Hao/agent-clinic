"""轨迹最小化：把一条长长的失败轨迹，压成「最短仍能说明问题」的因果链。

为什么这件事值得做：
一条失败轨迹通常有 10~20 步，其中真正导致失败的往往只有 3~5 步。
人读不完 20 步，测试集也吃不下一整条原始轨迹。
把噪音去掉之后，剩下的东西才可以被放进回归集、被反复验证。

**方法论上的一条硬线，必须写在代码里也要写在界面上：**
本模块做的是**结构最小化**（structural minimization），不是
「逐步删除后重跑验证」的最小化（replay-verified minimization）。
前者靠依赖分析推断，成本为零，但只是推断；
后者需要真花 token 重跑，才能断言「删掉这步仍然失败」。
所以输出的每个用例都带 `verified_by_replay: false`，
界面上也必须原样显示——用不实的最小化冒充验证过的最小化，
是这个项目最不该犯的错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .diagnose import Diagnosis
from .trace import Run, Step, kind_label

# 结构化标识符：订单号、日期、UID 之类。只有这类东西才能作为「依赖」的硬证据，
# 自由文本的语义相似度不作数，否则会把「仅措辞相近」误判成依赖。
_ID = re.compile(r"\b([A-Z]{1,4}[-_]?\d{3,})\b")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

Role = str  # root_cause | required_context | consequence | droppable | anchor


@dataclass
class ChainNode:
    step_index: int
    kind: str
    tool: str | None
    role: Role
    keep: bool
    why: str
    ok: bool = True
    args: dict[str, Any] = field(default_factory=dict)
    observation_brief: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "kind": self.kind,
            "tool": self.tool,
            "role": self.role,
            "keep": self.keep,
            "why": self.why,
            "ok": self.ok,
            "args": self.args,
            "observation_brief": self.observation_brief,
        }


def _ids(text: str) -> set[str]:
    t = text or ""
    return set(_ID.findall(t)) | set(_DATE.findall(t))


def _step_ids(step: Step) -> set[str]:
    """一个步骤「产出了哪些标识符」以及「消费了哪些标识符」。"""
    own = _ids(step.observation_text) | _ids(step.content or "")
    own |= _ids(" ".join(str(v) for v in (step.args or {}).values()))
    return own


def _brief(value: Any, limit: int = 160) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _args_ids(step: Step) -> set[str]:
    return _ids(" ".join(str(v) for v in (step.args or {}).values()))


# ── 处方：按病种给出「改哪一处」的反事实假设 ────────────────────────────────
_WHAT_IF = {
    "bad_argument": "补齐工具调用缺失的必填字段后重放",
    "tool_error": "遇到工具报错时改用另一组参数重试一次",
    "wrong_tool": "把该调用的工具写进动作清单，避免整条轨迹漏掉它",
    "hallucinated_fact": "禁止模型自行补全编号，所有标识必须来自工具返回",
    "loop": "加入重复检测：连续两次相同调用即强制换策略",
    "context_lost": "把关键事实即时沉淀成结构化摘要，而不是留在原始观测里",
    "premature_answer": "补充前置门槛：涉及金额/期限的问题未检索不得作答",
    "escalation_missed": "把转人工条件写成可判定清单并前置校验",
    "over_escalation": "补一份「可直接办理」的白名单：条件满足时必须直接执行，不得转人工",
    "asked_instead_of_acted": "明确写「信息齐备时必须直接执行，不得以向用户确认代替执行」",
    "promised_but_not_done": "收尾前自检：凡声称完成的动作必须能在本轮调用记录里找到",
    "no_action": "补一条收尾指令：信息足够后必须产生动作",
    "unknown": "现有规则未定性，建议人工看一遍这条轨迹",
}


def _missing_fields(evidence: str) -> list[str]:
    """从错误文案里抠出缺失字段名，让「补什么」是可执行的而不是空话。"""
    out: list[str] = []
    for pat in (
        r"missing[^:：]*[:：]\s*([a-z_]+(?:\s*,\s*[a-z_]+)*)",
        r"required[^:：]*[:：]\s*([a-z_]+)",
        r"缺少\s*([a-z_]+)",
        r"必填[^:：]*[:：]\s*([a-z_]+)",
    ):
        for m in re.finditer(pat, evidence or "", flags=re.I):
            for name in re.split(r"\s*,\s*", m.group(1)):
                name = name.strip()
                if name and name not in out:
                    out.append(name)
    return out


def minimize(run: Run, diag: Diagnosis) -> dict[str, Any]:
    """对一条失败轨迹做因果链最小化。

    判定顺序（保守优先，宁可少丢也不丢错）：
    1. 诊断指认的那一步 → root_cause，一定保留
    2. root_cause 依赖的前序步骤 → required_context，保留
    3. root_cause 之后被它牵连的步骤 → consequence，保留（这是失败传播的可见证据）
    4. 其余 → droppable，但只有拿得出「它的产出从未被消费」这条硬证据时才丢
    """
    tools = [s for s in run.steps if s.kind == "tool_call"]
    final = run.final
    anchor = diag.step_index

    nodes: dict[int, ChainNode] = {}

    def put(step: Step, role: Role, keep: bool, why: str) -> None:
        nodes[step.index] = ChainNode(
            step_index=step.index,
            kind=step.kind,
            tool=step.tool,
            role=role,
            keep=keep,
            why=why,
            ok=step.ok,
            args=step.args,
            observation_brief=_brief(step.observation or step.content, 120),
        )

    # ── 1. 锚点 ──
    anchor_step = next((s for s in run.steps if s.index == anchor), None)
    if anchor_step is not None:
        put(
            anchor_step,
            "root_cause",
            True,
            f"诊断指认的第 {anchor} 步：{diag.evidence}",
        )
    else:
        # 任务级失败常见：锚点落在 final 上
        anchor_step = final
        if anchor_step is not None:
            put(anchor_step, "root_cause", True, f"诊断指认的收尾动作：{diag.evidence}")

    # ── 2. 反向依赖：锚点用的标识符，是前面哪一步给的 ──
    needed: set[str] = set()
    if anchor_step is not None:
        needed |= _args_ids(anchor_step)
        needed |= _ids(anchor_step.content or "")

    frontier: list[Step] = [anchor_step] if anchor_step is not None else []
    consumed: dict[int, set[str]] = {}
    for s in run.steps:
        for tid in _args_ids(s):
            consumed.setdefault(s.index, set()).add(tid)

    seen_ctx: set[int] = set()
    while frontier:
        cur = frontier.pop()
        want = _args_ids(cur) | (_ids(cur.content or "") if cur.kind != "final" else set())
        if not want:
            continue
        for prev in tools:
            if cur.index is not None and prev.index >= cur.index:
                continue
            produced = _step_ids(prev)
            hit = want & produced
            if not hit:
                continue
            if prev.index in nodes and nodes[prev.index].keep:
                continue
            put(
                prev,
                "required_context",
                True,
                f"第 {cur.index} 步用到的 {', '.join(sorted(hit))} 来自这一步的返回，缺了它就无法复现",
            )
            seen_ctx.add(prev.index)
            frontier.append(prev)

    # ── 3. 前向后果：锚点之后被它牵连的步骤 ──
    if anchor_step is not None:
        carried = _step_ids(anchor_step)
        for s in tools + ([final] if final else []):
            if s.index <= anchor_step.index:
                continue
            if s.index in nodes:
                continue
            hit = carried & (_args_ids(s) | _ids(s.content or ""))
            if s is final:
                put(
                    s,
                    "consequence",
                    True,
                    "最终答复是失败的落点，必须保留才能看到「失败长什么样」",
                )
                continue
            if hit:
                put(
                    s,
                    "consequence",
                    True,
                    f"这一步用到了失败步骤产出的 {', '.join(sorted(hit))}，是失败向下游传播的证据",
                )
                carried |= _step_ids(s)
            else:
                put(
                    s,
                    "droppable",
                    False,
                    "在失败发生之后执行，且没有消费失败步骤的任何产出，与本次失败无因果关系",
                )

    # ── 4. 锚点之前剩下的步骤：只有「产出从未被消费」才丢 ──
    consumed_ids: set[str] = set()
    for idx, ids in consumed.items():
        if anchor_step is None or idx > anchor_step.index:
            consumed_ids |= ids
        else:
            other = [i for i in consumed if i != idx and (anchor_step is None or i <= anchor_step.index)]
            if other:
                consumed_ids |= ids
    if anchor_step is not None:
        consumed_ids |= _args_ids(anchor_step)
    for s in run.steps:
        if s.index in nodes:
            continue
        produced = _step_ids(s)
        if produced and produced & consumed_ids and s.kind == "tool_call":
            # 被别处消费了，但不属于支撑链 —— 说明消费它的那步也被丢了，一起丢
            put(s, "droppable", False, "它的返回没有被任何保留步骤使用，去掉不影响因果链")
        elif s.kind == "tool_call":
            put(s, "droppable", False, "探索性查询：返回值从未进入后续任何调用的参数，可安全删除")
        else:
            put(s, "droppable", False, "与失败无因果关联")

    ordered = [nodes[k] for k in sorted(nodes)]
    kept = [n for n in ordered if n.keep]
    total = len(ordered)
    minimal = len(kept)

    # 任务级失败（只问不做 / 没有动作 / 说了却没做）的最小复现会缩到只剩收尾那一步。
    # 这不是算法失灵，而是它们本来就**没有出错的那一步** —— 病在缺一条指令，不在某一步做错。
    # 单独标出来，界面才不会把一个深刻结论显示成一次失败。
    minimal_kind = (
        "terminal_only" if minimal <= 1 or all(n.kind != "tool_call" or n.role == "droppable" for n in kept) else "chain"
    )

    # ── 反事实：改哪一处能让这条链断掉 ──
    missing = _missing_fields(diag.evidence)
    what_if = _WHAT_IF.get(diag.kind, _WHAT_IF["unknown"])
    if missing:
        what_if = f"把 {', '.join(missing)} 补进调用参数后重放"

    case = {
        "case_id": f"M-{run.run_id}",
        "from_run": run.run_id,
        "task_id": run.task_id,
        "kind": diag.kind,
        "label": kind_label(diag.kind),
        "task_prompt": run.task_prompt,
        "expect": run.expected,
        "steps": [
            {
                "i": n.step_index,
                "kind": n.kind,
                "tool": n.tool,
                "args": n.args,
                "ok": n.ok,
                "role": n.role,
            }
            for n in kept
        ],
        "assertion": diag.evidence,
        "verified_by_replay": False,
        "note": (
            "这类失败没有「出错的那一步」，最短复现就是一次不产生任何动作的收尾。"
            "它说明问题在指令缺失，而不是某一步执行出错 —— 修法自然也不同：补规则，不是改工具。"
            if minimal_kind == "terminal_only"
            else ""
        ),
    }

    return {
        "run_id": run.run_id,
        "kind": diag.kind,
        "label": kind_label(diag.kind),
        "method": "structural",
        "verified_by_replay": False,
        "original_steps": total,
        "minimal_steps": minimal,
        "minimal_kind": minimal_kind,
        "reduction": round(1 - minimal / max(1, total), 2),
        "chain": [n.to_dict() for n in ordered],
        "case": case,
        "counterfactual": {
            "at_step": diag.step_index,
            "what_if": what_if,
            "predicted": _predicted_effect(ordered, diag),
            "confidence": "structural",
            "verified": False,
        },
        "caveat": (
            "结构最小化：靠参数与观测之间的标识符依赖推断，成本为零，但没有重跑验证。"
            "要拿到 replay-verified 的用例，需要把这条 case 放回运行器重跑一次。"
        ),
    }


def _predicted_effect(ordered: list[ChainNode], diag: Diagnosis) -> str:
    downstream = [n for n in ordered if n.step_index > diag.step_index and n.keep]
    if not downstream:
        return "该失败发生在收尾动作上，修复后预期直接产出正确动作。"
    return (
        f"预期第 {diag.step_index} 步不再出错，其下游 {len(downstream)} 步连锁失败随之消失。"
        "（这是依赖链推断，未经重放验证）"
    )


def minimize_pattern(runs: list[Run], diags: dict[str, Diagnosis], run_ids: list[str]) -> dict[str, Any] | None:
    """给一个失败模式挑一条最有代表性的轨迹做最小化。

    挑选偏好：能减少最多步数、且归因置信度最高的那条 —— 演示价值最高。
    """
    best: tuple[float, Run, Diagnosis] | None = None
    for rid in run_ids:
        run = next((r for r in runs if r.run_id == rid), None)
        diag = diags.get(rid)
        if run is None or diag is None:
            continue
        score = diag.confidence * 2 + len(run.steps) * 0.1
        if best is None or score > best[0]:
            best = (score, run, diag)
    if best is None:
        return None
    return minimize(best[1], best[2])
