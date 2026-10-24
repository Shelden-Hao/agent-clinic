"""实验对比：把「同一批任务 × 多个 prompt 版本」摊成一张矩阵。

## 为什么需要它

现有的版本对比只给一行数字：v1 失败率 62.5%，v2 50%。这行数字看起来像好消息，
但它把一个致命事实整个盖住了 —— **同一批任务里，有的被修好了，有的被弄坏了，
还有的从「稳定失败」变成了「一半概率失败」**。这三件事在总分里长一个样。

所以这里做三件多数 trace 工具不做的事：

1. **按任务拆开对比**，而不是按版本汇总。
2. **把「通过几次 / 共几次」原样保留**，不做成 0/1。一个任务跑两次过一次不过，
   和跑两次都失败，失败率的差别只有 0.5，但性质完全不同。
3. **单独统计稳定性**：稳定通过、稳定失败、抛硬币。抛硬币任务变多，
   说明系统的不确定性上升 —— 这是失败率降下来也换不回来的东西。

## 判定口径

判定只看「通过率怎么变」，不看变化幅度是否「显著」。样本只有每格 2 条，
做显著性检验是自欺欺人。所以标签描述的是**现象**，不是因果结论：

- `fixed`          从未全通过 → 现在全通过（唯一能说「修好了」的情形）
- `regressed`      从全通过 → 现在不是全通过（弄坏了，无论坏到什么程度）
- `flaky_improved` 通过率上升，但仍不是全通过（变好了，但不稳）
- `worse`          通过率下降，但基线本来就不完美
- `flaky`          前后通过率相同且都不完美（一直是抛硬币）
- `stable_ok`      两版都全通过
- `stable_bad`     两版都全不通过
- `missing`        某一侧没有数据，不做判定
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .trace import Run, kind_label

VERDICT_LABEL = {
    "fixed": "修好了",
    "regressed": "弄坏了",
    "flaky_improved": "变好了，但不稳",
    "worse": "更糟了",
    "flaky": "一直是抛硬币",
    "stable_ok": "本来就稳",
    "stable_bad": "一直坏着",
    "missing": "缺对照",
}

# 判定严重程度。前端用它决定排序和配色：负值越靠前，越该先看。
_SEVERITY = {
    "regressed": -3,
    "worse": -2,
    "flaky_improved": -1,
    "stable_bad": 0,
    "flaky": 0,
    "fixed": 1,
    "stable_ok": 2,
    "missing": -9,
}


def _verdict(a_ok: int, a_n: int, b_ok: int, b_n: int) -> str:
    """比对同一个任务在两个版本下的通过率。

    刻意用整数比值而不是浮点比较：2/2 和 1/1 是同一个事实（全通过），
    浮点相等判断在这里只会引入无意义的边界问题。
    """
    if a_n == 0 or b_n == 0:
        return "missing"
    a_all, b_all = a_ok == a_n, b_ok == b_n
    a_none, b_none = a_ok == 0, b_ok == 0
    if a_all and b_all:
        return "stable_ok"
    if a_none and b_none:
        return "stable_bad"
    if b_all:
        # 基线不全通过，现在全通过 —— 这是唯一能叫「修好」的情形
        return "fixed"
    if a_all:
        # 基线全通过，现在不全通过 —— 弄坏了。哪怕只坏了一条也是坏。
        return "regressed"
    # 两侧都不完美，按通过率方向分
    a_rate, b_rate = a_ok / a_n, b_ok / b_n
    if b_rate > a_rate:
        return "flaky_improved"
    if b_rate < a_rate:
        return "worse"
    return "flaky"


def _stability(cells: dict[str, dict[str, list[Run]]]) -> dict[str, dict[str, int]]:
    """单版本内的稳定性分布。

    这一项只看一个版本自己，不跨版本比较：
    一个任务在同一份 prompt、同一个任务下跑两次，结果却不同，
    说明它对这个系统是掷骰子 —— 这种任务会稀释失败率，也会让逐条归因失去代表性。
    """
    out: dict[str, dict[str, int]] = {}
    versions = sorted({v for per_task in cells.values() for v in per_task})
    for v in versions:
        row = {"stable_ok": 0, "stable_bad": 0, "flaky": 0}
        for per_version in cells.values():
            runs = per_version.get(v) or []
            if not runs:
                continue
            ok = sum(1 for r in runs if r.success)
            if ok == len(runs):
                row["stable_ok"] += 1
            elif ok == 0:
                row["stable_bad"] += 1
            else:
                row["flaky"] += 1
        out[v] = row
    return out


def _kinds_of(runs: list[Run], diagnoses: dict[str, Any]) -> list[str]:
    """这一格出现过哪些病，按次数降序。给矩阵单元格用。"""
    counter: dict[str, int] = defaultdict(int)
    for r in runs:
        if r.success:
            continue
        d = diagnoses.get(r.run_id)
        counter[d.kind if d else "unknown"] += 1
    return [k for k, _ in sorted(counter.items(), key=lambda x: -x[1])]


def _migration(rows: list[dict]) -> dict:
    """病种迁移：两个版本都失败、但死因换了的任务。

    这是最容易骗人的一种「改善」。一个任务从「只问不做」变成「说了却没做」，
    失败率一模一样，甚至步数还少了 —— 但性质变差了：
    前者用户能立刻发现没办成，后者用户会以为办成了，直到过几天才发现。

    所以这里单独把「换了死法」的任务拎出来。它不算修好，也不算弄坏，
    它算**你以为你修好了**。
    """
    changed = []
    inflow: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        a = r["from_kinds"]
        b = r["to_kinds"]
        if not a or not b:
            continue
        if a[0] == b[0]:
            continue
        changed.append(
            {
                "task_id": r["task_id"],
                "from": a[0],
                "to": b[0],
                # 顺手把中文带上，免得前端还要自己查表
                "from_label": kind_label(a[0]),
                "to_label": kind_label(b[0]),
            }
        )
        inflow[b[0]].append(r["task_id"])

    top = None
    if inflow:
        kind, tasks = max(inflow.items(), key=lambda x: len(x[1]))
        if len(tasks) >= 2:
            top = {
                "kind": kind,
                "label": kind_label(kind),
                "tasks": sorted(tasks),
                "count": len(tasks),
            }

    note = ""
    if changed:
        note = (
            f"{len(changed)} 个任务在两版之间换了死法（{'、'.join(c['task_id'] for c in changed)}）。"
            "失败率看不见这件事：它们始终是失败，只是失败的理由变了。"
        )
        if top:
            note += (
                f"其中 {top['count']} 个的新死因都是「{top['label']}」—— "
                "这不是修复，这是把失败换成了更难被发现的那一种。"
            )
    return {
        "changed": len(changed),
        "items": changed,
        "inflow": [
            {"kind": k, "label": kind_label(k), "tasks": sorted(v), "count": len(v)}
            for k, v in sorted(inflow.items(), key=lambda x: -len(x[1]))
        ],
        "top": top,
        "note": note,
    }


def _narrative(
    rows: list[dict],
    version_rows: list[dict],
    stability: dict[str, dict[str, int]],
    migration: dict,
) -> dict:
    """把矩阵翻译成人话结论。

    这是这个模块存在的主要理由：矩阵本身谁都会画，但从矩阵里读出
    「失败率下降了，可其中有多少是真修好的」需要有人替你说出来。
    """
    base, cand = (version_rows[0], version_rows[-1]) if len(version_rows) >= 2 else (None, None)
    by = defaultdict(list)
    for r in rows:
        by[r["verdict"]].append(r["task_id"])

    fixed = by.get("fixed", [])
    regressed = by.get("regressed", [])
    fake = by.get("flaky_improved", [])

    delta_pt = None
    if base and cand:
        delta_pt = round((cand["fail_rate"] - base["fail_rate"]) * 100, 1)

    # 稳定性对比：抛硬币任务数的变化，是最容易被总分掩盖的那一项
    vb = stability.get(base["version"]) if base else None
    vc = stability.get(cand["version"]) if cand else None
    flaky_delta = (vc["flaky"] - vb["flaky"]) if (vb and vc) else None
    bad_delta = (vc["stable_bad"] - vb["stable_bad"]) if (vb and vc) else None

    if not (base and cand):
        head = "只有一个版本，无法对比。"
    elif not fixed and not regressed and not fake:
        head = "两个版本的逐任务结果完全一致，这次改动在任务层面没有产生任何可观测的变化。"
    else:
        parts = []
        if fixed:
            parts.append(f"真正修好了 {len(fixed)} 个任务（{'、'.join(fixed)}）")
        if fake:
            parts.append(
                f"把 {len(fake)} 个从「每次都失败」改成了「一半概率失败」（{'、'.join(fake)}）"
            )
        if regressed:
            parts.append(f"弄坏了 {len(regressed)} 个本来稳定通过的任务（{'、'.join(regressed)}）")
        head = "；".join(parts) + "。"

    # 第二句：把「失败率」这个数字拆开，说清它到底由什么组成
    detail = ""
    if base and cand and delta_pt is not None:
        if delta_pt < 0:
            detail = (
                f"失败率从 {base['fail_rate']:.1%} 降到 {cand['fail_rate']:.1%}，"
                f"表面上是 {abs(delta_pt):.1f} 个百分点的改善。"
            )
        elif delta_pt > 0:
            detail = (
                f"失败率从 {base['fail_rate']:.1%} 升到 {cand['fail_rate']:.1%}，"
                f"整体是 {abs(delta_pt):.1f} 个百分点的退化。"
            )
        else:
            detail = f"失败率没有变化（{base['fail_rate']:.1%}），但内部构成可能已经换了一轮。"
        if fake:
            detail += (
                f"这 {abs(delta_pt):.1f} 个点里，有 {len(fake)} 个任务并没有被修好 —— "
                "它们只是从「确定失败」变成了「随机失败」。确定的问题可以排期修，"
                "随机的问题没法排期，只能被反复重新发现。"
            )

    # 第三句：稳定性
    stability_note = ""
    if vb and vc:
        if flaky_delta is not None and flaky_delta > 0:
            stability_note = (
                f"抛硬币任务从 {vb['flaky']} 个增加到 {vc['flaky']} 个（{flaky_delta:+d}）。"
                f"失败率降了，不确定性反而涨了 —— 这两个数字应该一起看。"
            )
        elif flaky_delta is not None and flaky_delta < 0:
            stability_note = (
                f"抛硬币任务从 {vb['flaky']} 个减少到 {vc['flaky']} 个（{flaky_delta:+d}），"
                "系统变得更可预测了，这比失败率本身更能说明质量在好转。"
            )
        else:
            stability_note = (
                f"抛硬币任务维持在 {vc['flaky']} 个。失败率的变化没有以稳定性为代价。"
            )

    return {
        "headline": head,
        "detail": detail,
        "stability": stability_note,
        "migration": migration.get("note", ""),
        "counts": {k: len(v) for k, v in by.items()},
        "delta_pt": delta_pt,
        "flaky_delta": flaky_delta,
        "stable_bad_delta": bad_delta,
    }


def compare(runs: list[Run], diagnoses: dict[str, Any], version_rows: list[dict]) -> dict:
    """主入口。

    `version_rows` 由 build_report 传入（已算好的逐版本汇总），
    这样版本指标只有一处口径，不会出现两个地方算出两个失败率。
    """
    if not runs:
        return {"ready": False, "reason": "没有轨迹"}

    versions = [v["version"] for v in version_rows] or sorted({r.prompt_version for r in runs})
    base_v, cand_v = versions[0], (versions[-1] if len(versions) > 1 else None)

    # task -> version -> runs
    cells: dict[str, dict[str, list[Run]]] = defaultdict(lambda: defaultdict(list))
    for r in runs:
        cells[r.task_id][r.prompt_version].append(r)

    rows: list[dict] = []
    for task_id in sorted(cells):
        per_version = cells[task_id]
        a = per_version.get(base_v) or []
        b = per_version.get(cand_v or base_v) or []
        a_ok = sum(1 for r in a if r.success)
        b_ok = sum(1 for r in b if r.success)
        verdict = _verdict(a_ok, len(a), b_ok, len(b)) if cand_v else "missing"

        # 每个版本格里，失败轨迹被判成了哪几种病 —— 让人看见「同一个任务，两版死的姿势不一样」
        per_cell = {}
        for v in versions:
            vr = per_version.get(v) or []
            ks = _kinds_of(vr, diagnoses)
            per_cell[v] = {
                "ok": sum(1 for r in vr if r.success),
                "total": len(vr),
                "kinds": ks,
                # 界面上要显示病名本身，而不是 over_escalation 这种内部 key
                "kind_labels": [kind_label(k) for k in ks],
                "run_ids": [r.run_id for r in vr],
            }

        rows.append(
            {
                "task_id": task_id,
                "prompt": (a or b or [None])[0].task_prompt if (a or b) else "",
                "cells": per_cell,
                "from_kinds": per_cell.get(base_v, {}).get("kinds") or [],
                "to_kinds": per_cell.get(cand_v or base_v, {}).get("kinds") or [],
                "from_labels": per_cell.get(base_v, {}).get("kind_labels") or [],
                "to_labels": per_cell.get(cand_v or base_v, {}).get("kind_labels") or [],
                "verdict": verdict,
                "verdict_label": VERDICT_LABEL[verdict],
                "severity": _SEVERITY[verdict],
                "fragile": any(
                    (c["total"] > 0 and 0 < c["ok"] < c["total"]) for c in per_cell.values()
                ),
            }
        )

    # 退化项排在前面：读者最该先看到的是「你这次改坏了什么」
    rows.sort(key=lambda r: (r["severity"], r["task_id"]))

    stability = _stability(cells)
    migration = _migration(rows)

    return {
        "ready": True,
        "baseline": base_v,
        "candidate": cand_v,
        "versions": versions,
        "version_rows": version_rows,
        "rows": rows,
        "stability": stability,
        "migration": migration,
        "narrative": _narrative(rows, version_rows, stability, migration),
        # 一句固定的口径声明，界面上会显示出来
        "caveat": (
            "每格只有很少的样本（本项目为每任务每版本 2 条）。"
            "这里给的是现象标签，不是统计显著性结论 —— 你该拿它去决定「先查哪个任务」，"
            "而不是拿它去证明「某个改动有效」。"
        ),
    }


def kinds_in_row(row: dict, version: str) -> str:
    """给前端一个可直接显示的病名串。"""
    kinds = (row.get("cells", {}).get(version) or {}).get("kinds") or []
    return " / ".join(kind_label(k) for k in kinds) if kinds else "—"
