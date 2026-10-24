"""把采集回来的原始轨迹，编译成一份可被前端直接渲染的诊断报告。

用法：
    python analysis/build_report.py            # 读 data/corpus.json
    python analysis/build_report.py --check    # 只打印摘要，不写文件

产物：data/report.json
设计约束：**这一步之后运行时不再需要模型**。报告是快照，不是每次打开都重算的服务。
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.align import align_runs, pick_pair  # noqa: E402
from analysis.cluster import cluster, summarize  # noqa: E402
from analysis.diagnose import diagnose  # noqa: E402
from analysis.evaluate import EvalConfig, evaluate_all  # noqa: E402
from analysis.minimize import minimize, minimize_pattern  # noqa: E402
from analysis.trace import Run, kind_label, run_from_dict  # noqa: E402

DATA = ROOT / "data"
REPORT_VERSION = "0.3.0"


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def load_runs() -> tuple[list[Run], dict[str, dict]]:
    """读轨迹，并把任务期望注入进去。

    为什么注入而不是让每个模块自己去查 tasks：
    期望是轨迹的**语义部分**，缺了它，任务级规则（退款/转人工/只问不做…）全都失效。
    让它随 Run 走，下游三个模块（诊断、评分、最小化）就都不必知道 tasks.json 的存在。
    """
    corpus = _load("corpus.json")
    tasks = {t["task_id"]: t for t in _load("tasks.json")["tasks"]}
    runs: list[Run] = []
    for d in corpus["runs"]:
        run = run_from_dict(d)
        if not run.expected:
            run.expected = dict(tasks.get(run.task_id, {}).get("expect") or {})
        run.expectation_missing = not bool(run.expected)
        runs.append(run)
    return runs, tasks


def _cost_block(runs: list[Run], diagnoses: dict) -> dict:
    """成本与延迟画像。数字本身不稀奇，稀奇的是和失败率摆在一起看。"""
    def bucket(subset: list[Run]) -> dict:
        tokens = [int(r.usage.get("total_tokens") or 0) for r in subset]
        steps = [len(r.tool_steps) for r in subset]
        lat = [sum(s.latency_ms for s in r.steps) or 0 for r in subset]
        return {
            "runs": len(subset),
            "tokens": sum(tokens),
            "tokens_avg": round(sum(tokens) / len(tokens)) if tokens else 0,
            "tokens_p90": sorted(tokens)[int(len(tokens) * 0.9)] if tokens else 0,
            "steps_avg": round(sum(steps) / len(steps), 1) if steps else 0,
            "latency_avg_ms": round(sum(lat) / len(lat)) if lat else 0,
        }

    hist: dict[int, int] = {}
    for r in runs:
        n = len(r.tool_steps)
        hist[n] = hist.get(n, 0) + 1

    by_version = {}
    for v in sorted({r.prompt_version for r in runs}):
        by_version[v] = bucket([r for r in runs if r.prompt_version == v])

    # 失败轨迹平均比成功轨迹贵多少 —— 这是「失败的真实成本」，多数报告不给这个数
    ok = [r for r in runs if r.success]
    bad = [r for r in runs if not r.success]
    extra = None
    if ok and bad:
        ok_tok = statistics.mean([int(r.usage.get("total_tokens") or 0) for r in ok])
        bad_tok = statistics.mean([int(r.usage.get("total_tokens") or 0) for r in bad])
        ok_step = statistics.mean([len(r.tool_steps) for r in ok])
        bad_step = statistics.mean([len(r.tool_steps) for r in bad])
        extra = {
            "tokens_delta": round(bad_tok - ok_tok),
            "tokens_ratio": round(bad_tok / ok_tok, 2) if ok_tok else None,
            "steps_delta": round(bad_step - ok_step, 1),
            "steps_ratio": round(bad_step / ok_step, 2) if ok_step else None,
            # 一句话结论，避免读者把「失败更便宜」误读成好消息
            "verdict": (
                "失败轨迹平均比成功的更省 token：因为这里的失败大多是「少做事」（不做动作、只反问），"
                "而不是「乱做事」。省钱不等于省代价，代价转移到人工和用户等待上。"
                if bad_tok < ok_tok
                else "失败轨迹平均比成功的更贵：模型在错误路径上多绕了步数。"
            ),
        }

    # 按失败种类拆成本，才能看出钱到底花在哪类病上
    by_kind: dict[str, dict] = {}
    for r in runs:
        d = diagnoses.get(r.run_id)
        key = kind_label(d.kind) if d else ("成功" if r.success else "未归类")
        row = by_kind.setdefault(key, {"label": key, "runs": 0, "tokens": 0, "steps": 0})
        row["runs"] += 1
        row["tokens"] += int(r.usage.get("total_tokens") or 0)
        row["steps"] += len(r.tool_steps)
    for row in by_kind.values():
        row["tokens_avg"] = round(row["tokens"] / max(1, row["runs"]))
        row["steps_avg"] = round(row["steps"] / max(1, row["runs"]), 1)

    return {
        "overall": bucket(runs),
        "by_version": by_version,
        "by_kind": sorted(by_kind.values(), key=lambda x: -x["runs"]),
        "steps_histogram": [{"steps": k, "runs": v} for k, v in sorted(hist.items())],
        "failure_premium": extra,
    }


def _run_index(runs: list[Run], diagnoses: dict, evals: dict) -> list[dict]:
    """轨迹索引：给列表页用的轻量投影，不带完整 steps（避免报告体积失控）。"""
    eval_map = {r["run_id"]: r for r in evals["rows"]}
    out = []
    for r in runs:
        d = diagnoses.get(r.run_id)
        e = eval_map.get(r.run_id)
        out.append(
            {
                "run_id": r.run_id,
                "task_id": r.task_id,
                "version": r.prompt_version,
                "model": r.model,
                "success": r.success,
                "steps": len(r.tool_steps),
                "tokens": int(r.usage.get("total_tokens") or 0),
                "kind": d.kind if d else ("ok" if r.success else "unknown"),
                "label": kind_label(d.kind) if d else ("成功" if r.success else "未归类"),
                "confidence": round(d.confidence, 2) if d else None,
                "step_index": d.step_index if d else None,
                "overall": e["overall"] if e else None,
                "tools": sorted({s.tool for s in r.tool_steps if s.tool}),
            }
        )
    return out


def _catalog(runs: list[Run], tasks: dict[str, dict], diagnoses: dict) -> dict:
    rows = []
    for tid, t in tasks.items():
        subset = [r for r in runs if r.task_id == tid]
        if not subset:
            continue
        rows.append(
            {
                "task_id": tid,
                "prompt": t.get("prompt", ""),
                "expect": t.get("expect", {}),
                "runs": len(subset),
                "failed": sum(1 for r in subset if not r.success),
            }
        )
    kinds = {}
    for d in diagnoses.values():
        kinds[d.kind] = kinds.get(d.kind, 0) + 1
    return {
        "tasks": rows,
        "versions": sorted({r.prompt_version for r in runs}),
        "models": sorted({r.model for r in runs}),
        "kinds": [{"kind": k, "label": kind_label(k), "count": v} for k, v in sorted(kinds.items(), key=lambda x: -x[1])],
        "imported": sorted({r.imported_from for r in runs}),
    }


def build() -> dict:
    runs, tasks = load_runs()
    diagnoses = {r.run_id: diagnose(r, r.expected) for r in runs if not r.success}
    patterns = cluster(runs, diagnoses)
    summary = summarize(runs)

    # 版本对比：同一任务在不同版本下的成功率
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
    regression: dict = {"fixed": [], "worse": []}
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

    # ── 每个失败模式配一条最小复现用例 ──
    pattern_dicts = []
    for p in patterns:
        item = p.to_dict(summary["failed"])
        if p.kind != "unknown" and p.run_ids:
            m = minimize_pattern(runs, diagnoses, p.run_ids)
            if m:
                item["minimal"] = m
        pattern_dicts.append(item)

    evals = evaluate_all(runs, EvalConfig())

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "report_version": REPORT_VERSION,
        "summary": summary,
        "versions": version_rows,
        "flips": flips,
        "patterns": pattern_dicts,
        "regression": regression,
        "fork": fork,
        "diagnoses": {k: v.to_dict() for k, v in diagnoses.items()},
        "runs": _run_index(runs, diagnoses, evals),
        "evaluations": {k: v for k, v in evals.items() if k != "rows"},
        "evaluation_rows": {r["run_id"]: r for r in evals["rows"]},
        "cost": _cost_block(runs, diagnoses),
        "catalog": _catalog(runs, tasks, diagnoses),
    }


def main() -> None:
    report = build()
    if "--check" in sys.argv:
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print("\n失败模式：")
        for p in report["patterns"]:
            m = p.get("minimal")
            extra = f" | 最小复现 {m['original_steps']}->{m['minimal_steps']} 步" if m else ""
            print(f"  {p['id']} {p['label']} / {p['tool']} × {p['count']}{extra}")
        print("\n评分（按版本）：")
        for v, row in report["evaluations"]["by_version"].items():
            print(f"  {v}  总分 {row['overall']}")
        print("\n成本：", json.dumps(report["cost"]["failure_premium"], ensure_ascii=False))
        return
    (DATA / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"report -> data/report.json  "
        f"({report['summary']['total']} 条轨迹 / 失败 {report['summary']['failed']} / "
        f"{len(report['patterns'])} 类失败模式)"
    )


if __name__ == "__main__":
    main()
