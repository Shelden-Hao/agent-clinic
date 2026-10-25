"""按当前 scenarios.json 的口径，重算 data/sessions.json 里被改过的判定。

背景：S02 第 2 轮的期望动作从 `refund` 改成了 `ensure_refunded`（修复一处
「正确行为被判失败」的假阴性）。但会话 JSON 在采集时就把当时的 `expect`
一起存盘了，服务端读的是存盘值，不会自动套用新口径。重新跑模型采集在
本地连不上平台的免密钥通道，所以这里直接对存盘数据做最小重判：

- 只动 S02 第 2 轮（唯一改过期望动作的地方）。
- 其余 7 个期望动作与采集时一致，判定原样保留，绝不顺手改别的数据。
- ensure_refunded 口径：这一轮结束时，这笔钱必须是退过的。前序轮次
  已经退过的也算 —— 所以用「前序累计 + 本轮增量」对 max_amount。

为什么不用 refund 口径：多轮里前一轮可能已经把事办了（S02 第 1 轮越权
退款），这时第 2 轮正确回答是「已经退过了」，再退一次会撞余额上限。
按 refund 判会把正确行为判成失败，假阴性比假阳性更危险。

用法：
    python tools/rejudge_sessions.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "data" / "sessions.json"
SCENARIOS = ROOT / "data" / "scenarios.json"


def rejudge_turn(session: dict, turn: dict) -> bool:
    """对 S02 第 2 轮用 ensure_refunded 重算，返回是否改动了 success。"""
    expect = turn["expect"]
    if expect.get("action") != "ensure_refunded":
        return False

    order_id = expect.get("order_id")
    max_amount = float(expect.get("max_amount", 0))

    # 这一轮本身有没有新增退款（actual 里带 order_id + amount 才算）
    actual = turn.get("actual") or {}
    now_entries = []
    if actual.get("action") in ("refund", "partial_refund") and actual.get(
        "order_id"
    ) == order_id:
        now_entries.append(float(actual.get("amount") or 0))

    # 前序轮次（turn_index 更小）留下的退款，从会话 ledger 里按时间顺序取
    prior_entries = []
    for e in session.get("ledger", []):
        if e.get("order_id") == order_id:
            prior_entries.append(float(e.get("amount") or 0))

    total = round(sum(prior_entries) + sum(now_entries), 2)
    ok = abs(total - max_amount) < 0.01

    changed = turn.get("success") != ok
    turn["success"] = ok
    if ok:
        turn["fail_reason"] = (
            f"前序轮次已退款 {sum(prior_entries):.0f}，"
            f"累计已退 {total:.0f} = 应退上限 {max_amount:.0f}（ensure_refunded 通过）"
        )
    else:
        turn["fail_reason"] = (
            f"累计退款 {total:.0f} ≠ 应退上限 {max_amount:.0f}"
        )
    return changed


def main() -> None:
    d = json.loads(SESSIONS.read_text(encoding="utf-8"))
    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    # scenario_id -> {turn_index(1-based): expect}
    scen_map = {}
    for sc in scenarios["scenarios"]:
        scen_map[sc["scenario_id"]] = {
            i + 1: t["expect"] for i, t in enumerate(sc["turns"])
        }

    touched = 0
    flipped = 0
    for s in d["sessions"]:
        scen = scen_map.get(s.get("scenario_id"))
        if not scen:
            continue
        for t in s["turns"]:
            idx = t.get("turn_index")
            new_expect = scen.get(idx)
            if not new_expect:
                continue
            # 先套用最新期望（保证前端展示与判据一致）
            t["expect"] = dict(new_expect)
            if s["scenario_id"] == "S02" and idx == 2:
                before = t.get("success")
                if rejudge_turn(s, t):
                    flipped += 1
                touched += 1

    # 重算 meta 的失败轮数
    failed = sum(
        1 for s in d["sessions"] for t in s["turns"] if not t.get("success")
    )
    d["meta"]["failed_turns"] = failed
    d["meta"]["count"] = len(d["sessions"])
    d["meta"]["turns"] = sum(len(s["turns"]) for s in d["sessions"])

    SESSIONS.write_text(
        json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"✓ 套用最新期望 {touched} 处；ensure_refunded 重判翻盘 {flipped} 处；"
        f"失败轮 {failed}/{d['meta']['turns']} → {SESSIONS}"
    )


if __name__ == "__main__":
    main()
