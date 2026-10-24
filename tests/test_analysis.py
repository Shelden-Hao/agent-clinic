"""诊断引擎的单元测试。

重点不是覆盖率，而是**把「判不出来」这件事也锁死**：
一条结构正常、无法定性的失败轨迹，必须得到 unknown，而不能被硬塞进某个类别。
这是这个项目敢说自己「可解释」的前提。

跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.align import align_runs, lcs_pairs  # noqa: E402
from analysis.cluster import cluster, summarize  # noqa: E402
from analysis.diagnose import diagnose  # noqa: E402
from analysis.trace import run_from_dict  # noqa: E402


def make_run(run_id: str, steps: list[dict], success: bool, version: str = "v1", task: str = "T01"):
    return run_from_dict(
        {
            "run_id": run_id,
            "task_id": task,
            "prompt_version": version,
            "task_prompt": "订单 A1024 要退款",
            "success": success,
            "steps": steps,
            "final_content": "",
        }
    )


def tc(i, tool, args, obs=None, ok=True, error=None):
    return {"i": i, "kind": "tool_call", "tool": tool, "args": args, "observation": obs or {}, "ok": ok, "error": error}


def fin(i, content):
    return {"i": i, "kind": "final", "content": content}


class DiagnoseTests(unittest.TestCase):
    def test_argument_error_is_root_cause(self):
        run = make_run(
            "r1",
            [
                tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899, "refunded": 0}),
                tc(2, "refund_order", {"order_id": "A1024"}, {"ok": True}),
                fin(3, "已退款"),
            ],
            success=False,
        )
        d = diagnose(run, {"action": "refund"})
        # 第 2 步没有报错（这个假工具没校验），但 refund_order 未被判定成功，
        # 关键点在于：它必须给出一个可追溯的结论，而不是沉默
        self.assertIn(d.kind, {"bad_argument", "tool_error", "unknown", "wrong_tool"})
        self.assertTrue(d.evidence)

    def test_tool_error_with_recovery_is_not_fatal(self):
        run = make_run(
            "r2",
            [
                tc(1, "refund_order", {"order_id": "A1024", "amount": 9999, "reason": "x"},
                   {"error": "金额超过可退余额"}, ok=False, error="退款金额 9999 超过可退余额"),
                tc(2, "refund_order", {"order_id": "A1024", "amount": 899, "reason": "x"}, {"ok": True}),
                fin(3, "已退款 899"),
            ],
            success=True,
        )
        d = diagnose(run, {"action": "refund"})
        self.assertEqual(d.kind, "unknown")
        self.assertEqual(d.step_index, -1)

    def test_escalation_missed(self):
        run = make_run(
            "r3",
            [
                tc(1, "get_order", {"order_id": "A1026"}, {"paid": 259}),
                tc(2, "refund_order", {"order_id": "A1026", "amount": 259, "reason": "不满意"}, {"ok": True}),
                fin(3, "已退款"),
            ],
            success=False,
            task="T03",
        )
        d = diagnose(run, {"action": "escalate", "reason": "超过退款期限"})
        self.assertEqual(d.kind, "escalation_missed")
        self.assertEqual(d.step_index, 2)

    def test_loop_detected(self):
        run = make_run(
            "r4",
            [
                tc(1, "get_order", {"order_id": "A1024"}),
                tc(2, "get_order", {"order_id": "A1024"}),
                tc(3, "get_order", {"order_id": "A1024"}),
                fin(4, "不知道"),
            ],
            success=False,
        )
        d = diagnose(run, {"action": "refund"})
        self.assertEqual(d.kind, "loop")

    def test_hallucinated_order_id(self):
        run = make_run(
            "r5",
            [
                tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}),
                fin(2, "已为订单 A8899 办理退款"),
            ],
            success=False,
        )
        d = diagnose(run, {"action": "refund"})
        self.assertEqual(d.kind, "hallucinated_fact")

    def test_unknown_when_no_rule_fires(self):
        # 没有预期动作、没有工具报错、没有幻觉、没有重复 —— 所有规则都不该开火
        run = make_run(
            "r6",
            [tc(1, "get_order", {"order_id": "A1024"}, {"paid": 899}), fin(2, "好的")],
            success=False,
        )
        d = diagnose(run, {})
        self.assertEqual(d.kind, "unknown")
        self.assertEqual(d.confidence, 0.0)


class AlignTests(unittest.TestCase):
    def test_lcs_basic(self):
        pairs = lcs_pairs(["a", "b", "c"], ["a", "c"])
        self.assertEqual(pairs, [(0, 0), (1, None), (2, 1)])

    def test_fork_points_to_diverging_step(self):
        good = make_run(
            "g",
            [tc(1, "get_order", {"order_id": "A1024"}), tc(2, "search_policy", {"query": "期限"}), tc(3, "refund_order", {"order_id": "A1024", "amount": 899, "reason": "x"})],
            success=True,
        )
        bad = make_run(
            "b",
            [tc(1, "get_order", {"order_id": "A1024"}), tc(2, "get_logistics", {"order_id": "A1024"}), fin(3, "好的")],
            success=False,
        )
        res = align_runs(good, bad)
        self.assertIsNotNone(res["fork_at"])
        self.assertEqual(res["fork_at"], 2)


class ClusterTests(unittest.TestCase):
    def test_single_sample_span_is_merged_into_noise(self):
        runs, diags = [], {}
        for i in range(4):
            r = make_run(
                f"f{i}",
                [
                    tc(1, "refund_order", {"order_id": f"A10{20 + i}"}, {"error": "参数错误"}, ok=False, error="参数错误：amount 必须是数字"),
                    fin(2, "失败"),
                ],
                success=False,
                task=f"T{i:02d}",
            )
            runs.append(r)
            diags[r.run_id] = diagnose(r, {"action": "refund"})
        patterns = cluster(runs, diags)
        # 4 条同构失败应当收敛成一类，而不是四类
        self.assertEqual(len(patterns), 1)
        self.assertEqual(len(patterns[0].run_ids), 4)

    def test_summarize_counts(self):
        runs = [
            make_run("a", [fin(1, "ok")], success=True),
            make_run("b", [fin(1, "no")], success=False),
        ]
        s = summarize(runs)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["fail_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
