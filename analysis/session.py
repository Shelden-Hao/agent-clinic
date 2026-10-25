"""多轮会话轨迹：单轮测试测不出的那一层。

**为什么这个模块必须存在**

单轮诊断有一个天然的盲区：它把每条轨迹当成一次独立的、自带全部信息的任务。
可真实的 Agent 不是这样工作的 —— 用户在第 1 轮说了订单号，第 2 轮就不说了；
用户第 2 轮改主意了；用户第 3 轮说「剩下的也退了吧」。

把这三轮分别当成三条独立轨迹去测，全部都很正常。合起来才暴露问题。

**方法论上的三条硬线**

1. 会话必须**共享世界状态**。第 1 轮退了款，第 2 轮查到的就是已退款状态。
   不共享状态的「多轮」只是把三条单轮轨迹粘在一起，除了编号以外没有新信息。

2. 跨轮病种必须**只依赖可见证据**：哪个 turn 的哪句话、哪次调用的哪个参数。
   不做「语义上好像不太连贯」这种判断 —— 那种判断没法反驳，也就没法修。

3. 单轮病种与跨轮病种**分开记，不互相覆盖**。
   一个 turn 可以既犯了「过度转人工」（单轮），又犯了「纠正被忽略」（跨轮）。
   把它们压成一个结论，就等于丢掉了其中一条线索。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .trace import Step, kind_label

# 跨轮病种。刻意与单轮病种分表 —— 它们的修复手段完全不同：
# 单轮病种改的是「这一次怎么决策」，跨轮病种改的是「记不记得住、跟不跟得上」。
SESSION_KINDS = {
    "correction_ignored": "纠正被忽略",
    "context_lost_across_turn": "跨轮丢失",
    "repeated_effect": "重复副作用",
}

SESSION_KIND_NOTE = {
    "correction_ignored": "用户明确改了目标，Agent 仍按旧目标执行。",
    "context_lost_across_turn": "用户在前面轮次给过的信息，后面轮次被重新索要。",
    "repeated_effect": "同一个副作用动作在会话里被重复执行，而用户没有要求第二次。",
}

# 用户在纠正。这些词之所以够用，是因为它们不是语义判断，而是**改口的语言标记**：
# 「不对」「不是这个」「我说的是」后面几乎必然跟着新目标。
_CORRECTION = re.compile(
    r"不对|不是这个|不是我要的|搞错|弄错|我说的是|我说过|改一下|改成|改退|换个|换一个|应该是|错了|重新|等等"
)

# Agent 在重新索要。同理只用语言标记，不猜语义。
_ASK_AGAIN = re.compile(
    r"请提供|麻烦提供|请告知|需要您提供|请您提供|能否提供|哪一个订单|哪个订单|订单号是|您的订单号|请确认订单"
)

# 有副作用的动作。查询重复十次只是浪费，写操作重复就是事故。
_WRITE_TOOLS = {"refund_order", "escalate", "cancel_order", "modify_order"}

# 用户明确要求「再来一次」的标记。出现这些词时，重复副作用就是用户要求的，不算病。
_ASK_MORE = re.compile(r"再|还|又|继续|第二|另一|剩下的|其余|全部")

# 结构化标识符：订单号、用户号、日期。只有这类东西才能作为「跨轮依赖」的硬证据。
_ID = re.compile(r"\b([A-Z]{1,4}[-_]?\d{3,})\b")


def _ids(text: str) -> set[str]:
    return set(_ID.findall(text or ""))


def _step_ids(step: Step) -> set[str]:
    out: set[str] = set()
    for v in (step.args or {}).values():
        if isinstance(v, str):
            out |= _ids(v)
        elif isinstance(v, (int, float)):
            out |= _ids(str(v))
    return out


def _key_args(step: Step) -> tuple:
    """副作用的身份：工具 + 它的标识类参数。金额不算身份 —— 分两次退不同金额仍是同一次副作用。"""
    items = sorted(
        (k, str(v))
        for k, v in (step.args or {}).items()
        if isinstance(v, str) and _ids(v)
    )
    return (step.tool, tuple(items))


@dataclass
class Turn:
    turn_index: int
    user: str
    steps: list[Step] = field(default_factory=list)
    final_content: str | None = None
    success: bool = True
    success_known: bool = True
    expect: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    fail_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    # 这一轮的失败在单轮视角下是什么病。由调用方逐 turn 跑 diagnose() 填进来，
    # 跨轮诊断本身不重算单轮结论 —— 两个层次各自出结论，互不覆盖。
    single_kind: str | None = None

    @property
    def tool_steps(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "tool_call"]

    @property
    def final_text(self) -> str:
        if self.final_content:
            return self.final_content
        for s in reversed(self.steps):
            if s.kind == "final":
                return s.content or ""
        return ""

    @property
    def grounded(self) -> bool:
        """这一轮的用户输入里带没带目标标识符。

        不带的话，agent 要做对就必须靠历史 —— 这就是「多轮才有的信息依赖」。
        """
        return bool(_ids(self.user))


@dataclass
class Session:
    session_id: str
    scenario_id: str
    prompt_version: str
    turns: list[Turn]
    model: str = "unknown"
    started_at: str = ""
    user_id: str = ""
    # 场景目标：这个会话原本要办成什么。用来判断「多轮下来有没有走远」。
    goal: str = ""

    @property
    def success(self) -> bool:
        return all(t.success for t in self.turns if t.success_known)

    @property
    def failed_turns(self) -> list[Turn]:
        return [t for t in self.turns if not t.success]

    @property
    def total_tokens(self) -> int:
        return sum(int(t.usage.get("total_tokens") or 0) for t in self.turns)

    @property
    def total_steps(self) -> int:
        return sum(len(t.tool_steps) for t in self.turns)

    @property
    def all_messages(self) -> str:
        """会话里所有用户说过的话，拼成一段。跨轮检测只在用户文本里找证据。"""
        return "\n".join(t.user for t in self.turns)


@dataclass
class SessionFinding:
    """一条跨轮发现。字段刻意与单轮 Finding 对齐，前端可以复用渲染逻辑。"""

    kind: str
    turn_index: int
    severity: int
    evidence: str
    confidence: float
    # 涉及的标识符：让「哪个订单、哪句话」可追溯
    ids: list[str] = field(default_factory=list)
    # 修复方向。跨轮病的修法通常是「在 system prompt 里加一条记忆规则」，
    # 和单轮病「加一条决策规则」不同，所以单独给。
    fix: str = ""

    @property
    def label(self) -> str:
        return SESSION_KINDS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "turn_index": self.turn_index,
            "severity": self.severity,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 2),
            "ids": self.ids,
            "fix": self.fix,
        }


# 每类跨轮病的处方。和单轮病的 patch 一样，目标是「能直接粘进 system prompt 的一句话」。
SESSION_PATCH = {
    "correction_ignored": (
        "用户在任何一轮修正目标（出现「不对」「不是这个」「我说的是」）时，"
        "必须立即放弃之前的目标，并在下一轮回复开头复述新的目标对象，确认改用后才继续执行。"
    ),
    "context_lost_across_turn": (
        "会话内已由用户提供的标识符（订单号、用户号）必须记入上下文，后续轮次直接复用，"
        "不得再次索要。只有在整个会话从未出现过时才可以追问。"
    ),
    "repeated_effect": (
        "执行有副作用的动作（退款、转人工、取消）前，先检查本会话是否已对同一对象执行过同类动作；"
        "已执行过则不得重复执行，应先向用户说明当前状态。"
    ),
}


def _finding_correction_ignored(turns: list[Turn], i: int) -> SessionFinding | None:
    """纠正被忽略：用户改口了，动作还指着旧目标。

    判据分两层，因为「没转向」和「转向了但转向的是写操作」严重程度不同：
    - 所有调用都没碰新目标 → 完全没跟上（查询层就没转）
    - 写操作落在旧目标上 → 已经造成实际损害
    """
    turn = turns[i]
    if i == 0 or not turn.tool_steps:
        return None
    if not _CORRECTION.search(turn.user):
        return None

    prev_ids: set[str] = set()
    for p in turns[:i]:
        prev_ids |= _ids(p.user)
    fresh = _ids(turn.user) - prev_ids
    if not fresh:
        # 改口了但没给出新标识符 —— 不算硬证据，无法断言「应该改成什么」
        return None

    used_all: set[str] = set()
    written: set[str] = set()
    for s in turn.tool_steps:
        used_all |= _step_ids(s)
        if s.tool in _WRITE_TOOLS:
            written |= _step_ids(s)

    if used_all & fresh:
        # 已经在查/在办新目标了，纠正生效
        return None

    old = sorted(used_all & prev_ids)
    damaged = bool(written & prev_ids)
    return SessionFinding(
        kind="correction_ignored",
        turn_index=turn.turn_index,
        severity=3 if damaged else 2,
        evidence=(
            f"第 {turn.turn_index} 轮用户明确改口（「{_first_match(_CORRECTION, turn.user)}」），"
            f"并把目标换成了 {'、'.join(sorted(fresh))}；"
            f"但 Agent 这一轮的动作全部落在 {'、'.join(old) or '旧目标'} 上，"
            f"整轮没有一次调用触及新目标"
            + ("，其中包含已生效的写操作。" if damaged else "（所幸还没动手写）。")
        ),
        confidence=0.9 if damaged else 0.8,
        ids=sorted(fresh | set(old)),
        fix=SESSION_PATCH["correction_ignored"],
    )


def _finding_context_lost(turns: list[Turn], i: int) -> SessionFinding | None:
    """跨轮丢失：前面给过的信息，后面重新要一遍。"""
    turn = turns[i]
    if i == 0:
        return None
    # 本轮用户自己给了标识符，就没有「该记住却没记住」这回事
    if _ids(turn.user):
        return None
    prev_ids: set[str] = set()
    for p in turns[:i]:
        prev_ids |= _ids(p.user)
    if not prev_ids:
        return None

    final = turn.final_text
    if not final or not _ASK_AGAIN.search(final):
        return None

    # 只有「追问之外没有任何实际行动」才算丢失。
    # 已经查完了再顺手确认一句，是谨慎，不是遗忘。
    acted = [s for s in turn.tool_steps if s.ok]
    if acted:
        return None

    return SessionFinding(
        kind="context_lost_across_turn",
        turn_index=turn.turn_index,
        severity=2,
        evidence=(
            f"{'、'.join(sorted(prev_ids))} 已在第 1~{i} 轮由用户给出，"
            f"第 {turn.turn_index} 轮用户没有重复；"
            f"Agent 却在这一轮末尾重新索要（「{_first_match(_ASK_AGAIN, final)}」），"
            "并且整轮没有做出任何工具调用。"
        ),
        confidence=0.82,
        ids=sorted(prev_ids),
        fix=SESSION_PATCH["context_lost_across_turn"],
    )


def _finding_repeated_effect(turns: list[Turn], i: int) -> SessionFinding | None:
    """重复副作用：同一个写操作在会话里做了两次，用户没要求第二次。"""
    turn = turns[i]
    if i == 0:
        return None
    if _ASK_MORE.search(turn.user):
        # 用户说了「再退剩下的」，重复就是被要求的
        return None

    now = {
        _key_args(s)
        for s in turn.tool_steps
        if s.tool in _WRITE_TOOLS and s.ok and _key_args(s)[1]
    }
    if not now:
        return None

    before: dict[tuple, int] = {}
    for p in turns[:i]:
        for s in p.tool_steps:
            if s.tool in _WRITE_TOOLS and s.ok and _key_args(s)[1]:
                before.setdefault(_key_args(s), p.turn_index)

    hits = [(k, before[k]) for k in now if k in before]
    if not hits:
        return None

    key, first_turn = hits[0]
    tool, args = key
    arg_text = "、".join(f"{k}={v}" for k, v in args)
    return SessionFinding(
        kind="repeated_effect",
        turn_index=turn.turn_index,
        severity=3,
        evidence=(
            f"{tool}（{arg_text}）已在第 {first_turn} 轮成功执行过，"
            f"第 {turn.turn_index} 轮又执行了一次；"
            "本轮用户的话里没有「再/还/剩下的」这类追加要求。"
        ),
        confidence=0.88,
        ids=sorted({v for _, v in args}),
        fix=SESSION_PATCH["repeated_effect"],
    )


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text or "")
    return m.group(0) if m else ""


_DETECTORS = (_finding_correction_ignored, _finding_context_lost, _finding_repeated_effect)


def diagnose_session(session: Session) -> list[SessionFinding]:
    """逐轮跑三类跨轮检测，返回按「发生早晚 + 严重度」排序的发现。

    排序原则与单轮诊断一致：早发生的错更可能是根因，晚发生的多半是它的后果。
    所以先比 turn_index，再比 severity。
    """
    out: list[SessionFinding] = []
    for i in range(len(session.turns)):
        for det in _DETECTORS:
            f = det(session.turns, i)
            if f is not None:
                out.append(f)
    out.sort(key=lambda f: (f.turn_index, -f.severity))
    return out


# ── 会话级度量：单轮视角 vs 会话视角 ─────────────────────────────────────────


def history_dependency(session: Session) -> list[dict[str, Any]]:
    """给每一轮打一个「历史对它是帮了还是害了」的标签。

    这是多轮视图真正值得存在的理由。它回答的不是「哪一轮错了」，
    而是「如果把这一轮单独拿出来测，能不能测出问题」。

    - `needs_history`：本轮用户没给标识符，要靠历史才能做对。
      单轮测试给它一条孤立输入，它必然做不对 —— 这类 turn 是单轮测试的盲区。
    - `harmed_by_history`：本轮失败，且失败原因是历史带来的（改口没跟上、重复副作用）。
      这类 turn 单独测反而会通过 —— 单轮测试会给它一个假绿灯。
    - `independent`：自带完整信息，也没被历史影响。单轮测它等于多轮测它。
    """
    kinds_by_turn: dict[int, list[str]] = {}
    for f in diagnose_session(session):
        kinds_by_turn.setdefault(f.turn_index, []).append(f.kind)

    harmful = {"correction_ignored", "repeated_effect"}
    out = []
    for t in session.turns:
        kinds = kinds_by_turn.get(t.turn_index, [])
        if set(kinds) & harmful:
            tag = "harmed_by_history"
        elif not t.grounded:
            tag = "needs_history"
        else:
            tag = "independent"
        out.append(
            {
                "turn_index": t.turn_index,
                "tag": tag,
                "success": t.success,
                "grounded": t.grounded,
                "cross_kinds": kinds,
                "single_kind": t.single_kind,
                "steps": len(t.tool_steps),
            }
        )
    return out


TAG_LABEL = {
    "needs_history": "离开历史就做不对 —— 单轮测试的盲区",
    "harmed_by_history": "被历史害了 —— 单轮测试会给它假绿灯",
    "independent": "自带完整信息 —— 单轮和多轮等价",
}


def summarize(sessions: list[Session]) -> dict[str, Any]:
    """会话级汇总 + 与单轮视角的对照。

    最要紧的一个数字是 `blind_spot_rate`：有多少「放在会话里会失败」的轮次，
    如果单独去测是测不出来的。这个数字就是「只做单轮测试」的漏检率。
    """
    turns_all = [t for s in sessions for t in s.turns]
    total_turns = len(turns_all)
    failed_turns = [t for t in turns_all if not t.success]
    ok_sessions = [s for s in sessions if s.success]

    tags = [row for s in sessions for row in history_dependency(s)]
    harmed = [r for r in tags if r["tag"] == "harmed_by_history"]
    needs = [r for r in tags if r["tag"] == "needs_history"]

    # 漏检 = 单独测会过、合起来测会挂的轮次
    blind = [r for r in harmed if not r["success"]]
    blind_rate = (len(blind) / len(failed_turns)) if failed_turns else 0.0

    kind_count: dict[str, int] = {}
    for s in sessions:
        for f in diagnose_session(s):
            kind_count[f.kind] = kind_count.get(f.kind, 0) + 1

    # 首轮与末轮的失败率：多轮最常见的形态是「越聊越差」
    def turn_fail_rate(idx: int) -> dict[str, Any]:
        pool = [s.turns[idx] for s in sessions if len(s.turns) > idx]
        bad = [t for t in pool if not t.success]
        return {
            "turn": idx + 1,
            "n": len(pool),
            "failed": len(bad),
            "rate": round(len(bad) / len(pool), 3) if pool else 0.0,
        }

    depth = max((len(s.turns) for s in sessions), default=0)
    progression = [turn_fail_rate(i) for i in range(depth)]

    return {
        "sessions": len(sessions),
        "turns": total_turns,
        "session_success": len(ok_sessions),
        "session_success_rate": round(len(ok_sessions) / len(sessions), 3) if sessions else 0.0,
        "turn_success_rate": round((total_turns - len(failed_turns)) / total_turns, 3) if total_turns else 0.0,
        "failed_turns": len(failed_turns),
        "blind_spot_turns": len(blind),
        "blind_spot_rate": round(blind_rate, 3),
        "needs_history_turns": len(needs),
        "harmed_turns": len(harmed),
        "progression": progression,
        "kinds": [
            {"kind": k, "label": SESSION_KINDS.get(k, k), "count": v, "note": SESSION_KIND_NOTE.get(k, "")}
            for k, v in sorted(kind_count.items(), key=lambda x: -x[1])
        ],
        "tags": [
            {"tag": k, "label": v, "count": sum(1 for r in tags if r["tag"] == k)}
            for k, v in TAG_LABEL.items()
        ],
        "caveat": (
            "会话级结论的样本量比单轮更小（一个会话只有 2~4 轮）。"
            "这里给的是「哪一类交互会出问题」，不是「出问题的概率是多少」。"
        ),
    }


# ── 反序列化 ────────────────────────────────────────────────────────────────


def _step_from(d: dict[str, Any], index: int) -> Step:
    return Step(
        index=int(d.get("i", index)),
        kind=d.get("kind", "tool_call"),
        tool=d.get("tool"),
        args=d.get("args") or {},
        observation=d.get("observation"),
        ok=bool(d.get("ok", True)),
        error=d.get("error"),
        content=d.get("content"),
        latency_ms=int(d.get("latency_ms") or 0),
        tokens=int(d.get("tokens") or 0),
    )


def turn_from_dict(d: dict[str, Any], index: int) -> Turn:
    steps = [_step_from(s, i + 1) for i, s in enumerate(d.get("steps") or [])]
    return Turn(
        turn_index=int(d.get("turn_index", index)),
        user=d.get("user") or "",
        steps=steps,
        final_content=d.get("final_content"),
        success=bool(d.get("success")),
        success_known=bool(d.get("success_known", True)),
        expect=d.get("expect") or {},
        actual=d.get("actual") or {},
        fail_reason=d.get("fail_reason"),
        usage=d.get("usage") or {},
    )


def session_from_dict(d: dict[str, Any]) -> Session:
    return Session(
        session_id=d["session_id"],
        scenario_id=d.get("scenario_id") or d["session_id"].split("-")[0],
        prompt_version=d.get("prompt_version", "v1"),
        turns=[turn_from_dict(t, i + 1) for i, t in enumerate(d.get("turns") or [])],
        model=d.get("model", "unknown"),
        started_at=d.get("started_at", ""),
        user_id=d.get("user_id", ""),
        goal=d.get("goal", ""),
    )


def single_kind_label(turn: Turn) -> str:
    """某一轮在单轮视角下的病名。跨轮视图要同时显示两层结论。"""
    if turn.success:
        return "通过"
    return kind_label(turn.single_kind or "unknown")
