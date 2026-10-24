"""人工复核队列：机器判不准的那几条，不直接进结论，先进队列等人签字。

## 为什么必须做这个

规则式归因绕不过一个问题：它给出的是**证据强度**，不是**正确概率**。
一条置信度 0.70 的诊断和一条 0.85 的诊断，在报告里长得一模一样 ——
都是一个病名、一句证据、一个步号。但前者很可能只是证据刚够写下来而已。

多数工具的处理方式是：不处理。所有诊断一律用同样的语气呈现，让人自己去分辨
哪条可信。演示时零成本，真要据它改 prompt 时就是灾难 —— 你会照着一条猜出来的
结论去改线上系统。

所以这里做三件事：

1. **主动挑出可疑项**，而不是等用户点开列表去发现。每一条都写明「为什么可疑」。
2. 人可以确认、改判、驳回。**三个动作的分量不同**，统计时分开计数，不揉成一个
   「已处理 6 条」。
3. 报告里「机器判定」和「人工确认」分开计，不混成一个更好看的数字。

这也是「会诊室」这个隐喻唯一说得通的做法：主治医师出报告，主任签字才算数。
没签字的报告不是废纸，但它不是结论。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .trace import Run, kind_label

# 进队列的信号。每个信号都对应一个具体的怀疑理由，不是「感觉不太准」。
SIGNAL_LABEL = {
    "low_confidence": "规则证据刚够，置信度贴下限",
    "unclassified": "规则没认出来，只能记未归类",
    "unstable_pair": "同任务同版本跑两次结果不同，这条代表不了该任务",
    "expectation_missing": "缺任务期望，只能判工具层",
}

# 附加线索的文案模板。带具体病名，因为「另一版死因不同」这句话本身没有信息量，
# 有用的是「另一版死因是『过度转人工』」。
HINT_LABEL = {
    "cross_version": "同一任务在别的版本上主要死因不同：{kind}",
}

# 置信度在这个值以下的诊断默认进队列。
# 0.70 是 no_action 那类「什么都没做」的分数 —— 证据只有「没有工具调用」这一条，
# 它既可能是模型偷懒，也可能是工具压根没注册。规则分不开，人一眼能分开。
CONFIDENCE_FLOOR = 0.70

ACTION_LABEL = {
    "confirm": "确认机器判定",
    "override": "改判为另一种病",
    "dismiss": "判为不算失败",
}

VALID_ACTIONS = set(ACTION_LABEL)


def _signals(
    run: Run,
    diag: Any,
    group: list[Run],
    others: dict[str, str],
) -> tuple[list[str], list[str]]:
    """算出这条诊断为什么可疑。

    分两档返回：
    - `signals`：这条诊断本身不可靠 → 必须进队列
    - `hints`：结论可能没错，但周围证据有矛盾 → 复核时该顺手看一眼

    分开的理由很简单：如果所有「有点意思」的疑点都触发进队，
    27 条失败里会有 17 条要人签字，队列就退化成「把列表又念了一遍」。
    筛不动的队列等于没有队列。
    """
    sig: list[str] = []
    hints: list[str] = []
    if diag is None:
        return sig, hints

    if getattr(diag, "kind", None) == "unknown":
        sig.append("unclassified")
    elif diag.confidence <= CONFIDENCE_FLOOR:
        sig.append("low_confidence")

    # group 含自己：同任务同版本下有成有败，说明这条轨迹代表不了该任务
    if len(group) > 1 and len({r.success for r in group}) > 1:
        sig.append("unstable_pair")

    if getattr(run, "expectation_missing", False):
        sig.append("expectation_missing")

    # 跨版本主死因不同：本身不算疑点（改了 prompt 本来就该换病），
    # 但对复核的人是有用背景 —— 他知道这条不是孤例。
    mine = getattr(diag, "kind", None)
    diff = {v: k for v, k in others.items() if k and k != mine}
    for _v, k in sorted(diff.items()):
        hints.append(HINT_LABEL["cross_version"].format(kind=f"「{kind_label(k)}」"))
        break

    return sig, hints


def build_queue(
    runs: list[Run],
    diagnoses: dict[str, Any],
    reviews: dict[str, dict] | None = None,
) -> dict:
    """组装复核队列 + 复核前后对照。

    `reviews` 是已落盘的复核记录（run_id -> 记录）。传空表示全部待复核。
    """
    reviews = reviews or {}

    by_task_version: dict[tuple[str, str], list[Run]] = defaultdict(list)
    for r in runs:
        by_task_version[(r.task_id, r.prompt_version)].append(r)

    # 每个「任务 × 版本」的主死因（出现次数最多的那种病）。用来给复核的人交代背景：
    # 这一条是不是孤例，还是这个任务在另一个版本上死得更狠。
    primary: dict[tuple[str, str], str] = {}
    for key, group in by_task_version.items():
        counter: dict[str, int] = defaultdict(int)
        for r in group:
            d = diagnoses.get(r.run_id)
            if d is not None and not r.success:
                counter[d.kind] += 1
        if counter:
            primary[key] = max(counter.items(), key=lambda x: (x[1], x[0]))[0]

    items: list[dict] = []
    for r in runs:
        if r.success:
            continue
        d = diagnoses.get(r.run_id)
        if d is None:
            continue
        group = by_task_version[(r.task_id, r.prompt_version)]
        others = {
            v: primary.get((r.task_id, v))
            for v in {vv for (tt, vv) in primary if tt == r.task_id}
            if v != r.prompt_version
        }
        sig, hints = _signals(r, d, group, others)
        if not sig:
            continue
        peers = [p for p in group if p.run_id != r.run_id]

        rec = reviews.get(r.run_id)
        items.append(
            {
                "run_id": r.run_id,
                "task_id": r.task_id,
                "version": r.prompt_version,
                "task_prompt": r.task_prompt,
                "steps": len(r.tool_steps),
                "machine": {
                    "kind": d.kind,
                    "label": kind_label(d.kind),
                    "step_index": d.step_index,
                    "evidence": d.evidence,
                    "confidence": round(d.confidence, 2),
                },
                "signals": sig,
                "signal_labels": [SIGNAL_LABEL[s] for s in sig],
                "hints": hints,
                # 同任务同版本的其他轨迹，供人对照 —— 单看一条会误判。
                # 连它们的判定一起带出来，因为「另一条被判成了什么」比「另一条失败了」有用。
                "peers": [
                    {
                        "run_id": p.run_id,
                        "success": p.success,
                        "steps": len(p.tool_steps),
                        "kind": (diagnoses.get(p.run_id).kind if diagnoses.get(p.run_id) else None),
                        "label": (
                            kind_label(diagnoses[p.run_id].kind)
                            if p.run_id in diagnoses
                            else ("成功" if p.success else None)
                        ),
                    }
                    for p in sorted(peers, key=lambda x: x.run_id)
                ],
                "review": rec,
                "status": "resolved" if rec else "pending",
            }
        )

    # 可疑信号越多越靠前；同分时置信度低的靠前；再同就按 id 稳定排序
    items.sort(key=lambda i: (-len(i["signals"]), i["machine"]["confidence"], i["run_id"]))

    # ── 复核前后对照 ──
    # 前：机器判定的病种分布（只统计失败轨迹）
    before: dict[str, int] = defaultdict(int)
    for r in runs:
        d = diagnoses.get(r.run_id)
        if d is not None and not r.success:
            before[d.kind] += 1
    # 后：把已复核的动作应用上去
    after = dict(before)
    failed_total = sum(1 for r in runs if not r.success)
    after_failed = failed_total
    for item in items:
        rec = item["review"]
        if not rec:
            continue
        act = rec.get("action")
        old = rec.get("original_kind") or item["machine"]["kind"]
        if act == "override":
            new = rec.get("kind")
            if not new:
                continue
            after[old] = after.get(old, 0) - 1
            after[new] = after.get(new, 0) + 1
        elif act == "dismiss":
            # 判为不算失败：从失败里摘出去。诊断分布和失败总数都要减。
            after[old] = after.get(old, 0) - 1
            after_failed -= 1
    for k in list(after):
        if after[k] <= 0:
            after.pop(k)

    def _rows(counter: dict[str, int]) -> list[dict]:
        return [
            {"kind": k, "label": kind_label(k), "count": v}
            for k, v in sorted(counter.items(), key=lambda x: (-x[1], x[0]))
        ]

    pending = sum(1 for i in items if i["status"] == "pending")
    counts = defaultdict(int)
    for i in items:
        if i["review"]:
            counts[i["review"].get("action") or "unknown"] += 1

    return {
        "ready": True,
        "total_failed": failed_total,
        "queued": len(items),
        "pending": pending,
        "resolved": len(items) - pending,
        "actions": {a: counts.get(a, 0) for a in ACTION_LABEL},
        "actions_label": ACTION_LABEL,
        "signal_label": SIGNAL_LABEL,
        "hint_label": HINT_LABEL,
        "confidence_floor": CONFIDENCE_FLOOR,
        "items": items,
        "before": _rows(before),
        "after": _rows(after),
        "after_failed": after_failed,
        "changed": _rows(after) != _rows(before) or after_failed != failed_total,
        "caveat": (
            "复核只改诊断口径，不改轨迹本身。驳回一条诊断不会让那次失败消失 —— "
            "它只是把「这是一次归因失败」和「这是一次任务失败」分开记。"
        ),
    }


def verify_review(record: dict, allowed_kinds: set[str]) -> dict:
    """校验一条复核记录。宁可拒绝，也不要存一条读不懂的记录。"""
    action = record.get("action")
    if action not in VALID_ACTIONS:
        raise ValueError(f"动作必须是 {'/'.join(sorted(VALID_ACTIONS))} 之一")
    note = (record.get("note") or "").strip()
    kind = record.get("kind")
    if action == "override":
        if not kind or kind not in allowed_kinds:
            raise ValueError("改判必须给出一个合法的病种")
        if not note:
            # 改判是唯一会改写结论的动作，必须留下理由
            raise ValueError("改判必须写一句理由 —— 否则三个月后没人知道当初为什么改")
    return {
        "action": action,
        "kind": kind if action == "override" else None,
        "note": note,
        "reviewer": (record.get("reviewer") or "值班医师").strip()[:24],
        "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
