"""轨迹数据模型。

设计要点：轨迹是**唯一事实来源**，分析层不允许回头再问模型。
所有结论都必须能追溯到某个具体 step 的某个字段，这是「可解释」而不是「玄学」的物理基础。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

StepKind = Literal["tool_call", "final", "plan"]

# 失败的八种性质。宁可多分一类，也不要塞进 unknown 里蒙混过去。
ErrorKind = Literal[
    "tool_error",             # 工具抛错 / 业务拒绝
    "bad_argument",           # 参数缺失、类型错、取值非法
    "wrong_tool",             # 选了不该选的工具，或该用的工具始终没被调用
    "hallucinated_fact",      # 结论里出现的数据在观测中不存在
    "loop",                   # 三步内重复同一动作
    "context_lost",           # 用户已给出的信息被重新询问
    "premature_answer",       # 关键信息未取就直接作答
    "escalation_missed",      # 该转人工却自行处理
    "over_escalation",        # 该自己处理却甩给人工
    "asked_instead_of_acted", # 把「执行」变成了「反问用户」
    "promised_but_not_done",  # 声称已办理，实际没调用工具
    "no_action",              # 既没执行也没转人工也没追问
    "unknown",
]

_KIND_LABEL = {
    "tool_error": "工具报错",
    "bad_argument": "参数错误",
    "wrong_tool": "工具选错",
    "hallucinated_fact": "事实幻觉",
    "loop": "原地绕圈",
    "context_lost": "上下文丢失",
    "premature_answer": "过早作答",
    "escalation_missed": "该转人工没转",
    "over_escalation": "过度转人工",
    "asked_instead_of_acted": "只问不做",
    "promised_but_not_done": "说了却没做",
    "no_action": "没有动作",
    "unknown": "未归类",
}


def kind_label(kind: str) -> str:
    return _KIND_LABEL.get(kind, kind)


@dataclass
class Step:
    index: int
    kind: StepKind
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    observation: Any = None
    ok: bool = True
    error: str | None = None
    content: str | None = None
    latency_ms: int = 0
    tokens: int = 0

    @property
    def signature(self) -> str:
        """用于对齐与去重的决策签名：工具名 + 参数键（不含值，值会被最小化改写）。"""
        if self.kind != "tool_call":
            return self.kind
        keys = ",".join(sorted(self.args.keys()))
        return f"{self.tool}({keys})"

    @property
    def full_signature(self) -> str:
        if self.kind != "tool_call":
            return f"{self.kind}:{(self.content or '')[:60]}"
        vals = ";".join(f"{k}={self.args[k]}" for k in sorted(self.args))
        return f"{self.tool}({vals})"

    @property
    def observation_text(self) -> str:
        if self.observation is None:
            return ""
        if isinstance(self.observation, str):
            return self.observation
        return str(self.observation)


@dataclass
class Run:
    run_id: str
    task_id: str
    prompt_version: str
    task_prompt: str
    steps: list[Step]
    success: bool
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    fail_reason: str | None = None
    model: str = "unknown"
    started_at: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def tool_steps(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "tool_call"]

    @property
    def final(self) -> Step | None:
        for s in reversed(self.steps):
            if s.kind == "final":
                return s
        return None

    @property
    def tools_used(self) -> list[str]:
        return [s.tool or "" for s in self.tool_steps]


def step_from_dict(d: dict[str, Any], index: int) -> Step:
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


def run_from_dict(d: dict[str, Any]) -> Run:
    steps = [step_from_dict(s, i + 1) for i, s in enumerate(d.get("steps") or [])]
    return Run(
        run_id=d["run_id"],
        task_id=d["task_id"],
        prompt_version=d.get("prompt_version", "v1"),
        task_prompt=d.get("task_prompt", ""),
        steps=steps,
        success=bool(d.get("success")),
        expected=d.get("expected") or {},
        actual=d.get("actual") or {},
        fail_reason=d.get("fail_reason"),
        model=d.get("model", "unknown"),
        started_at=d.get("started_at", ""),
        usage=d.get("usage") or {},
    )
