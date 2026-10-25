"""多轮会话与 Prompt 版本管理的单测。

这里的重点不是「功能能用」，而是**不许糊弄**：
- 跨轮病不允许在证据不足时触发（宁可漏报，不可误报 —— 误报会让人去改对的规则）
- 草稿版本的失败率必须是 None，不能是 0
- 句级 diff 必须数得清「几句变成几句」
- compose 不许改动基线版的正文
"""

from __future__ import annotations

import unittest

from analysis.promptlib import (
    Source,
    Version,
    compose_patch,
    diff,
    dump_versions,
    lineage,
    load_versions,
    next_id,
    overview,
    stats,
)
from analysis.session import (
    SESSION_KINDS,
    Session,
    diagnose_session,
    history_dependency,
    session_from_dict,
    summarize,
    turn_from_dict,
)


def mk_session(turns: list[dict], sid="S01-v1-1") -> Session:
    return session_from_dict({"session_id": sid, "scenario_id": "S01", "prompt_version": "v1", "turns": turns})


def tool(i, name, args, ok=True):
    return {"i": i, "kind": "tool_call", "tool": name, "args": args, "ok": ok}


def final(content):
    return {"kind": "final", "content": content}


class TestCorrectionIgnored(unittest.TestCase):
    def test_detects_when_action_stays_on_old_target(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "退掉 A1025", "success": True,
                 "steps": [tool(1, "refund_order", {"order_id": "A1025", "amount": 469})]},
                {"turn_index": 2, "user": "不对，我说的是 A1035 那个背包", "success": False,
                 "steps": [tool(1, "get_order", {"order_id": "A1025"})]},
            ]
        )
        kinds = [f.kind for f in diagnose_session(s)]
        self.assertIn("correction_ignored", kinds)

    def test_write_on_old_target_is_severity_3(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "退掉 A1025", "success": True, "steps": []},
                {"turn_index": 2, "user": "不对，我要退 A1035", "success": False,
                 "steps": [tool(1, "refund_order", {"order_id": "A1025", "amount": 469})]},
            ]
        )
        f = [x for x in diagnose_session(s) if x.kind == "correction_ignored"][0]
        self.assertEqual(f.severity, 3)

    def test_query_only_on_old_target_is_severity_2(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "退掉 A1025", "success": True, "steps": []},
                {"turn_index": 2, "user": "不对，我要退 A1035", "success": False,
                 "steps": [tool(1, "get_order", {"order_id": "A1025"})]},
            ]
        )
        f = [x for x in diagnose_session(s) if x.kind == "correction_ignored"][0]
        self.assertEqual(f.severity, 2)

    def test_no_hit_when_agent_followed_correction(self):
        """跟上了就不能报 —— 误报会让人去改本来对的规则。"""
        s = mk_session(
            [
                {"turn_index": 1, "user": "退掉 A1025", "success": True, "steps": []},
                {"turn_index": 2, "user": "不对，我要退 A1035", "success": True,
                 "steps": [tool(1, "refund_order", {"order_id": "A1035", "amount": 549})]},
            ]
        )
        self.assertNotIn("correction_ignored", [f.kind for f in diagnose_session(s)])

    def test_no_hit_when_no_new_target_given(self):
        """用户说「不对」但没给新目标 —— 无法断言应该改成什么，不报。"""
        s = mk_session(
            [
                {"turn_index": 1, "user": "退掉 A1025", "success": True, "steps": []},
                {"turn_index": 2, "user": "不对，你搞错了", "success": False,
                 "steps": [tool(1, "get_order", {"order_id": "A1025"})]},
            ]
        )
        self.assertNotIn("correction_ignored", [f.kind for f in diagnose_session(s)])


class TestContextLost(unittest.TestCase):
    def test_detects_reask_after_user_already_gave_it(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "订单 A1033 音质太差还能退吗", "success": True,
                 "steps": [tool(1, "get_order", {"order_id": "A1033"})]},
                {"turn_index": 2, "user": "那就退了吧", "success": False,
                 "steps": [final("请提供一下您的订单号")]},
            ]
        )
        self.assertIn("context_lost_across_turn", [f.kind for f in diagnose_session(s)])

    def test_no_hit_when_this_turn_has_own_id(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "订单 A1033 能退吗", "success": True, "steps": []},
                {"turn_index": 2, "user": "把 A1033 退了吧", "success": False,
                 "steps": [final("请提供订单号")]},
            ]
        )
        self.assertNotIn("context_lost_across_turn", [f.kind for f in diagnose_session(s)])

    def test_no_hit_when_agent_actually_acted(self):
        """查过东西再顺手确认一句，是谨慎，不是遗忘。"""
        s = mk_session(
            [
                {"turn_index": 1, "user": "订单 A1033 能退吗", "success": True, "steps": []},
                {"turn_index": 2, "user": "那就退了吧", "success": True,
                 "steps": [tool(1, "search_policy", {"query": "退款期限"}), final("请确认订单号 A1033 对吗")]},
            ]
        )
        self.assertNotIn("context_lost_across_turn", [f.kind for f in diagnose_session(s)])


class TestRepeatedEffect(unittest.TestCase):
    def test_detects_duplicate_escalate(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "椅子 A1029 要全额退，运费也退", "success": True,
                 "steps": [tool(1, "escalate", {"summary": "A1029 运费争议"})]},
                {"turn_index": 2, "user": "工单开好了吗", "success": False,
                 "steps": [tool(1, "escalate", {"summary": "A1029 运费争议"})]},
            ]
        )
        self.assertIn("repeated_effect", [f.kind for f in diagnose_session(s)])

    def test_no_hit_when_user_asked_for_more(self):
        """用户说了「剩下的也退」，重复就是被要求的。"""
        s = mk_session(
            [
                {"turn_index": 1, "user": "充电宝 A1028 先退 99", "success": True,
                 "steps": [tool(1, "refund_order", {"order_id": "A1028", "amount": 99})]},
                {"turn_index": 2, "user": "剩下的 60 也退了吧", "success": True,
                 "steps": [tool(1, "refund_order", {"order_id": "A1028", "amount": 60})]},
            ]
        )
        self.assertNotIn("repeated_effect", [f.kind for f in diagnose_session(s)])

    def test_no_hit_across_different_orders(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "退 A1025", "success": True,
                 "steps": [tool(1, "refund_order", {"order_id": "A1025", "amount": 469})]},
                {"turn_index": 2, "user": "那 A1035 也退", "success": True,
                 "steps": [tool(1, "refund_order", {"order_id": "A1035", "amount": 549})]},
            ]
        )
        self.assertNotIn("repeated_effect", [f.kind for f in diagnose_session(s)])


class TestDependencyAndSummary(unittest.TestCase):
    def test_tags(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "退掉 A1025", "success": True, "steps": []},
                {"turn_index": 2, "user": "那就退了吧", "success": False, "steps": []},
                {"turn_index": 3, "user": "不对，我要 A1035", "success": False,
                 "steps": [tool(1, "get_order", {"order_id": "A1025"})]},
            ]
        )
        deps = {r["turn_index"]: r["tag"] for r in history_dependency(s)}
        self.assertEqual(deps[1], "independent")
        self.assertEqual(deps[2], "needs_history")       # 没有标识符，靠历史
        self.assertEqual(deps[3], "harmed_by_history")   # 被纠正忽略拖累

    def test_session_success_requires_every_turn(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "A1025 退", "success": True, "steps": []},
                {"turn_index": 2, "user": "再确认下", "success": False, "steps": []},
            ]
        )
        self.assertFalse(s.success)

    def test_blind_spot_rate_counts_only_harmed_failures(self):
        """盲区 = 单轮测试会给假绿灯的失败轮。needs_history 不算盲区（单轮会报错，不会假绿灯）。"""
        s = mk_session(
            [
                {"turn_index": 1, "user": "退掉 A1025", "success": True, "steps": []},
                {"turn_index": 2, "user": "那就退了吧", "success": False, "steps": []},
                {"turn_index": 3, "user": "不对，我要 A1035", "success": False,
                 "steps": [tool(1, "get_order", {"order_id": "A1025"})]},
            ]
        )
        out = summarize([s])
        self.assertEqual(out["failed_turns"], 2)
        self.assertEqual(out["blind_spot_turns"], 1)
        self.assertAlmostEqual(out["blind_spot_rate"], 0.5)

    def test_kinds_table_covers_every_detected_kind(self):
        s = mk_session(
            [
                {"turn_index": 1, "user": "退 A1025", "success": True, "steps": []},
                {"turn_index": 2, "user": "不对，我说的是 A1035", "success": False,
                 "steps": [tool(1, "get_order", {"order_id": "A1025"})]},
            ]
        )
        found = {f.kind for f in diagnose_session(s)}
        self.assertTrue(found <= set(SESSION_KINDS))
        self.assertTrue(any(k["kind"] in found for k in summarize([s])["kinds"]))


def mk_ver(vid, system, parent=None, status="active", label=None):
    return Version(vid=vid, label=label or vid, system=system, parent=parent, status=status)


class TestPromptLib(unittest.TestCase):
    def test_lineage_stops_on_cycle(self):
        """手改 JSON 造出环不该把服务拖死。"""
        vs = {
            "v1": mk_ver("v1", "a", parent="v2"),
            "v2": mk_ver("v2", "b", parent="v1"),
        }
        self.assertEqual(lineage(vs, "v1"), ["v2", "v1"])

    def test_next_id_ignores_non_numeric(self):
        vs = {"v1": mk_ver("v1", ""), "v10": mk_ver("v10", ""), "beta": mk_ver("beta", "")}
        self.assertEqual(next_id(vs), "v11")

    def test_sentence_diff_counts_rules_not_lines(self):
        a = mk_ver("v1", "你是客服。你可以退款。请帮用户解决问题。")
        b = mk_ver("v2", "你是客服。\n1. 必须先查政策。\n2. 必须核对金额。")
        d = diff(a, b)
        self.assertEqual(d["stats"]["sentences_a"], 3)
        self.assertEqual(d["stats"]["sentences_b"], 3)
        self.assertGreater(d["stats"]["changed"], 0)

    def test_diff_of_same_text_is_empty(self):
        a = mk_ver("v1", "你是客服。请帮用户解决问题。")
        d = diff(a, mk_ver("v2", "你是客服。请帮用户解决问题。"))
        self.assertTrue(d["same"])
        self.assertEqual(d["stats"]["changed"], 0)

    def test_compose_does_not_touch_base_text(self):
        base = mk_ver("v2", "1. 只查政策。\n2. 只核对金额。")
        src = Source(kind="pattern", ref="over_escalation", label="过度转人工", evidence="6→9")
        v = compose_patch(base, [("以下情形必须直接办理。", "过度转人工（15 次）", src)], "v3")
        self.assertTrue(v.system.startswith(base.system))
        self.assertIn("以下情形必须直接办理。", v.system)
        self.assertIn("来源:pattern:over_escalation", v.system)
        self.assertEqual(v.parent, "v2")

    def test_compose_consumes_generator_once(self):
        """生成器只能消费一次 —— 早期版本在这里静默少写过规则。"""
        base = mk_ver("v1", "基线。")
        src = Source(kind="pattern", ref="loop", label="原地绕圈")
        gen = ((f"规则{i}。", f"标题{i}", src) for i in range(3))
        v = compose_patch(base, gen, "v2")
        self.assertIn("规则0。", v.system)
        self.assertIn("规则2。", v.system)
        self.assertEqual(len(v.sources), 3)

    def test_draft_has_no_fail_rate(self):
        """草稿没跑过轨迹 —— 失败率必须是 None，不能是 0。"""
        base = mk_ver("v2", "x")
        v = compose_patch(base, [("规则。", "标题", Source("pattern", "loop", "原地绕圈"))], "v3")
        self.assertEqual(v.status, "draft")
        st = stats([], {"v2": base, "v3": v})
        self.assertIsNone(st["v3"]["fail_rate"])
        self.assertFalse(st["v3"]["measured"])

    def test_overview_headline_says_draft_is_unmeasured(self):
        base = mk_ver("v2", "x")
        v = compose_patch(base, [("规则。", "标题", Source("pattern", "loop", "原地绕圈"))], "v3")
        rows = [{"version": "v2", "success": True, "kind": None, "label": None}] * 4
        lines = [{"version": "v2", "success": False, "kind": "loop", "label": "原地绕圈"}]
        ov = overview({"v2": base, "v3": v}, rows + lines)
        self.assertIn("草稿", ov["headline"])
        self.assertEqual(ov["drafts"], ["v3"])

    def test_load_dump_roundtrip_keeps_sources(self):
        raw = {
            "versions": {
                "v3": {
                    "label": "草稿",
                    "system": "x",
                    "status": "draft",
                    "parent": "v2",
                    "sources": [{"kind": "session", "ref": "repeated_effect", "label": "重复副作用"}],
                }
            }
        }
        vs = load_versions(raw)
        self.assertEqual(vs["v3"].sources[0].kind, "session")
        again = load_versions(dump_versions(vs))
        self.assertEqual(again["v3"].sources[0].ref, "repeated_effect")
        self.assertEqual(again["v3"].status, "draft")

    def test_legacy_two_field_version_still_loads(self):
        """老的 {label, note, system} 版本不该因为新增字段就变成非法。"""
        vs = load_versions({"versions": {"v1": {"label": "基线", "note": "n", "system": "s"}}})
        self.assertEqual(vs["v1"].status, "active")
        self.assertEqual(vs["v1"].sources, [])

    def test_stats_labels_each_kind_not_the_last_run(self):
        """每种残留病必须显示自己的名字。

        曾经这里读的是上一个 for 循环漏出来的循环变量 `r.get("label")`，
        于是四种病全显示成最后一条轨迹的病名（「过度转人工 6/4/3/2」）。
        这种 bug 不报错、不崩，只是把一张表变成废话，所以必须锁死。
        """
        v = mk_ver("v2", "x")
        rows = [
            {"version": "v2", "success": False, "kind": "over_escalation", "label": "过度转人工"},
            {"version": "v2", "success": False, "kind": "asked_instead_of_acted", "label": "只问不做"},
            {"version": "v2", "success": False, "kind": "no_action", "label": "没有动作"},
        ]
        kinds = stats(rows, {"v2": v})["v2"]["kinds"]
        labels = [k["label"] for k in kinds]
        self.assertEqual(labels, ["过度转人工", "只问不做", "没有动作"])
        self.assertEqual(len(set(labels)), len(labels))

    def test_stats_unknown_kind_falls_back_to_itself(self):
        v = mk_ver("v2", "x")
        rows = [{"version": "v2", "success": False, "kind": "brand_new_kind", "label": "随便写的"}]
        kinds = stats(rows, {"v2": v})["v2"]["kinds"]
        self.assertEqual(kinds[0]["label"], "brand_new_kind")


if __name__ == "__main__":
    unittest.main()
