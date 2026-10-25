"""把采集台导出的会话 dump 并入 data/sessions.json。

为什么不直接把 dump 拷成 data/sessions.json：dump 只有 `sessions`，没有 `meta`；
而且采集是分批跑的（先采 v1 再采 v2、先跑 2 次再补跑 2 次），
直接覆盖会把上一批悄悄丢掉。这里按 `session_id` 合并，重复的以后来的为准。

用法：
    python tools/merge_sessions.py runtime/sessions-v2.json
    python tools/merge_sessions.py runtime/*.json --out data/sessions.json

合并后按「场景 + 版本 + 重跑序号」排序，让界面上的顺序稳定 ——
不然每次合并顺序不同，截图和 diff 都会无谓地变。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "data" / "sessions.json"

# 会话 id 形如 S01-v1-2：场景-版本-第几次重跑
_ID = re.compile(r"^(?P<sc>[A-Za-z0-9]+)-(?P<ver>v\d+)-(?P<run>\d+)$")


def sort_key(s: dict) -> tuple:
    m = _ID.match(str(s.get("session_id", "")))
    if not m:
        return (str(s.get("scenario_id", "")), str(s.get("prompt_version", "")), 0)
    return (m["sc"], m["ver"], int(m["run"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dumps", nargs="+", help="采集台导出的 JSON 文件")
    ap.add_argument("--out", default=str(TARGET))
    args = ap.parse_args()

    out_path = Path(args.out)
    merged: dict[str, dict] = {}

    if out_path.exists():
        old = json.loads(out_path.read_text(encoding="utf-8"))
        for s in old.get("sessions", []):
            merged[s["session_id"]] = s
        print(f"已有 {len(merged)} 场（来自 {out_path.name}）")

    added = 0
    for p in args.dumps:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        batch = raw.get("sessions", raw if isinstance(raw, list) else [])
        for s in batch:
            sid = s.get("session_id")
            if not sid:
                continue
            if sid not in merged:
                added += 1
            merged[sid] = s

    sessions = sorted(merged.values(), key=sort_key)
    turns = sum(len(s.get("turns", [])) for s in sessions)
    failed = sum(1 for s in sessions for t in s.get("turns", []) if not t.get("success"))

    payload = {
        "meta": {
            "note": "多轮会话快照（浏览器端真实采集，各轮共享世界状态与对话历史）",
            "count": len(sessions),
            "turns": turns,
            "failed_turns": failed,
            "scenarios": sorted({s.get("scenario_id") for s in sessions if s.get("scenario_id")}),
            "versions": sorted({s.get("prompt_version") for s in sessions if s.get("prompt_version")}),
        },
        "sessions": sessions,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"✓ {len(sessions)} 场 / {turns} 轮 / 失败轮 {failed}（新增 {added}）→ {out_path}")


if __name__ == "__main__":
    main()
