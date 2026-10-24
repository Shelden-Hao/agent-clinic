"""回归验收：把回归集里钉住的最小复现用例，和新一批轨迹对一遍。

这是 L5 真正闭环的那一步。
只做「最小化 + 存进回归集」是不够的 —— 那只是准备好了考卷；
真正有用的是**改完 prompt 之后，这批考卷还过不过**。

做法刻意保持轻量：不重跑模型、不调外部服务，
只把新轨迹重新过一遍归因规则，然后逐条回答三个问题：

    这条病还在吗？→ present
    它消失了？    → fixed
    变成了另一种病？→ changed   ← 这一项最重要，它对应「修 A 弄坏 B」
    这批里没有对应任务？→ uncovered

第四种情况必须单独列出来，不能算通过。
「没考」和「考过了」是两件事，混在一起就是自欺。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .diagnose import diagnose
from .trace import Run, kind_label

STATUS_LABEL = {
    "fixed": "已修复",
    "partial": "部分修复",
    "present": "仍存在",
    "changed": "变成了别的病",
    "uncovered": "这次没有覆盖",
}


@dataclass
class Verdict:
    case_id: str
    kind: str
    label: str
    status: str
    detail: str
    run_id: str | None = None
    now_kind: str | None = None
    hit: int = 0
    sample: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "status_label": STATUS_LABEL.get(self.status, self.status),
            "detail": self.detail,
            "run_id": self.run_id,
            "now_kind": self.now_kind,
            "now_label": kind_label(self.now_kind) if self.now_kind else None,
            "hit": self.hit,
            "sample": self.sample,
        }


def _index_runs(runs: list[Run]) -> tuple[dict[str, list[Run]], dict[str, list[Run]]]:
    by_task: dict[str, list[Run]] = {}
    by_prompt: dict[str, list[Run]] = {}
    for r in runs:
        by_task.setdefault(r.task_id, []).append(r)
        if r.task_prompt:
            by_prompt.setdefault(r.task_prompt.strip(), []).append(r)
    return by_task, by_prompt


def verify(cases: list[dict], runs: list[Run]) -> dict[str, Any]:
    by_task, by_prompt = _index_runs(runs)
    verdicts: list[Verdict] = []

    for case in cases:
        kind = case.get("kind") or "unknown"
        label = case.get("label") or kind_label(kind)
        case_id = case.get("case_id") or "?"

        # 有 task_id 就只按 task_id 对，绝不退回按 prompt 模糊匹配。
        # prompt 撞了不代表同一个任务 —— 用模糊匹配会把「没考到」伪装成「考过了」。
        if case.get("task_id"):
            candidates = by_task.get(case["task_id"], [])
        else:
            candidates = by_prompt.get((case.get("task_prompt") or "").strip(), [])
        if not candidates:
            verdicts.append(
                Verdict(
                    case_id=case_id,
                    kind=kind,
                    label=label,
                    status="uncovered",
                    detail="这批轨迹里没有同一任务，这条用例没有被考到",
                )
            )
            continue

        # 同一任务下的每一条都要看：只挑一条会把「时好时坏」掩盖成「修好了」或「还坏着」
        expect = case.get("expect") or None
        hits: list[Run] = []
        misses: list[tuple[Run, str]] = []
        for run in candidates:
            diag = diagnose(run, expect or run.expected or None)
            if diag.kind == kind:
                hits.append(run)
            else:
                misses.append((run, diag.kind))

        n = len(candidates)
        hit = len(hits)
        sample_run = (hits or [m[0] for m in misses])[0]

        if hit == n:
            verdicts.append(
                Verdict(
                    case_id=case_id,
                    kind=kind,
                    label=label,
                    status="present",
                    detail=f"{n} 条同任务轨迹全部仍被判成「{label}」，这条病没有被修掉",
                    run_id=sample_run.run_id,
                    now_kind=kind,
                    hit=hit,
                    sample=n,
                )
            )
        elif hit > 0:
            verdicts.append(
                Verdict(
                    case_id=case_id,
                    kind=kind,
                    label=label,
                    status="partial",
                    detail=(
                        f"{n} 条同任务轨迹里 {hit} 条仍被判成「{label}」，其余不再命中 —— "
                        "这是不稳定，不是修好了"
                    ),
                    run_id=hits[0].run_id,
                    now_kind=kind,
                    hit=hit,
                    sample=n,
                )
            )
        else:
            other_kinds = sorted({k for _, k in misses})
            clean = all(r.success and r.success_known for r, _ in misses)
            if clean:
                verdicts.append(
                    Verdict(
                        case_id=case_id,
                        kind=kind,
                        label=label,
                        status="fixed",
                        detail=f"{n} 条同任务轨迹全部成功，且都不再命中「{label}」",
                        run_id=sample_run.run_id,
                        now_kind="unknown",
                        hit=0,
                        sample=n,
                    )
                )
            else:
                now = kind_label(other_kinds[0]) if other_kinds else "未判定"
                verdicts.append(
                    Verdict(
                        case_id=case_id,
                        kind=kind,
                        label=label,
                        status="changed",
                        detail=(
                            f"「{label}」确实不见了，但也不是干净通过 —— "
                            f"现在的问题是「{now}」"
                        ),
                        run_id=sample_run.run_id,
                        now_kind=other_kinds[0] if other_kinds else None,
                        hit=0,
                        sample=n,
                    )
                )

    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v.status] = counts.get(v.status, 0) + 1

    covered = len(verdicts) - counts.get("uncovered", 0)
    return {
        "total": len(verdicts),
        "counts": counts,
        "covered": covered,
        "verdicts": [v.to_dict() for v in verdicts],
        "summary": _summary(counts, covered),
    }


def _summary(counts: dict[str, int], covered: int) -> str:
    if not covered:
        return "这批轨迹没有覆盖任何一条回归用例，无法判断修复效果。"
    fixed = counts.get("fixed", 0)
    present = counts.get("present", 0)
    partial = counts.get("partial", 0)
    changed = counts.get("changed", 0)

    parts = []
    if fixed:
        parts.append(f"{fixed} 条修好")
    if present:
        parts.append(f"{present} 条仍在")
    if partial:
        parts.append(f"{partial} 条时好时坏")
    if changed:
        parts.append(f"{changed} 条变成了别的病")
    head = f"{covered} 条被考到：" + "，".join(parts) + "。"

    if changed:
        return head + f"改完之后要重点看这 {changed} 条 —— 它们是从一个坑被推到了另一个坑。"
    if partial:
        return head + "时好时坏不算修好，它只是把一个确定的问题变成了一个概率问题。"
    if present:
        return head + "修复没有真正生效。"
    return head + "这是可以拿去说「这次真的改对了」的证据。"
