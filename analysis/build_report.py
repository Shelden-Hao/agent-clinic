"""把采集回来的原始轨迹，编译成一份可被前端直接渲染的诊断报告。

用法：
    python analysis/build_report.py            # 读 data/corpus.json
    python analysis/build_report.py --check    # 只打印摘要，不写文件

产物：data/report.json
设计约束：**这一步之后运行时不再需要模型**。报告是快照，不是每次打开都重算的服务。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.align import align_runs, pick_pair  # noqa: E402
from analysis.cluster import cluster, summarize  # noqa: E402
from analysis.diagnose import diagnose  # noqa: E402
from analysis.trace import run_from_dict  # noqa: E402

DATA = ROOT / "data"


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def build() -> dict:
    corpus = _load("corpus.json")
    tasks = {t["task_id"]: t for t in _load("tasks.json")["tasks"]}
    runs = [run_from_dict(d) for d in corpus["runs"]]

    diagnoses = {r.run_id: diagnose(r, tasks.get(r.task_id, {}).get("expect")) for r in runs if not r.success}
    patterns = cluster(runs, diagnoses)
    summary = summarize(runs)

    # 版本对比：同一任务在 v1 / v2 下的成功率
    versions = sorted({r.prompt_version for r in runs})
    version_rows = []
    for v in versions:
        subset = [r for r in runs if r.prompt_version == v]
        version_rows.append(
            {
                "version": v,
                "total": len(subset),
                "failed": sum(1 for r in subset if not r.success),
                "fail_rate": round(sum(1 for r in subset if not r.success) / max(1, len(subset)), 3),
                "tokens": sum(r.usage.get("total_tokens", 0) for r in subset),
                "avg_steps": round(sum(len(r.tool_steps) for r in subset) / max(1, len(subset)), 1),
            }
        )

    # 同一任务在两个版本之间的翻转情况，这是「修复到底有没有用」的直接证据
    flips = []
    if len(versions) >= 2:
        v1, v2 = versions[0], versions[-1]
        by_task: dict[str, dict[str, list[bool]]] = {}
        for r in runs:
            by_task.setdefault(r.task_id, {}).setdefault(r.prompt_version, []).append(r.success)
        for task_id, bucket in sorted(by_task.items()):
            a, b = bucket.get(v1, []), bucket.get(v2, [])
            if a and b:
                flips.append(
                    {
                        "task_id": task_id,
                        "from_ok": round(sum(a) / len(a), 2),
                        "to_ok": round(sum(b) / len(b), 2),
                        "delta": round(sum(b) / len(b) - sum(a) / len(a), 2),
                    }
                )

    # 「修复的代价」：按版本算每类失败的出现密度，找出改好之后反而变多的那几类。
    # 这是全网 trace 工具都不给的东西 —— 它们能对比两次实验，但不会告诉你「你修 A 的时候弄坏了 B」。
    regression: dict[str, list[dict]] = {"fixed": [], "worse": []}
    if len(versions) >= 2:
        v_old, v_new = versions[0], versions[-1]
        totals = {
            v: max(1, sum(1 for r in runs if r.prompt_version == v)) for v in (v_old, v_new)
        }
        for p in patterns:
            old = p.versions.get(v_old, 0) / totals[v_old]
            new = p.versions.get(v_new, 0) / totals[v_new]
            delta = round(new - old, 3)
            row = {
                "id": p.pid,
                "label": p.label,
                "signature": p.signature,
                "old": round(old, 3),
                "new": round(new, 3),
                "old_n": p.versions.get(v_old, 0),
                "new_n": p.versions.get(v_new, 0),
                "delta": delta,
            }
            if delta < -0.01:
                regression["fixed"].append(row)
            elif delta > 0.01:
                regression["worse"].append(row)
        regression["fixed"].sort(key=lambda r: r["delta"])
        regression["worse"].sort(key=lambda r: -r["delta"])
        regression["from"], regression["to"] = v_old, v_new
        regression["net"] = round(
            version_rows[-1]["fail_rate"] - version_rows[0]["fail_rate"], 3
        )

    pair = pick_pair(runs, diagnoses)
    fork = None
    if pair:
        good, bad = pair
        fork = align_runs(good, bad)
        fork["task_prompt"] = bad.task_prompt
        d = diagnoses.get(bad.run_id)
        if d:
            fork["diagnosis"] = d.to_dict()

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "summary": summary,
        "versions": version_rows,
        "flips": flips,
        "patterns": [p.to_dict(summary["failed"]) for p in patterns],
        "regression": regression,
        "fork": fork,
        "diagnoses": {k: v.to_dict() for k, v in diagnoses.items()},
    }


def main() -> None:
    report = build()
    if "--check" in sys.argv:
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print("\n失败模式：")
        for p in report["patterns"]:
            print(f"  {p['id']} {p['label']} / {p['tool']} × {p['count']}  {p['signature'][:60]}")
        return
    (DATA / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"report -> data/report.json  "
        f"({report['summary']['total']} 条轨迹 / 失败 {report['summary']['failed']} / "
        f"{len(report['patterns'])} 类失败模式)"
    )


if __name__ == "__main__":
    main()
