"""接入层、最小化、评分器的单元测试。

这三个模块的共同点是：**它们很容易写出「看起来对」的假结果**。
所以测试的重点不是覆盖率，而是把三件容易被糊弄过去的事钉死：

1. 接入层不许把「没有任务期望」当成「任务成功」（OTLP / LangSmith 的真实陷阱）
2. 最小化不许把「拿不到证据」的步骤说成「可以删」
3. 评分器不许在没有依据时给分（必须 skip，而不是给 1.0）

跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.diagnose import diagnose  # noqa: E402
from analysis.evaluate import EvalConfig, evaluate  # noqa: E402
from analysis.ingest import detect_format, ingest, ingest_jsonl, roundtrip_stats  # noqa: E402
from analysis.minimize import minimize  # noqa: E402
from analysis.regress import verify as verify_regression  # noqa: E402
from analysis.trace import run_from_dict  # noqa: E402

SAMPLES = ROOT / "data" / "samples"


def make_run(steps: list[dict], success: bool = False, expect: dict | None = None) -> object:
    return run_from_dict(
        {
            "run_id": "R1",
            "task_id": "T01",
            "prompt_version": "v1",
            "task_prompt": "订单 A1024 要退款",
            "expected": expect if expect is not None else {"action": "refund", "order_id": "A1024"},
            "success": success,
            "steps": steps,
        }
    )


def tc(i, tool, args, obs=None, ok=True, error=None):
    return {"i": i, "kind": "tool_call", "tool": tool, "args": args, "observation": obs or {}, "ok": ok, "error": error}


def fin(i, content):
    return {"i": i, "kind": "final", "content": content}


# ── 接入层 ──────────────────────────────────────────────────────────────────
class TestIngest(unittest.TestCase):
    def test_detect_each_format(self):
        self.assertEqual(detect_format({"resourceSpans": []}), "otlp")
        self.assertEqual(detect_format({"messages": []}), "openai")
        self.assertEqual(detect_format({"runs": [{"run_type": "llm"}]}), "langsmith")
        self.assertEqual(detect_format({"run_id": "x", "steps": []}), "native")
        self.assertEqual(detect_format({"hello": 1}), "unknown")

    def test_unknown_format_reports_instead_of_guessing(self):
        res = ingest({"totally": "unrelated"})
        self.assertEqual(res.fmt, "unknown")
        self.assertEqual(res.runs, [])
        # 必须给出可操作的提示，而不是静默返回空
        self.assertTrue(any("无法识别" in w for w in res.warnings))

    def test_openai_adapter_builds_steps_and_diagnoses(self):
        """OpenAI messages 转过来之后，诊断结论必须和原生轨迹一致。"""
        msgs = [
            {"role": "user", "content": "订单 A1024 要退款"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "get_order", "arguments": json.dumps({"order_id": "A1024"})},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"order_id": "A1024", "paid": 899})},
            {"role": "assistant", "content": "需要我帮您确认一下退款原因吗？"},
        ]
        res = ingest([{"run_id": "X1", "expected": {"action": "refund"}, "success": False, "messages": msgs}])
        self.assertEqual(res.fmt, "openai")
        self.assertEqual(len(res.runs), 1)
        run = res.runs[0]
        self.assertEqual(len(run.tool_steps), 1)
        self.assertEqual(run.tool_steps[0].observation["paid"], 899)
        self.assertEqual(diagnose(run, run.expected).kind, "asked_instead_of_acted")

    def test_otlp_without_expectation_is_not_reported_as_success(self):
        """这是最重要的一条：工具全绿 ≠ 任务完成。"""
        payload = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "t1",
                                    "spanId": "s1",
                                    "name": "get_order",
                                    "startTimeUnixNano": "1000000000",
                                    "endTimeUnixNano": "2000000000",
                                    "attributes": [
                                        {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                                        {"key": "gen_ai.tool.name", "value": {"stringValue": "get_order"}},
                                        {
                                            "key": "gen_ai.tool.call.arguments",
                                            "value": {"stringValue": '{"order_id":"A1024"}'},
                                        },
                                    ],
                                    "status": {"code": "STATUS_CODE_UNSET"},
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        res = ingest(payload)
        self.assertEqual(res.fmt, "otlp")
        run = res.runs[0]
        self.assertFalse(run.success_known, "OTLP 无法判定任务成败，success_known 必须为 False")
        self.assertEqual(diagnose(run, run.expected).kind, "unknown")

    def test_langsmith_flat_array_groups_by_parent(self):
        """扁平 runs 数组必须顺 parent_run_id 聚类，而不是每条 run 自成一棵树。"""
        payload = {
            "runs": [
                {"id": "root", "run_type": "chain", "parent_run_id": None, "inputs": {"question": "退款 A1024"}, "outputs": {"output": "好的"}},
                {"id": "a", "name": "get_order", "run_type": "tool", "parent_run_id": "root", "inputs": {"order_id": "A1024"}, "outputs": {"paid": 899}},
                {"id": "b", "name": "get_user", "run_type": "tool", "parent_run_id": "root", "inputs": {"user_id": "U001"}, "outputs": {"vip": True}},
            ]
        }
        res = ingest(payload)
        self.assertEqual(res.fmt, "langsmith")
        self.assertEqual(len(res.runs), 1, f"应聚成 1 条轨迹，实际 {len(res.runs)}")
        self.assertEqual(len(res.runs[0].tool_steps), 2)

    def test_jsonl_multiple_lines(self):
        lines = "\n".join(
            [
                json.dumps({"run_id": "A", "success": False, "steps": [tc(1, "get_order", {"order_id": "A1"}), fin(2, "好的")]}),
                json.dumps({"run_id": "B", "success": False, "steps": [tc(1, "get_order", {"order_id": "A2"}), fin(2, "好的")]}),
            ]
        )
        res = ingest_jsonl(lines)
        self.assertEqual(len(res.runs), 2)
        self.assertEqual(roundtrip_stats(res.runs)["readable_ratio"], 1.0)

    def test_real_samples_roundtrip(self):
        """从真轨迹转出的样例，读回来必须能被识别格式且有工具调用。"""
        for fmt in ("openai", "otlp", "langsmith"):
            path = SAMPLES / f"{fmt}.json"
            if not path.exists():
                self.skipTest(f"样例 {fmt}.json 未生成，先跑 tools/make_samples.py")
            res = ingest(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(res.fmt, fmt)
            self.assertEqual(len(res.runs), 3, f"{fmt} 应解析出 3 条轨迹")
            for r in res.runs:
                self.assertGreater(len(r.tool_steps), 0)


# ── 最小化 ──────────────────────────────────────────────────────────────────
class TestMinimize(unittest.TestCase):
    def test_root_cause_is_always_kept(self):
        run = make_run(
            [
                tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}),
                tc(2, "escalate", {"order_id": "A1024", "user_id": "U001"}, {"ok": True}),
                fin(3, "已转人工"),
            ]
        )
        d = diagnose(run, run.expected)
        m = minimize(run, d)
        root = [n for n in m["chain"] if n["role"] == "root_cause"]
        self.assertEqual(len(root), 1)
        self.assertTrue(root[0]["keep"])

    def test_unrelated_query_is_dropped_with_reason(self):
        run = make_run(
            [
                tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}),
                tc(2, "search_policy", {"query": "退款政策"}, {"text": "7 天无理由"}),
                tc(3, "escalate", {"order_id": "A1024", "user_id": "U001"}, {"ok": True}),
                fin(4, "已转人工"),
            ]
        )
        m = minimize(run, diagnose(run, run.expected))
        by_index = {n["step_index"]: n for n in m["chain"]}
        self.assertFalse(by_index[2]["keep"], "从未被消费的查询应当可丢")
        self.assertIn("可", by_index[2]["why"])

    def test_never_claims_replay_verification(self):
        run = make_run([tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}), fin(2, "好的")])
        m = minimize(run, diagnose(run, run.expected))
        self.assertFalse(m["verified_by_replay"])
        self.assertEqual(m["method"], "structural")
        self.assertIn("没有重跑验证", m["caveat"])
        self.assertFalse(m["case"]["verified_by_replay"])

    def test_terminal_only_failure_is_marked(self):
        """任务级失败没有「出错的那一步」，必须标成 terminal_only 并解释清楚。"""
        run = make_run(
            [tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}), fin(2, "需要我帮您退款吗？")]
        )
        m = minimize(run, diagnose(run, run.expected))
        self.assertEqual(m["minimal_kind"], "terminal_only")
        self.assertTrue(m["case"]["note"])

    def test_counterfactual_picks_missing_field(self):
        run = make_run(
            [
                tc(1, "order_query", {"amount": 100}, {"error": "missing: order_id"}, ok=False, error="missing: order_id"),
                fin(2, "查不到"),
            ]
        )
        m = minimize(run, diagnose(run, run.expected))
        self.assertIn("order_id", m["counterfactual"]["what_if"])


# ── 评分器 ──────────────────────────────────────────────────────────────────
class TestEvaluate(unittest.TestCase):
    def test_without_expectation_scoring_is_tool_only(self):
        run = make_run([tc(1, "get_order", {"order_id": "A1"}, {"ok": 1}), fin(2, "好的")], expect={})
        ev = evaluate(run)
        self.assertEqual(ev["scope"], "tool_only")
        self.assertIn("task_completion", ev["skipped"])
        self.assertTrue(ev["note"], "必须说明为什么分数偏高")

    def test_completed_task_scores_full(self):
        run = make_run(
            [
                tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}),
                tc(2, "refund_order", {"order_id": "A1024", "amount": 899}, {"ok": True}),
                fin(3, "已为您退款 899 元"),
            ],
            success=True,
        )
        ev = evaluate(run)
        self.assertEqual(ev["scope"], "full")
        self.assertEqual(ev["overall"], 1.0)

    def test_skipped_never_counts_as_pass(self):
        """没有发生工具错误时，recovery 必须是跳过（None），不能记 1.0。"""
        run = make_run([tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}), fin(2, "好的")], success=True)
        ev = evaluate(run)
        rec = next(s for s in ev["scores"] if s["key"] == "recovery")
        self.assertIsNone(rec["score"])
        self.assertIn("没有发生工具错误", rec["reason"])

    def test_budget_violation_is_caught(self):
        steps = [tc(i, "get_order", {"order_id": f"A{1000 + i}"}, {"ok": 1}) for i in range(1, 12)]
        steps.append(fin(12, "好的"))
        run = make_run(steps, success=True)
        ev = evaluate(run, EvalConfig(step_budget=5))
        eff = next(s for s in ev["scores"] if s["key"] == "efficiency")
        self.assertEqual(eff["score"], 0.0)
        self.assertIn("超出", eff["reason"])


# ── 回归验收 ────────────────────────────────────────────────────────────────
CASE = {
    "case_id": "M-R1",
    "kind": "over_escalation",
    "label": "过度转人工",
    "task_id": "T01",
    "task_prompt": "订单 A1024 要退款",
    "expect": {"action": "refund"},
}

ESCALATING = [tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}), tc(2, "escalate", {"order_id": "A1024"}, {"ok": True}), fin(3, "已转人工")]
REFUNDING = [tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}), tc(2, "refund_order", {"order_id": "A1024", "amount": 899}, {"ok": True}), fin(3, "已为您退款")]


def case_runs(steps_sets: list[tuple[list[dict], bool]], task: str = "T01"):
    out = []
    for i, (steps, ok) in enumerate(steps_sets):
        out.append(
            run_from_dict(
                {
                    "run_id": f"R{i}",
                    "task_id": task,
                    "prompt_version": "v9",
                    "task_prompt": "订单 A1024 要退款",
                    "expected": {"action": "refund"},
                    "success": ok,
                    "steps": steps,
                }
            )
        )
    return out


class TestRegress(unittest.TestCase):
    def test_all_hit_means_present(self):
        res = verify_regression([CASE], case_runs([(ESCALATING, False), (ESCALATING, False)]))
        self.assertEqual(res["verdicts"][0]["status"], "present")
        self.assertEqual(res["counts"]["present"], 1)

    def test_some_hit_means_partial_not_fixed(self):
        """时好时坏必须单独算一类 —— 混进「已修复」就是骗自己。"""
        res = verify_regression([CASE], case_runs([(ESCALATING, False), (REFUNDING, True)]))
        v = res["verdicts"][0]
        self.assertEqual(v["status"], "partial")
        self.assertEqual((v["hit"], v["sample"]), (1, 2))
        self.assertIn("不稳定", v["detail"])

    def test_clean_pass_means_fixed(self):
        res = verify_regression([CASE], case_runs([(REFUNDING, True)]))
        self.assertEqual(res["verdicts"][0]["status"], "fixed")

    def test_still_failing_other_way_means_changed(self):
        steps = [tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}), fin(2, "需要我帮您确认吗？")]
        res = verify_regression([CASE], case_runs([(steps, False)]))
        v = res["verdicts"][0]
        self.assertEqual(v["status"], "changed")
        self.assertEqual(v["now_kind"], "asked_instead_of_acted")

    def test_uncovered_is_not_counted_as_pass(self):
        """没考 ≠ 考过了。这条是这个模块最重要的一条断言。"""
        res = verify_regression([CASE], case_runs([(REFUNDING, True)], task="T99"))
        self.assertEqual(res["verdicts"][0]["status"], "uncovered")
        self.assertEqual(res["covered"], 0)
        self.assertIn("无法判断", res["summary"])

    def test_empty_batch_does_not_claim_success(self):
        res = verify_regression([CASE], [])
        self.assertEqual(res["covered"], 0)
        self.assertIn("无法判断", res["summary"])


if __name__ == "__main__":
    unittest.main()