"""失败模式归纳。

这一层要回答的不是「这次为什么错」，而是「这类错一共有多少种、各占几成」。
做法：给每次失败算一个**签名**（性质 + 首个出错工具 + 错误特征），再按签名聚起来。
签名里刻意保留可读文本，这样聚出来的每一类都能被人类看懂，而不是一串向量 id。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .diagnose import Diagnosis
from .trace import Run, kind_label

# 错误特征归一：把具体金额、订单号抹掉，只留骨架。
_NORM = [
    (re.compile(r"[A-Z]{1,3}\d{4,}"), "<订单>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<数值>"),
    (re.compile(r"\s+"), " "),
]

_ADVICE = {
    "bad_argument": "工具调用缺少字段或字段类型不对。建议在工具 description 里直接写出一个完整调用示例，并在 system prompt 里要求「调用前先自检必填字段」。",
    "tool_error": "工具已经明确报错，但模型没有改参数重试。建议明确写「工具返回错误时必须换一种参数再试一次，仍失败则转人工」。",
    "hallucinated_fact": "结论里出现了系统里不存在的订单号/日期。建议把「政策与订单信息必须来自工具返回」写成硬性前置，并禁止模型自行补全编号。",
    "wrong_tool": "该用的工具从头到尾没被调用。建议在 system prompt 中把工具选择条件写成显式规则，而不是靠模型理解。",
    "loop": "同一动作被重复执行且没有引入新信息。建议加入重复检测：连续两次相同调用即强制换策略。",
    "context_lost": "用相同参数重复查询，说明中间结论没有留在上下文里。建议把关键事实即时沉淀成结构化摘要，而不是依赖原始观测留在上下文里。",
    "premature_answer": "在缺少关键依据时直接给了结论。建议对「涉及金额/期限/政策」的问题设定前置门槛：未检索不得作答。",
    "escalation_missed": "应当转人工却自行处理。建议把转人工条件写成可判定的清单（期限、金额争议、运费、用户明示）。",
    "over_escalation": "本可自动完成的动作被甩给了人工。根因通常是规程里只写了「什么情况要转人工」，没写「什么情况可以直接办」。建议补一份可直接执行的白名单条件。",
    "asked_instead_of_acted": "必填信息已经齐全，Agent 却把执行变成了反问。建议明确写「信息齐备时必须直接执行动作，不得以向用户确认代替执行」。",
    "promised_but_not_done": "答复里声称已经办理，但没有任何对应的工具调用。建议在收尾前增加一次自检：凡是声称完成的动作，必须能在本轮调用记录里找到。",
    "no_action": "整条轨迹没有产生任何有效动作。通常不是理解问题，而是缺少「信息足够之后该做什么」的收尾指令。",
    "unknown": "现有规则无法定性。这类样本应当人工看一遍，再决定补哪条规则。",
}


# 可直接粘贴进 system prompt 的规则草稿。
# 为什么单独给一份：_ADVICE 回答「往哪个方向改」，patch 回答「具体加哪句话」。
# 诊断到这一步才算闭环 —— 否则用户看完报告，还是得自己把建议翻译成 prompt。
_PATCH = {
    "bad_argument": "调用任何工具前先核对必填字段；缺任何一项都不发起调用，先补齐或向用户询问。",
    "tool_error": "工具返回错误时，换一组参数重试一次；仍失败则转人工，不得跳过或忽略。",
    "wrong_tool": "退款类请求必须走退款工具；转人工只用于政策明确要求的情形，不得替代执行。",
    "hallucinated_fact": "所有订单号、日期、金额必须来自工具返回，禁止自行补全、推测或改写。",
    "loop": "连续两次以相同参数调用同一工具后必须停止并换策略：换参数、换工具，或转人工。",
    "context_lost": "已经查到的事实直接引用，不得用相同参数重复查询。",
    "premature_answer": "涉及金额、期限、政策的结论，必须先检索政策库；未检索不得给出结论。",
    "escalation_missed": "遇到以下任一情形必须转人工：超出受理期限、金额存在争议、用户明确要求人工。",
    "over_escalation": "以下情形必须直接办理，不得转人工：在受理期限内、金额未超上限、订单归属校验通过。",
    "asked_instead_of_acted": "必填信息齐备时必须直接执行动作；不得以「需要我帮您确认吗」代替执行。",
    "promised_but_not_done": "答复前自检：凡声称「已办理」的动作，必须能在本轮调用记录里找到对应调用；否则改为说明进展。",
    "no_action": "信息足够后必须产生一个动作：执行、转人工、或明确追问，三者之一，不得停在纯文本。",
    "unknown": "",
}


def patch_for(kind: str) -> str:
    return _PATCH.get(kind, "")


def _tokens(text: str) -> str:
    t = text or ""
    for pattern, repl in _NORM:
        t = pattern.sub(repl, t)
    return t.strip()[:80]


# 任务级失败的证据措辞会随工具名变化，但病还是同一种 —— 统一签名，避免把同一类病拆成好几条
_CANON = {
    "over_escalation": "本可直接办理，却转给了人工",
    "asked_instead_of_acted": "信息已齐备，却把执行变成了反问",
    "promised_but_not_done": "声称已办理，但没有任何对应的工具调用",
    "no_action": "任务要求执行动作，但整条轨迹没有产生任何动作",
    "escalation_missed": "应当转人工，却自行处理了",
}


@dataclass
class Pattern:
    pid: str
    kind: str
    label: str
    tool: str
    signature: str
    run_ids: list[str] = field(default_factory=list)
    tasks: set[str] = field(default_factory=set)
    versions: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)
    advice: str = ""

    def to_dict(self, total_failures: int) -> dict[str, Any]:
        return {
            "id": self.pid,
            "kind": self.kind,
            "label": self.label,
            "tool": self.tool,
            "signature": self.signature,
            "count": len(self.run_ids),
            "share": round(len(self.run_ids) / max(1, total_failures), 3),
            "tasks": sorted(self.tasks),
            "versions": self.versions,
            "run_ids": self.run_ids[:12],
            "examples": self.examples[:2],
            "advice": self.advice,
            "patch": patch_for(self.kind),
        }


def _tool_of(run: Run, step_index: int) -> str:
    for s in run.steps:
        if s.index == step_index and s.kind == "tool_call":
            return s.tool or "-"
    return "-"


def _best_evidence(run: Run, diag: Diagnosis) -> str:
    for f in diag.findings:
        if f.step_index == diag.step_index:
            return f.evidence
    return diag.evidence


def cluster(runs: list[Run], diagnoses: dict[str, Diagnosis]) -> list[Pattern]:
    failed = [r for r in runs if not r.success]
    buckets: dict[tuple[str, str, str], Pattern] = {}

    for run in failed:
        diag = diagnoses.get(run.run_id)
        if diag is None:
            continue
        tool = _tool_of(run, diag.step_index) if diag.step_index > 0 else "-"
        canon = _CANON.get(diag.kind)
        if canon:
            key = (diag.kind, "-", canon)
            signature = canon
        else:
            key = (diag.kind, tool, _tokens(_best_evidence(run, diag)))
            signature = _best_evidence(run, diag)
        pat = buckets.get(key)
        if pat is None:
            pat = Pattern(
                pid="",
                kind=diag.kind,
                label=kind_label(diag.kind),
                tool="-" if canon else tool,
                signature=signature,
                advice=_ADVICE.get(diag.kind, _ADVICE["unknown"]),
            )
            buckets[key] = pat
        pat.run_ids.append(run.run_id)
        pat.tasks.add(run.task_id)
        pat.versions[run.prompt_version] = pat.versions.get(run.prompt_version, 0) + 1
        if len(pat.examples) < 2:
            pat.examples.append(_best_evidence(run, diag))

    # 只有 1 条样本的碎片合并成「其他」，避免出现「8 类失败里有 6 类各 1 次」这种自欺欺人的报告
    solid = sorted([p for p in buckets.values() if len(p.run_ids) >= 2], key=lambda p: -len(p.run_ids))
    singles = [p for p in buckets.values() if len(p.run_ids) == 1]

    if len(singles) >= 2:
        merged = Pattern(
            pid="",
            kind="unknown",
            label="零散异常",
            tool="-",
            signature=f"{len(singles)} 种只出现过一次、彼此不相似的失败",
            advice="样本量不足以归纳，暂不构成结论。建议继续采样，不要基于单例下判断。",
        )
        for p in singles:
            merged.run_ids.extend(p.run_ids)
            merged.tasks |= p.tasks
            for v, c in p.versions.items():
                merged.versions[v] = merged.versions.get(v, 0) + c
            if len(merged.examples) < 2:
                merged.examples.append(p.signature)
        solid.append(merged)
    else:
        solid.extend(singles)

    for i, p in enumerate(solid, 1):
        p.pid = f"P{i}"
    return solid


def summarize(runs: list[Run]) -> dict[str, Any]:
    total = len(runs)
    failed = [r for r in runs if not r.success]
    by_version: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "failed": 0})
    for r in runs:
        by_version[r.prompt_version]["total"] += 1
        if not r.success:
            by_version[r.prompt_version]["failed"] += 1
    return {
        "total": total,
        "failed": len(failed),
        "fail_rate": round(len(failed) / max(1, total), 3),
        "by_version": {
            k: {**v, "fail_rate": round(v["failed"] / max(1, v["total"]), 3)}
            for k, v in sorted(by_version.items())
        },
        "models": sorted({r.model for r in runs}),
        "tasks": len({r.task_id for r in runs}),
    }
