"""实验矩阵与复核队列的单元测试。

这两个模块的价值不在「算得对」，而在**口径说得住**。所以测试钉的是四件事：

1. 通过率相同但内部构成变了，不能被当成「没变化」
2. 「一部分成功」不能算「修好了」，也不能算「弄坏了」
3. 复核队列不许把所有失败都收进来（筛不动的队列等于没有队列）
4. 改判/驳回必须真的改变复核后的统计，而确认不能

跑法：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.experiment import _migration, _verdict, compare  # noqa: E402
from analysis.review import build_queue, verify_review  # noqa: E402
from analysis.trace import run_from_dict  # noqa: E402


def make_run(
    run_id: str,
    task_id: str,
    version: str,
    success: bool,
    steps: list[dict] | None = None,
) -> object:
    return run_from_dict(
        {
            "run_id": run_id,
            "task_id": task_id,
            "prompt_version": version,
            "task_prompt": f"{task_id} 的任务",
            "model": "test",
            "success": success,
            "expected": {"action": "refund"},
            "actual": {},
            "usage": {"total_tokens": 100},
            "steps": steps
            or [
                {"index": 1, "kind": "tool_call", "tool": "get_order", "args": {"order_id": "A1"}},
                {"index": 2, "kind": "final", "content": "好的"},
            ],
        }
    )


def diag(kind: str, confidence: float = 0.8) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, confidence=confidence, evidence="测试证据", step_index=1)


def version_row(name: str, total: int, failed: int) -> dict:
    return {
        "version": name,
        "total": total,
        "failed": failed,
        "fail_rate": round(failed / total, 3) if total else 0.0,
        "tokens": 0,
        "avg_steps": 4.0,
    }


class TestVerdict(unittest.TestCase):
    """判定口径：三件事不能混。"""

    def test_all_pass_to_all_fail_is_regressed(self):
        """基线全过 → 现在全挂。必须叫「弄坏了」，而不是「失败率上升」。"""
        self.assertEqual(_verdict(2, 2, 0, 2), "regressed")

    def test_all_fail_to_all_pass_is_the_only_fixed(self):
        """只有「从没全过到全过」才允许叫修好了。"""
        self.assertEqual(_verdict(0, 2, 2, 2), "fixed")
        self.assertEqual(_verdict(1, 2, 2, 2), "fixed")

    def test_partial_pass_is_not_fixed(self):
        """一半过一次不过，绝不能算修好 —— 这是这个项目最想拦住的一种自欺。"""
        self.assertEqual(_verdict(0, 2, 1, 2), "flaky_improved")
        self.assertNotEqual(_verdict(0, 2, 1, 2), "fixed")

    def test_partial_to_zero_is_worse(self):
        self.assertEqual(_verdict(1, 2, 0, 2), "worse")

    def test_stable_states(self):
        self.assertEqual(_verdict(2, 2, 2, 2), "stable_ok")
        self.assertEqual(_verdict(0, 2, 0, 2), "stable_bad")

    def test_same_partial_rate_is_flaky_not_stable(self):
        """前后都是 1/2：看起来「没变化」，实际是两次都在抛硬币。"""
        self.assertEqual(_verdict(1, 2, 1, 2), "flaky")

    def test_missing_side_is_not_judged(self):
        self.assertEqual(_verdict(0, 0, 2, 2), "missing")
        self.assertEqual(_verdict(2, 2, 0, 0), "missing")


class TestCompare(unittest.TestCase):
    """矩阵与稳定性统计。"""

    def _toy(self):
        # T1: 稳定失败 → 全通过（真修好）
        # T2: 稳定失败 → 一半（变抛硬币）
        # T3: 稳定通过 → 全挂（弄坏）
        # T4: 一直失败
        runs = [
            make_run("T1-v1-1", "T1", "v1", False),
            make_run("T1-v1-2", "T1", "v1", False),
            make_run("T1-v2-1", "T1", "v2", True),
            make_run("T1-v2-2", "T1", "v2", True),
            make_run("T2-v1-1", "T2", "v1", False),
            make_run("T2-v1-2", "T2", "v1", False),
            make_run("T2-v2-1", "T2", "v2", True),
            make_run("T2-v2-2", "T2", "v2", False),
            make_run("T3-v1-1", "T3", "v1", True),
            make_run("T3-v1-2", "T3", "v1", True),
            make_run("T3-v2-1", "T3", "v2", False),
            make_run("T3-v2-2", "T3", "v2", False),
            make_run("T4-v1-1", "T4", "v1", False),
            make_run("T4-v1-2", "T4", "v1", False),
            make_run("T4-v2-1", "T4", "v2", False),
            make_run("T4-v2-2", "T4", "v2", False),
        ]
        diags = {
            "T1-v1-1": diag("no_action"),
            "T1-v1-2": diag("no_action"),
            "T2-v1-1": diag("over_escalation"),
            "T2-v1-2": diag("over_escalation"),
            "T2-v2-2": diag("promised_but_not_done"),
            "T3-v2-1": diag("over_escalation"),
            "T3-v2-2": diag("over_escalation"),
            "T4-v1-1": diag("loop"),
            "T4-v1-2": diag("loop"),
            "T4-v2-1": diag("loop"),
            "T4-v2-2": diag("loop"),
        }
        return runs, diags

    def test_matrix_cells_keep_the_ratio(self):
        """单元格必须保留「几次通过 / 共几次」，不能压成 0/1。"""
        runs, diags = self._toy()
        out = compare(runs, diags, [version_row("v1", 8, 6), version_row("v2", 8, 5)])
        cells = {r["task_id"]: r for r in out["rows"]}
        self.assertEqual(cells["T2"]["cells"]["v2"]["ok"], 1)
        self.assertEqual(cells["T2"]["cells"]["v2"]["total"], 2)
        self.assertTrue(cells["T2"]["fragile"])

    def test_verdicts_match_reality(self):
        runs, diags = self._toy()
        out = compare(runs, diags, [version_row("v1", 8, 6), version_row("v2", 8, 5)])
        got = {r["task_id"]: r["verdict"] for r in out["rows"]}
        self.assertEqual(got["T1"], "fixed")
        self.assertEqual(got["T2"], "flaky_improved")
        self.assertEqual(got["T3"], "regressed")
        self.assertEqual(got["T4"], "stable_bad")

    def test_stability_counts_flaky_tasks(self):
        """抛硬币任务数必须单独统计 —— 它是失败率盖不住的那一项。"""
        runs, diags = self._toy()
        out = compare(runs, diags, [version_row("v1", 8, 6), version_row("v2", 8, 5)])
        self.assertEqual(out["stability"]["v1"]["flaky"], 0)
        self.assertEqual(out["stability"]["v2"]["flaky"], 1)
        # v1 有 T1/T2/T4 三个稳定失败；v2 剩 T3/T4 两个，其中 T3 是新坏的
        self.assertEqual(out["stability"]["v1"]["stable_bad"], 3)
        self.assertEqual(out["stability"]["v2"]["stable_bad"], 2)

    def test_narrative_says_partial_is_not_fixed(self):
        """结论句里不能出现「修好了 4 个」这种把部分通过算成修好的说法。"""
        runs, diags = self._toy()
        out = compare(runs, diags, [version_row("v1", 8, 6), version_row("v2", 8, 5)])
        n = out["narrative"]
        self.assertIn("真正修好了 1 个", n["headline"])
        self.assertIn("一半概率失败", n["headline"])
        self.assertIn("抛硬币", n["stability"])
        # 失败率是改善的，但结论必须同时点出它由什么撑起来的
        self.assertLess(n["delta_pt"], 0)
        self.assertEqual(n["flaky_delta"], 1)

    def test_single_version_does_not_pretend(self):
        runs, diags = self._toy()
        out = compare(runs, diags, [version_row("v1", 8, 6)])
        self.assertIn("无法对比", out["narrative"]["headline"])


class TestMigration(unittest.TestCase):
    """病种迁移：换了死法不能算修好。"""

    def test_changed_kind_is_counted_separately(self):
        rows = [
            {"task_id": "T1", "from_kinds": ["asked_instead_of_acted"], "to_kinds": ["promised_but_not_done"]},
            {"task_id": "T2", "from_kinds": ["over_escalation"], "to_kinds": ["promised_but_not_done"]},
            {"task_id": "T3", "from_kinds": ["loop"], "to_kinds": ["loop"]},
        ]
        out = _migration(rows)
        self.assertEqual(out["changed"], 2)
        self.assertEqual(out["top"]["kind"], "promised_but_not_done")
        self.assertEqual(out["top"]["count"], 2)
        self.assertIn("换了死法", out["note"])

    def test_no_change_means_silence(self):
        rows = [{"task_id": "T1", "from_kinds": ["loop"], "to_kinds": ["loop"]}]
        out = _migration(rows)
        self.assertEqual(out["changed"], 0)
        self.assertEqual(out["note"], "")


class TestReviewQueue(unittest.TestCase):
    """复核队列：筛不动等于没有队列。"""

    def _corpus(self, n_clean: int = 20):
        """构造一批「判得很确定、且同任务下没有反例」的失败，加上少量可疑项。"""
        runs, diags = [], {}
        for i in range(n_clean):
            rid = f"OK{i:02d}"
            runs.append(make_run(rid, f"T{i:02d}", "v1", False))
            diags[rid] = diag("over_escalation", 0.85)
        # 一条置信度贴下限的
        runs.append(make_run("LOW1", "TX1", "v1", False))
        diags["LOW1"] = diag("no_action", 0.70)
        # 一组同版本有成有败的
        runs.append(make_run("FLK-1", "TX2", "v1", False))
        runs.append(make_run("FLK-2", "TX2", "v1", True))
        diags["FLK-1"] = diag("promised_but_not_done", 0.80)
        # 一条规则没认出来的
        runs.append(make_run("UNK1", "TX3", "v1", False))
        diags["UNK1"] = diag("unknown", 0.2)
        return runs, diags

    def test_queue_is_a_minority_not_the_whole_list(self):
        """绝大多数诊断不该进队列。全收进来就失去了筛选的意义。"""
        runs, diags = self._corpus()
        out = build_queue(runs, diags, {})
        self.assertEqual(out["total_failed"], sum(1 for r in runs if not r.success))
        self.assertLess(out["queued"], out["total_failed"] * 0.5)
        self.assertEqual(out["queued"], 3)  # LOW1 / FLK-1 / UNK1

    def test_each_signal_has_a_reason(self):
        runs, diags = self._corpus()
        out = build_queue(runs, diags, {})
        got = {i["run_id"]: i["signals"] for i in out["items"]}
        self.assertIn("low_confidence", got["LOW1"])
        self.assertIn("unstable_pair", got["FLK-1"])
        self.assertIn("unclassified", got["UNK1"])

    def test_unstable_pair_considers_self(self):
        """「同版本有成有败」必须把被判的这条自己算进去，否则永远不触发。"""
        runs = [make_run("A", "T1", "v1", False), make_run("B", "T1", "v1", True)]
        diags = {"A": diag("loop", 0.9)}
        out = build_queue(runs, diags, {})
        self.assertEqual(out["queued"], 1)
        self.assertEqual(out["items"][0]["run_id"], "A")
        self.assertEqual(out["items"][0]["signals"], ["unstable_pair"])

    def test_peers_carry_their_own_verdict(self):
        """复核要看「同版本另一条被判成了什么」，光说「另一条失败了」不够。"""
        runs = [make_run("A", "T1", "v1", False), make_run("B", "T1", "v1", True)]
        diags = {"A": diag("loop", 0.9)}
        item = build_queue(runs, diags, {})["items"][0]
        self.assertEqual(len(item["peers"]), 1)
        self.assertTrue(item["peers"][0]["success"])
        self.assertEqual(item["peers"][0]["label"], "成功")

    def test_confirm_changes_nothing_but_counts(self):
        runs, diags = self._corpus()
        base = build_queue(runs, diags, {})
        rec = {"action": "confirm", "kind": None, "note": "", "original_kind": "no_action"}
        out = build_queue(runs, diags, {"LOW1": rec})
        self.assertEqual(out["before"], base["before"])
        self.assertEqual(out["after"], base["after"])
        self.assertEqual(out["actions"]["confirm"], 1)
        self.assertEqual(out["pending"], base["pending"] - 1)
        self.assertFalse(out["changed"])

    def test_override_moves_the_count(self):
        """改判必须让复核后的分布真的变 —— 否则签字就是走过场。"""
        runs, diags = self._corpus()
        rec = {"action": "override", "kind": "context_lost", "note": "看第 2 步重复问了订单号", "original_kind": "no_action"}
        out = build_queue(runs, diags, {"LOW1": rec})
        before = {r["kind"]: r["count"] for r in out["before"]}
        after = {r["kind"]: r["count"] for r in out["after"]}
        # 归零的病种会从结果里消失，而不是留一个 0
        self.assertEqual(after.get("no_action", 0), before["no_action"] - 1)
        self.assertEqual(after.get("context_lost"), 1)
        self.assertTrue(out["changed"])
        # 改判只改口径，失败总数不变
        self.assertEqual(out["after_failed"], out["total_failed"])

    def test_dismiss_removes_a_failure(self):
        """驳回 = 判为不算失败，失败总数必须减一，不能只改病名。"""
        runs, diags = self._corpus()
        rec = {"action": "dismiss", "kind": None, "note": "用户本来就没要求退款", "original_kind": "unknown"}
        out = build_queue(runs, diags, {"UNK1": rec})
        self.assertEqual(out["after_failed"], out["total_failed"] - 1)
        self.assertTrue(out["changed"])

    def test_unknown_kind_counts_as_failure_for_average(self):
        """unknown 也是诊断，不能从分母里消失。"""
        runs, diags = self._corpus()
        out = build_queue(runs, diags, {})
        kinds = {r["kind"] for r in out["before"]}
        self.assertIn("unknown", kinds)


class TestReviewValidation(unittest.TestCase):
    """复核记录的校验：宁可拒绝，也不要存一条读不懂的记录。"""

    def test_override_requires_reason(self):
        with self.assertRaises(ValueError):
            verify_review({"action": "override", "kind": "loop", "note": ""}, {"loop"})

    def test_override_requires_known_kind(self):
        with self.assertRaises(ValueError):
            verify_review({"action": "override", "kind": "made_up", "note": "x"}, {"loop"})

    def test_bad_action_rejected(self):
        with self.assertRaises(ValueError):
            verify_review({"action": "delete", "note": "x"}, {"loop"})

    def test_ok_override_normalises(self):
        rec = verify_review(
            {"action": "override", "kind": "loop", "note": " 重复调用  ", "reviewer": ""},
            {"loop"},
        )
        self.assertEqual(rec["kind"], "loop")
        self.assertEqual(rec["note"], "重复调用")
        self.assertEqual(rec["reviewer"], "值班医师")
        self.assertTrue(rec["at"])

    def test_confirm_drops_the_kind(self):
        """确认机器判定时，不该顺手记一个 kind —— 那会让记录看起来像改判。"""
        rec = verify_review({"action": "confirm", "kind": "loop", "note": ""}, {"loop"})
        self.assertIsNone(rec["kind"])


if __name__ == "__main__":
    unittest.main()
