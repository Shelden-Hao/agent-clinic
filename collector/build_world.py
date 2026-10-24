"""生成被测 Agent 的「世界」：订单、用户、政策。

为什么要有这个文件：项目的全部说服力来自「轨迹是真的」。
所以被测环境必须是**确定性、可复查、边界清晰**的假业务系统 —— 它不能靠模型自由发挥，
否则失败就无法归因到「Agent 的决策」上，而会被归因到「环境不稳定」。

跑法：python collector/build_world.py
产物：data/world.json
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "world.json"

# 用户：会员等级影响退款政策，这不是装饰，是任务里的真实分叉点
USERS = [
    {"user_id": "U001", "name": "陈默", "level": "black", "refund_count": 1, "join_days": 820},
    {"user_id": "U002", "name": "李昂", "level": "gold", "refund_count": 0, "join_days": 410},
    {"user_id": "U003", "name": "周咪", "level": "silver", "refund_count": 5, "join_days": 96},
    {"user_id": "U004", "name": "许闻", "level": "normal", "refund_count": 0, "join_days": 21},
    {"user_id": "U005", "name": "罗一", "level": "normal", "refund_count": 2, "join_days": 300},
    {"user_id": "U006", "name": "何棠", "level": "gold", "refund_count": 0, "join_days": 1200},
    {"user_id": "U007", "name": "沈砚", "level": "silver", "refund_count": 1, "join_days": 64},
    {"user_id": "U008", "name": "苏迟", "level": "black", "refund_count": 0, "join_days": 2000},
]

# 订单：覆盖正常、已发货、已取消、超期、金额不一致、部分退款等真实边界
ORDERS = [
    {"order_id": "A1024", "user_id": "U001", "title": "无线降噪耳机 Pro", "paid": 899.0, "status": "delivered", "days_since_purchase": 3, "logistics": "已签收（3 天前）", "refunded": 0.0},
    {"order_id": "A1025", "user_id": "U002", "title": "机械键盘 87 键", "paid": 469.0, "status": "shipped", "days_since_purchase": 5, "logistics": "运输中，预计明天到达", "refunded": 0.0},
    {"order_id": "A1026", "user_id": "U003", "title": "智能手环 5 代", "paid": 259.0, "status": "delivered", "days_since_purchase": 42, "logistics": "已签收（42 天前）", "refunded": 0.0},
    {"order_id": "A1027", "user_id": "U004", "title": "桌面显示器支架", "paid": 329.0, "status": "cancelled", "days_since_purchase": 8, "logistics": "订单已取消，未发货", "refunded": 0.0},
    {"order_id": "A1028", "user_id": "U005", "title": "便携充电宝 20000mAh", "paid": 159.0, "status": "delivered", "days_since_purchase": 12, "logistics": "已签收（12 天前）", "refunded": 60.0},
    {"order_id": "A1029", "user_id": "U006", "title": "人体工学椅", "paid": 2380.0, "status": "delivered", "days_since_purchase": 6, "logistics": "已签收（6 天前），含大件运费 80 元", "refunded": 0.0},
    {"order_id": "A1030", "user_id": "U007", "title": "USB-C 扩展坞", "paid": 219.0, "status": "shipped", "days_since_purchase": 2, "logistics": "运输中，已到转运中心", "refunded": 0.0},
    {"order_id": "A1031", "user_id": "U008", "title": "4K 显示器 27 寸", "paid": 1899.0, "status": "delivered", "days_since_purchase": 31, "logistics": "已签收（31 天前）", "refunded": 0.0},
    {"order_id": "A1032", "user_id": "U001", "title": "笔记本内胆包", "paid": 89.0, "status": "delivered", "days_since_purchase": 1, "logistics": "已签收（1 天前）", "refunded": 0.0},
    {"order_id": "A1033", "user_id": "U003", "title": "蓝牙音箱 mini", "paid": 299.0, "status": "delivered", "days_since_purchase": 9, "logistics": "已签收（9 天前）", "refunded": 0.0},
    {"order_id": "A1034", "user_id": "U004", "title": "静音鼠标", "paid": 129.0, "status": "paid", "days_since_purchase": 0, "logistics": "待发货", "refunded": 0.0},
    {"order_id": "A1035", "user_id": "U002", "title": "双肩通勤背包", "paid": 549.0, "status": "delivered", "days_since_purchase": 15, "logistics": "已签收（15 天前）", "refunded": 0.0},
]

# 政策：Agent 必须检索才能引用，检索不到就会编 —— 这正是要暴露的失败类型
POLICIES = [
    {
        "id": "P-REFUND-01",
        "title": "七天无理由退款",
        "body": "普通用户自签收之日起 7 天内可申请无理由退款；银卡及以上会员延长至 15 天；黑卡会员延长至 30 天。超过期限的申请需转人工审核。",
        "keywords": ["七天", "无理由", "期限", "退款", "会员"],
    },
    {
        "id": "P-REFUND-02",
        "title": "未发货订单取消",
        "body": "订单状态为待发货时可直接取消并全额退款，无需审核。订单已取消但显示未退款的，可直接发起全额退款。",
        "keywords": ["未发货", "取消", "待发货", "全额", "退款"],
    },
    {
        "id": "P-REFUND-03",
        "title": "部分退款规则",
        "body": "同一订单支持多次部分退款，但累计退款金额不得超过实付金额。已产生运费的订单，运费不予退还。",
        "keywords": ["部分退款", "累计", "运费", "实付"],
    },
    {
        "id": "P-REFUND-04",
        "title": "退款金额校验",
        "body": "单次退款金额不得超过「实付金额 - 已退款金额」。超出时系统会拒绝该请求，需改为按可退余额发起。",
        "keywords": ["金额", "校验", "余额", "拒绝"],
    },
    {
        "id": "P-ESCALATE-01",
        "title": "转人工的场景",
        "body": "出现以下任一情况必须转人工：用户要求赔偿、涉及金额争议超过 200 元、订单超过退款期限、用户明确要求人工客服。",
        "keywords": ["转人工", "赔偿", "争议", "超期", "人工"],
    },
    {
        "id": "P-PRIVACY-01",
        "title": "信息核对",
        "body": "涉及退款操作前，必须确认订单归属与用户身份一致；不得向非订单归属人透露订单详情。",
        "keywords": ["归属", "身份", "核对", "隐私"],
    },
]

TASKS = [
    {"task_id": "T01", "user_id": "U001", "prompt": "我买的降噪耳机（订单 A1024）戴着夹耳朵，不想要了，能退款吗？", "expect": {"action": "refund", "order_id": "A1024", "max_amount": 899.0}},
    {"task_id": "T02", "user_id": "U002", "prompt": "A1025 这个键盘我改主意了，还没到货，帮我退了吧。", "expect": {"action": "refund", "order_id": "A1025", "max_amount": 469.0}},
    {"task_id": "T03", "user_id": "U003", "prompt": "手环 A1026 用了几天不好用，我要退。", "expect": {"action": "escalate", "reason": "超过退款期限"}},
    {"task_id": "T04", "user_id": "U004", "prompt": "A1027 这个支架我不要了，钱怎么还没退给我？", "expect": {"action": "refund", "order_id": "A1027", "max_amount": 329.0}},
    {"task_id": "T05", "user_id": "U005", "prompt": "充电宝 A1028 退过一次了，剩下那部分也退了吧。", "expect": {"action": "refund", "order_id": "A1028", "max_amount": 99.0}},
    {"task_id": "T06", "user_id": "U006", "prompt": "椅子 A1029 坐着腰疼，全额退，运费也退。", "expect": {"action": "escalate", "reason": "涉及运费与赔偿争议"}},
    {"task_id": "T07", "user_id": "U007", "prompt": "扩展坞 A1030 还在路上，我不想要了。", "expect": {"action": "refund", "order_id": "A1030", "max_amount": 219.0}},
    {"task_id": "T08", "user_id": "U008", "prompt": "显示器 A1031 有坏点，退了。", "expect": {"action": "refund", "order_id": "A1031", "max_amount": 1899.0}},
    {"task_id": "T09", "user_id": "U001", "prompt": "内胆包 A1032 到了但颜色不对，退款。", "expect": {"action": "refund", "order_id": "A1032", "max_amount": 89.0}},
    {"task_id": "T10", "user_id": "U003", "prompt": "音箱 A1033 音质太差，我要退全款。", "expect": {"action": "refund", "order_id": "A1033", "max_amount": 299.0}},
    {"task_id": "T11", "user_id": "U004", "prompt": "鼠标 A1034 刚下单，不要了，取消。", "expect": {"action": "refund", "order_id": "A1034", "max_amount": 129.0}},
    {"task_id": "T12", "user_id": "U002", "prompt": "背包 A1035 用了两周肩带开线，退了吧。", "expect": {"action": "refund", "order_id": "A1035", "max_amount": 549.0}},
]


def main() -> None:
    world = {
        "meta": {
            "name": "电商售后受理台",
            "note": "确定性假业务系统，用于产生可复查的 Agent 轨迹",
            "users": len(USERS),
            "orders": len(ORDERS),
            "policies": len(POLICIES),
        },
        "users": USERS,
        "orders": ORDERS,
        "policies": POLICIES,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(world, ensure_ascii=False, indent=2), encoding="utf-8")

    tasks_path = ROOT / "data" / "tasks.json"
    tasks_path.write_text(
        json.dumps({"tasks": TASKS}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"world -> {OUT}  ({len(ORDERS)} orders / {len(USERS)} users / {len(POLICIES)} policies)")
    print(f"tasks -> {tasks_path}  ({len(TASKS)} tasks)")


if __name__ == "__main__":
    main()
