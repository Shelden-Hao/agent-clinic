"""Prompt 版本管理：让「改 prompt」这件事有出处、有对照、有血缘。

**为什么一个诊断工具要管 prompt 版本**

诊断的终点不是「你这里有 4 类病」，而是「下一版该怎么写」。
如果版本管理只做成一个文本框，那这个项目就停在了一句话上：
用户拿到规则草稿，得自己打开编辑器粘贴、自己记住改了什么、自己对比效果。

于是诊断给出的证据、复核写下的理由、规则草稿描述的对策，
到「真正落进 prompt 的那一行」之间就断了。

这个模块把那一段接上：**每一条新增规则都必须带来源**。
来源可以是某个失败模式、某条复核改判、某个跨轮场景。
没有来源的改动也能做，但会被显式标成 `manual`，界面上与「有据可查」的改动分开显示。

**一条刻意的限制**：这里不做「自动生成最优 prompt」。
文本质量不是一个规则引擎能保证的事，声称能自动优化只会让输出不可追责。
它做的是：把该写什么摆出来，把为什么写摆出来，剩下的编辑动作留给人。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from analysis.trace import kind_label

# 有据可查的来源类型。`manual` 不在其中 —— 它是「没来源」的代名词，
# 所以单独处理，在界面上也要用不同颜色标出来。
SOURCE_KINDS = {
    "pattern": "失败模式",
    "review": "人工复核改判",
    "session": "跨轮场景",
    "seed": "初始版本",
}

STATUS_LABEL = {
    "active": "已投入使用",
    "draft": "草稿",
    "archived": "已归档",
}


@dataclass
class Source:
    """一条规则是从哪来的。没有这个字段的改动，就是拍脑袋改的。"""

    kind: str
    ref: str  # 病种 key / run_id / scenario_id
    label: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "kind_label": SOURCE_KINDS.get(self.kind, self.kind),
            "ref": self.ref,
            "label": self.label,
            "evidence": self.evidence,
        }


@dataclass
class Version:
    vid: str
    label: str
    system: str
    note: str = ""
    parent: str | None = None
    created_at: str = ""
    status: str = "active"
    changelog: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.vid,
            "label": self.label,
            "system": self.system,
            "note": self.note,
            "parent": self.parent,
            "created_at": self.created_at,
            "status": self.status,
            "status_label": STATUS_LABEL.get(self.status, self.status),
            "changelog": self.changelog,
            "sources": [s.to_dict() for s in self.sources],
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_versions(raw: dict | None) -> dict[str, Version]:
    """把 prompts.json 读成版本表。

    兼容两种形态：老的两字段版本（label/note/system）与新的完整版本。
    老数据不该因为新增了字段就变成非法 —— 迁移成本会转嫁给用户。
    """
    out: dict[str, Version] = {}
    if not isinstance(raw, dict):
        return out
    for vid, v in (raw.get("versions") or {}).items():
        if not isinstance(v, dict):
            continue
        out[vid] = Version(
            vid=vid,
            label=v.get("label") or vid,
            system=v.get("system") or "",
            note=v.get("note") or "",
            parent=v.get("parent"),
            created_at=v.get("created_at") or "",
            status=v.get("status") or "active",
            changelog=list(v.get("changelog") or []),
            sources=[
                Source(
                    kind=s.get("kind", "manual"),
                    ref=s.get("ref", ""),
                    label=s.get("label", ""),
                    evidence=s.get("evidence", ""),
                )
                for s in (v.get("sources") or [])
                if isinstance(s, dict)
            ],
        )
    return out


def dump_versions(versions: dict[str, Version]) -> dict[str, Any]:
    """写回磁盘。保持 `{"versions": {...}}` 外壳，老读者不受影响。"""
    return {
        "versions": {
            vid: {
                "label": v.label,
                "note": v.note,
                "system": v.system,
                "parent": v.parent,
                "created_at": v.created_at,
                "status": v.status,
                "changelog": v.changelog,
                "sources": [s.to_dict() for s in v.sources],
            }
            for vid, v in versions.items()
        }
    }


# ── 血缘 ────────────────────────────────────────────────────────────────────


def lineage(versions: dict[str, Version], vid: str) -> list[str]:
    """从根到当前版本的路径。环状 parent 会在这里被截断 —— 手改 JSON 造出来的环不该把服务拖死。"""
    chain: list[str] = []
    seen: set[str] = set()
    cur: str | None = vid
    while cur and cur in versions and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = versions[cur].parent
    return list(reversed(chain))


def next_id(versions: dict[str, Version], prefix: str = "v") -> str:
    """下一个版本号。只认 `v<数字>`，别的手工命名不参与计算。"""
    nums = [
        int(v.vid[len(prefix) :])
        for v in versions.values()
        if v.vid.startswith(prefix) and v.vid[len(prefix) :].isdigit()
    ]
    return f"{prefix}{(max(nums) + 1) if nums else 1}"


# ── 行级 diff ───────────────────────────────────────────────────────────────


def _segments(system: str) -> list[dict[str, Any]]:
    """把 prompt 切成对齐用的最小单位：一句一条。

    **为什么不用行**：v1 的正文是一整段话，v2 是五条编号规则。
    按行对齐只会得到「6 行全部被替换」，读者看不出「到底加了哪几条规矩」。
    按句对齐才能显示成「1 句变成 6 句」，而那个 1→6 就是这次修复本身。

    切分只用 `。！？` 与换行，不用 `；` ——
    分号在中文明细里是并列项，拆开会让一条规则碎成几段。
    """
    out: list[dict[str, Any]] = []
    for line_no, line in enumerate((system or "").splitlines(), start=1):
        text = line.rstrip()
        if not text.strip():
            out.append({"line": line_no, "text": "", "blank": True})
            continue
        # 在句末标点之后切开，标点留在前一句里
        buf = ""
        for ch in text:
            buf += ch
            if ch in "。！？":
                out.append({"line": line_no, "text": buf.strip(), "blank": False})
                buf = ""
        if buf.strip():
            out.append({"line": line_no, "text": buf.strip(), "blank": False})
    # 全空行没有信息量，但对齐时它是「段落分隔」，保留首尾各一个即可
    while out and out[0]["blank"]:
        out.pop(0)
    while out and out[-1]["blank"]:
        out.pop()
    return out


def diff(a: Version, b: Version) -> dict[str, Any]:
    """把两个版本的正文按句对齐。

    每一行输出都带 `a_line` / `b_line`，即该句在原文里的行号 ——
    句级对齐提供了精度，行号负责让人能回到原文定位。
    """
    aseg = _segments(a.system)
    bseg = _segments(b.system)
    al = [s["text"] if not s["blank"] else "\u200b" for s in aseg]
    bl = [s["text"] if not s["blank"] else "\u200b" for s in bseg]

    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    rows: list[dict[str, Any]] = []
    added = removed = changed = 0

    def row(op: str, ai: int | None, bj: int | None) -> dict[str, Any]:
        return {
            "op": op,
            "a_line": aseg[ai]["line"] if ai is not None else None,
            "b_line": bseg[bj]["line"] if bj is not None else None,
            "a": (aseg[ai]["text"] if ai is not None else None),
            "b": (bseg[bj]["text"] if bj is not None else None),
        }

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(row("equal", i1 + k, j1 + k))
        elif tag == "insert":
            for k in range(j2 - j1):
                added += 1
                rows.append(row("insert", None, j1 + k))
        elif tag == "delete":
            for k in range(i2 - i1):
                removed += 1
                rows.append(row("delete", i1 + k, None))
        else:  # replace
            span = max(i2 - i1, j2 - j1)
            for k in range(span):
                ai = i1 + k if i1 + k < i2 else None
                bj = j1 + k if j1 + k < j2 else None
                changed += 1
                rows.append(row("replace", ai, bj))

    lines_a = len((a.system or "").splitlines())
    lines_b = len((b.system or "").splitlines())
    return {
        "a": a.vid,
        "b": b.vid,
        "a_label": a.label,
        "b_label": b.label,
        "stats": {
            "added": added,
            "removed": removed,
            "changed": changed,
            "lines_a": lines_a,
            "lines_b": lines_b,
            "sentences_a": len(aseg),
            "sentences_b": len(bseg),
        },
        "rows": rows,
        "same": not (added or removed or changed),
        "note": (
            "按句对齐，每句带原文行号。用句子而不是用行，是因为 v1 是一整段、v2 是五条规则 —— "
            "行级对齐只会说「全改了」，句级对齐才能说出「1 句变成 6 句」。"
            "切分只认句末标点，不拆分号：分号在中文明细里是并列项，拆开会把一条规则碎成几段。"
        ),
    }


# ── 从诊断证据生成本版 ──────────────────────────────────────────────────────


def compose_patch(
    base: Version,
    items: Iterable[tuple[str, str, Source]],
    new_id: str,
    label: str = "",
    note: str = "",
) -> Version:
    """在基线版末尾追加一组规则，生成草稿版本。

    `items` 每项是 (规则正文, 分段标题, 来源)。
    生成的文本里，每条规则前面带一个可追溯标记 `[来源: ...]` ——
    这是刻意的冗余：prompt 是给模型看的，但这一行是给人看的。
    等这个版本验证通过、准备定稿时，删掉这些标记即可，判断已经做完了。

    **不做的事**：不重写基线版的任何一行。基线是被验证过的，
    自动改写会让「修好的是新规则还是被改坏的旧规则」永远说不清。
    """
    # 先把可迭代对象定下来，再数长度。
    # 生成器只能消费一次，若在上面那句 f-string 里先 len(list(items))，
    # 下面再 list(items) 就会拿到空列表 —— 这种 bug 不会报错，只会静默少写规则。
    entries = list(items)

    lines = [base.system.rstrip()] if base.system.strip() else []
    lines.append("")
    lines.append(f"# 追加规程（依据 {base.vid} 的诊断结论，{len(entries)} 条）")

    sources: list[Source] = []
    for body, header, src in entries:
        lines.append(f"[来源:{src.kind}:{src.ref}] {header}")
        lines.append(f"- {body}")
        sources.append(src)

    return Version(
        vid=new_id,
        label=label or f"{base.label} + 修复规程",
        system="\n".join(lines),
        note=note or f"由 {base.vid} 的诊断结论追加生成，尚未采集轨迹验证。",
        parent=base.vid,
        created_at=now_iso(),
        # 生成出来的是草稿。没有轨迹验证过的版本，不能和验证过的版本并列展示通过率。
        status="draft",
        changelog=[f"在 {base.vid} 基础上追加 {len(sources)} 条规程，每条带来源标记"],
        sources=sources,
    )


# ── 版本体检 ────────────────────────────────────────────────────────────────


def stats(runs: list[dict], versions: dict[str, Version]) -> dict[str, Any]:
    """每个版本关联了多少轨迹、通过率多少、还剩哪些病。

    `runs` 是 report.json 里的轻量索引（含 version / success / kind）。
    没有轨迹的版本（通常是草稿）通过率返回 None，不返回 0 ——
    「没测过」和「测了全挂」是两件事，把前者显示成 0% 会误导排期。
    """
    out: dict[str, dict[str, Any]] = {}
    for vid, v in versions.items():
        mine = [r for r in runs if r.get("version") == vid]
        ok = [r for r in mine if r.get("success")]
        kinds: dict[str, int] = {}
        for r in mine:
            if not r.get("success"):
                k = r.get("kind") or "unknown"
                kinds[k] = kinds.get(k, 0) + 1
        out[vid] = {
            "runs": len(mine),
            "ok": len(ok),
            "fail_rate": round(1 - len(ok) / len(mine), 3) if mine else None,
            # 标签必须由 kind 查表得到，不能顺手读循环变量 r.get("label")。
            # 那个 r 是上一个 for 循环漏出来的最后一个元素，会让每种病的标签
            # 都变成最后一条轨迹的标签 —— 曾经在这一页上显示成
            # 「过度转人工 6 / 过度转人工 4 / 过度转人工 3」，四种病一个名字。
            "kinds": [
                {"kind": k, "label": kind_label(k), "count": c}
                for k, c in sorted(kinds.items(), key=lambda x: -x[1])
            ],
            "measured": bool(mine),
        }
    return out


def overview(versions: dict[str, Version], runs: list[dict]) -> dict[str, Any]:
    """版本总览：列表 + 血缘 + 体检 + 一句话结论。"""
    st = stats(runs, versions)
    rows = []
    for vid, v in versions.items():
        rows.append(
            {
                **v.to_dict(),
                "lineage": lineage(versions, vid),
                "stats": st[vid],
            }
        )
    # 已投入使用的排前面，草稿其次，归档最后；同状态内按版本号倒序
    order = {"active": 0, "draft": 1, "archived": 2}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["id"]), reverse=False)

    drafts = [r for r in rows if r["status"] == "draft"]
    unmeasured = [r for r in rows if not r["stats"]["measured"]]
    measured = [r for r in rows if r["stats"]["measured"]]
    best = min(measured, key=lambda r: r["stats"]["fail_rate"]) if measured else None

    if not measured:
        head = "还没有任何版本被轨迹验证过。"
    else:
        bits = [f"{len(measured)} 个版本有轨迹证据"]
        if len(measured) >= 2:
            bits.append(
                f"其中失败率最低的是 {best['id']}（{best['stats']['fail_rate']:.1%}）"
            )
        if drafts:
            bits.append(f"{len(drafts)} 个是未验证的草稿")
        if len(unmeasured) > len(drafts):
            bits.append(f"另有 {len(unmeasured) - len(drafts)} 个版本没有轨迹")
        head = "，".join(bits) + "。"

    return {
        "versions": rows,
        "count": len(rows),
        "active": [r["id"] for r in rows if r["status"] == "active"],
        "drafts": [r["id"] for r in rows if r["status"] == "draft"],
        "headline": head,
        "caveat": (
            "草稿版本的失败率是空的，不是 0% —— 它还没跑过轨迹。"
            "把「没测」显示成「0%」会让人以为它是最好的版本。"
        ),
    }


def apply_edit(
    versions: dict[str, Version],
    vid: str,
    *,
    system: str | None = None,
    label: str | None = None,
    note: str | None = None,
    status: str | None = None,
    changelog_entry: str | None = None,
) -> Version:
    """就地编辑一个版本。每次编辑都会往 changelog 里落一条 —— 版本表的价值在于能回看，不是能改。"""
    v = versions[vid]
    before = (v.system or "").strip()
    if system is not None:
        v.system = system
    if label is not None:
        v.label = label
    if note is not None:
        v.note = note
    if status is not None:
        if status not in STATUS_LABEL:
            raise ValueError(f"未知状态 {status}，可选：{'、'.join(STATUS_LABEL)}")
        v.status = status
    if changelog_entry:
        v.changelog.append(changelog_entry)
    elif system is not None and (v.system or "").strip() != before:
        v.changelog.append(f"{now_iso()} 手工编辑正文")
    return v
