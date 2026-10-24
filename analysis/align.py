"""轨迹对齐：把两条执行路径摆在一起，指出它们从哪一步开始走岔。

这是「会诊」区别于「看日志」的地方 —— 日志是线性的，而诊断需要比较。
比较的对象是**决策签名**（工具 + 参数键），故意不含参数值：
值会被最小化改写，签名才是结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .trace import Run, Step


def _sigs(run: Run) -> list[str]:
    return [s.signature for s in run.steps if s.kind in ("tool_call", "final")]


def _steps(run: Run) -> list[Step]:
    return [s for s in run.steps if s.kind in ("tool_call", "final")]


def lcs_pairs(a: list[str], b: list[str]) -> list[tuple[int | None, int | None]]:
    """最长公共子序列对齐；未匹配的位置用 None 表示（即插入/删除）。"""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])

    pairs: list[tuple[int | None, int | None]] = []
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            pairs.append((i, None))
            i += 1
        else:
            pairs.append((None, j))
            j += 1
    while i < n:
        pairs.append((i, None))
        i += 1
    while j < m:
        pairs.append((None, j))
        j += 1
    return pairs


@dataclass
class ForkRow:
    left: dict[str, Any] | None
    right: dict[str, Any] | None
    relation: str  # same | only_left | only_right


def _brief(step: Step) -> dict[str, Any]:
    return {
        "step_index": step.index,
        "kind": step.kind,
        "tool": step.tool,
        "args": step.args,
        "ok": step.ok,
        "error": step.error,
        "content": (step.content or "")[:240],
        "signature": step.signature,
    }


def align_runs(good: Run, bad: Run) -> dict[str, Any]:
    """对齐一条成功轨迹与一条失败轨迹。

    返回的 fork_at 是**第一条不一致的决策**在失败轨迹中的步号；
    如果两条路径结构完全一致却结果不同，fork_at 为 None —— 说明问题不在「做了什么」，
    而在「同样的动作拿到了不同结果」，这时会明确告诉上层不要硬编一个分叉点。
    """
    a, b = _steps(good), _steps(bad)
    pairs = lcs_pairs([s.signature for s in a], [s.signature for s in b])

    rows: list[ForkRow] = []
    for li, ri in pairs:
        left = _brief(a[li]) if li is not None else None
        right = _brief(b[ri]) if ri is not None else None
        relation = "same" if left and right else ("only_left" if left else "only_right")
        rows.append(ForkRow(left=left, right=right, relation=relation))

    fork_at = next(
        (r.right["step_index"] for r in rows if r.relation == "only_right" and r.right),
        None,
    )
    if fork_at is None:
        # 结构相同但结果不同：找出第一步参数不同的位置
        for r in rows:
            if r.left and r.right and r.left["args"] != r.right["args"]:
                fork_at = r.right["step_index"]
                break

    return {
        "task_id": bad.task_id,
        "good_run": good.run_id,
        "bad_run": bad.run_id,
        "fork_at": fork_at,
        "rows": [
            {"left": r.left, "right": r.right, "relation": r.relation} for r in rows
        ],
        "length_good": len(a),
        "length_bad": len(b),
    }


def pick_pair(runs: list[Run], diagnoses: dict[str, Any]) -> tuple[Run, Run] | None:
    """在同一任务里挑一对「结构最像」的成功/失败轨迹，让对比更有说服力。"""
    by_task: dict[str, dict[str, list[Run]]] = {}
    for r in runs:
        bucket = by_task.setdefault(r.task_id, {"ok": [], "bad": []})
        bucket["ok" if r.success else "bad"].append(r)

    best: tuple[Run, Run] | None = None
    best_score = -10**9
    for task_id, bucket in by_task.items():
        if not bucket["ok"] or not bucket["bad"]:
            continue
        for good in bucket["ok"]:
            for bad in bucket["bad"]:
                d = diagnoses.get(bad.run_id)
                # 偏好：失败有明确归因、两条长度接近、失败轨迹里真的调了工具
                score = 0.0
                if d and getattr(d, "kind", "unknown") != "unknown":
                    score += 3
                score -= abs(len(good.steps) - len(bad.steps)) * 0.3
                score += min(len(bad.tool_steps), 5) * 0.2
                if score > best_score:
                    best_score = score
                    best = (good, bad)
    return best
