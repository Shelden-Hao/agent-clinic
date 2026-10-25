"""FastAPI 应用装配。

设计原则：
- **运行时零外部依赖**：所有分析都基于已采集并入库的轨迹快照，服务启动不调用任何模型 API。
  这样线上演示是确定性的、免费的和可复现的 —— 也是这个项目能被别人反复打开的前提。
- **算法与 HTTP 分离**：`analysis/` 是纯函数库，可独立单测；这里只负责取数、调用、序列化。
- **采集与诊断分离**：`/api/lab/*` 只负责把真实轨迹收进来；诊断永远读同一份数据，不重新问模型。
- **接入是无状态的**：`/api/intake` 现场解析现场诊断，不写任何东西。
  只有用户明确点「存进回归集」，才会落盘 —— 陌生人的轨迹不该在服务器上留痕。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.align import align_runs  # noqa: E402
from analysis.cluster import patch_for  # noqa: E402
from analysis.diagnose import diagnose  # noqa: E402
from analysis.evaluate import EvalConfig, evaluate  # noqa: E402
from analysis.ingest import FORMATS, ingest, ingest_jsonl, roundtrip_stats  # noqa: E402
from analysis.minimize import minimize  # noqa: E402
from analysis.promptlib import (  # noqa: E402
    Source,
    Version,
    apply_edit,
    compose_patch,
    diff as prompt_diff,
    dump_versions,
    lineage,
    load_versions,
    next_id,
    overview as prompt_overview,
)
from analysis.regress import verify  # noqa: E402
from analysis.review import build_queue, verify_review  # noqa: E402
from analysis.session import (  # noqa: E402
    SESSION_KINDS,
    SESSION_PATCH,
    diagnose_session,
    history_dependency,
    session_from_dict,
    summarize as summarize_sessions,
)
from analysis.trace import kind_label, run_from_dict  # noqa: E402

DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "server" / "static"
RUNTIME_DIR = ROOT / "runtime"
SAMPLES_DIR = DATA_DIR / "samples"
SDK_DIR = ROOT / "sdk"
REGRESSION = DATA_DIR / "regression.json"
REVIEWS = DATA_DIR / "reviews.json"
PROMPTS = DATA_DIR / "prompts.json"

VERSION = "0.5.0"
MAX_INTAKE_BYTES = 4 * 1024 * 1024
MAX_INTAKE_RUNS = 60

# 实时上报的入口密钥。默认值只够本地开发用，线上由环境变量覆盖。
# 不做成完全开放：一个能写数据的公开接口，几小时内就会变成垃圾场。
CLINIC_KEY = os.environ.get("CLINIC_KEY", "dev")
# 每分钟允许的上报条数。可观测性数据不值得为它开无限配额。
STREAM_RATE_LIMIT = int(os.environ.get("STREAM_RATE_LIMIT", "900"))

# 云服务激活时拿到的公开配置（endpoint / publishableKey 可进前端，服务端密钥不会出现）
PUBLIC_CONFIG = {
    "endpoint": "https://agent-clinic.app.workbuddy.host",
    "oauthRelayBaseUrl": "https://www.workbuddy.cn/v2/as/genie-baas/oauth",
    "publishableKey": "wbpk_WNt740kcRavrc6QXdM2OIO_77POI2VYzqJSMHz1KtQ2v9PzIbTQR06G",
}


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_json_name(name: str) -> dict | list | None:
    return _load_json(DATA_DIR / name)


def _corpus_runs() -> list[dict]:
    corpus = _load_json_name("corpus.json")
    return corpus.get("runs", []) if isinstance(corpus, dict) else []


def _task_expects() -> dict[str, dict]:
    tasks = _load_json_name("tasks.json")
    if not isinstance(tasks, dict):
        return {}
    return {t["task_id"]: t.get("expect") or {} for t in tasks.get("tasks", [])}


def _diagnose_dict(raw: dict, expects: dict[str, dict]):
    """把一条原始轨迹字典变成 (Run, 诊断, 最小化, 评分)。列表与详情共用，保证口径一致。"""
    run = run_from_dict(raw)
    if not run.expected:
        run.expected = dict(expects.get(run.task_id) or {})
    run.expectation_missing = not bool(run.expected)
    diag = diagnose(run, run.expected)
    mini = minimize(run, diag) if not run.success or not run.success_known else None
    return run, diag, mini, evaluate(run)


def _report_diagnoses() -> dict[str, SimpleNamespace]:
    """复用报告里已算好的诊断。

    复核队列不该重新跑一遍归因 —— 那会让「复核的对象」和「报告里的结论」
    有可能不是同一条。复核的必须是用户看到的那一条，否则签字没有意义。
    """
    report = _load_json_name("report.json")
    if not isinstance(report, dict):
        return {}
    return {k: SimpleNamespace(**v) for k, v in (report.get("diagnoses") or {}).items()}


def _corpus_run_objs() -> list:
    expects = _task_expects()
    out = []
    for raw in _corpus_runs():
        run = run_from_dict(raw)
        if not run.expected:
            run.expected = dict(expects.get(run.task_id) or {})
        run.expectation_missing = not bool(run.expected)
        out.append(run)
    return out


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent 会诊室",
        version=VERSION,
        description="把 Agent 的失败轨迹归纳成失败模式，生成最小复现用例，并让修复可回归。",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # 上报接口要被别人的服务端/浏览器跨域调用，所以必须开 CORS。
    # 只放开上报与只读接口用得到的方法，且不携带 cookie —— 这个服务没有登录态。
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Clinic-Key"],
        allow_credentials=False,
    )

    @app.get("/api/health")
    def health() -> JSONResponse:
        corpus = _load_json_name("corpus.json")
        return JSONResponse(
            {
                "ok": True,
                "version": VERSION,
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "corpus": {
                    "loaded": bool(corpus),
                    "runs": len(corpus.get("runs", [])) if isinstance(corpus, dict) else 0,
                },
            }
        )

    @app.get("/api/overview")
    def overview() -> JSONResponse:
        report = _load_json_name("report.json")
        if report is None:
            return JSONResponse(
                {"ready": False, "reason": "轨迹数据集尚未采集", "version": VERSION}
            )
        return JSONResponse({"ready": True, "version": VERSION, **report})

    @app.get("/api/catalog")
    def catalog() -> JSONResponse:
        """筛选器要用的字典：任务、版本、模型、失败类型。"""
        report = _load_json_name("report.json")
        if not isinstance(report, dict):
            raise HTTPException(status_code=503, detail="报告尚未编译")
        return JSONResponse(report.get("catalog") or {})

    @app.get("/api/experiments")
    def experiments() -> JSONResponse:
        """逐任务的实验矩阵。

        版本汇总只有两行失败率，容易被总分掩盖真相。这里把同一批任务摊开：
        哪个被修好、哪个被弄坏、哪个从「确定失败」变成了「抛硬币」。
        """
        report = _load_json_name("report.json")
        if not isinstance(report, dict):
            raise HTTPException(status_code=503, detail="报告尚未编译")
        exp = report.get("experiment")
        if not isinstance(exp, dict) or not exp.get("ready"):
            raise HTTPException(status_code=503, detail="实验矩阵尚未编译")
        return JSONResponse({**exp, "generated_at": report.get("generated_at")})

    @app.get("/api/runs")
    def list_runs(
        version: str | None = None,
        task: str | None = None,
        success: str | None = None,
        kind: str | None = None,
        q: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> JSONResponse:
        """轨迹列表。全部从 report.json 的索引读，不重复跑诊断。"""
        report = _load_json_name("report.json")
        if not isinstance(report, dict):
            raise HTTPException(status_code=503, detail="报告尚未编译")
        rows = report.get("runs") or []
        if version:
            rows = [r for r in rows if r["version"] == version]
        if task:
            rows = [r for r in rows if r["task_id"] == task]
        if success in ("true", "1", "ok"):
            rows = [r for r in rows if r["success"]]
        elif success in ("false", "0", "fail"):
            rows = [r for r in rows if not r["success"]]
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if q:
            needle = q.lower()
            rows = [
                r
                for r in rows
                if needle in r["run_id"].lower()
                or needle in (r.get("label") or "").lower()
                or any(needle in t.lower() for t in r.get("tools") or [])
            ]
        total = len(rows)
        page = rows[offset : offset + limit]
        return JSONResponse({"total": total, "offset": offset, "limit": limit, "rows": page})

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        """单条轨迹的完整体检：原始步骤 + 逐步标注 + 最小复现 + 评分明细。

        这里**现场重算**而不是读快照，因为详情页要展示每一步的角色标注，
        那是 minimize 的产物，存进报告会让 report.json 膨胀十倍。
        """
        raw = next((r for r in _corpus_runs() if r.get("run_id") == run_id), None)
        if raw is None:
            raise HTTPException(status_code=404, detail=f"未找到轨迹 {run_id}")

        run, diag, mini, ev = _diagnose_dict(raw, _task_expects())
        roles = {n["step_index"]: n for n in (mini or {}).get("chain", [])}

        steps = []
        for s in run.steps:
            role = roles.get(s.index)
            steps.append(
                {
                    "step_index": s.index,
                    "kind": s.kind,
                    "tool": s.tool,
                    "args": s.args,
                    "ok": s.ok,
                    "error": s.error,
                    "content": (s.content or "")[:2000],
                    "observation": (
                        s.observation
                        if isinstance(s.observation, (dict, list))
                        else (s.observation_text[:2000] if s.observation is not None else None)
                    ),
                    "latency_ms": s.latency_ms,
                    "mark": (role or {}).get("role"),
                    "keep": (role or {}).get("keep"),
                    "why": (role or {}).get("why"),
                }
            )

        return JSONResponse(
            {
                "run_id": run.run_id,
                "task_id": run.task_id,
                "task_prompt": run.task_prompt,
                "version": run.prompt_version,
                "model": run.model,
                "imported_from": run.imported_from,
                "success": run.success,
                "success_known": run.success_known,
                "fail_reason": run.fail_reason,
                "expected": run.expected,
                "actual": run.actual,
                "usage": run.usage,
                "steps": steps,
                "diagnosis": diag.to_dict(),
                "minimal": mini,
                "evaluation": ev,
            }
        )

    # ── 任意两条轨迹并排对比 ────────────────────────────────────────────────
    @app.get("/api/compare")
    def compare_runs(a: str, b: str) -> JSONResponse:
        """把任意两条轨迹逐决策对齐。

        和 `report.fork` 的区别：那个是预先挑好的「最像的一对」，用来说明算法能做什么；
        这个是**你想看哪两条就看哪两条** —— 通常是一条 v1 成功 + 一条 v2 失败，
        用来回答「这一版到底把哪一步改坏了」。

        刻意不限制必须同任务、同结局：跨任务对比虽然少见，
        但「两条轨迹结构一模一样、结局不同」这种情况本身就是有用的信息。
        """
        raw = {r.get("run_id"): r for r in _corpus_runs()}
        missing = [x for x in (a, b) if x not in raw]
        if missing:
            raise HTTPException(status_code=404, detail=f"未找到轨迹 {'、'.join(missing)}")
        if a == b:
            raise HTTPException(status_code=400, detail="两条轨迹不能是同一条")

        expects = _task_expects()
        run_a, diag_a, _, _ = _diagnose_dict(raw[a], expects)
        run_b, diag_b, _, _ = _diagnose_dict(raw[b], expects)

        aligned = align_runs(run_a, run_b)

        def side(run, diag):
            return {
                "run_id": run.run_id,
                "task_id": run.task_id,
                "version": run.prompt_version,
                "success": run.success,
                "steps": len(run.tool_steps),
                "tokens": int(run.usage.get("total_tokens") or 0),
                "kind": diag.kind,
                "kind_label": kind_label(diag.kind),
                "diagnosis": diag.to_dict(),
            }

        return JSONResponse(
            {
                **aligned,
                "a": side(run_a, diag_a),
                "b": side(run_b, diag_b),
                "same_task": run_a.task_id == run_b.task_id,
                "note": (
                    "对齐用的是决策签名（工具名 + 参数键），不含参数值 —— "
                    "参数值会被最小化改写，签名才是结构。"
                    "所以「参数不同」不会显示成一次分叉，只有「调用结构不同」才是。"
                ),
            }
        )

    # ── 实时上报：SDK 边跑边送，这里边送边诊 ────────────────────────────────
    #
    # 用内存环形缓冲 + SSE，不引 Redis / WebSocket / 消息队列。
    # 理由和整个项目一致：单进程、零外部依赖、打开就能用。
    # 代价是**多副本部署时各看各的**，这一点在 /api/stream/info 里如实标注。
    _stream_buf: deque = deque(maxlen=400)
    _subs: set[asyncio.Queue] = set()
    _rate: list[float] = []

    def _rate_ok() -> bool:
        t = time.monotonic()
        while _rate and t - _rate[0] > 60:
            _rate.pop(0)
        if len(_rate) >= STREAM_RATE_LIMIT:
            return False
        _rate.append(t)
        return True

    def _broadcast(item: dict) -> None:
        _stream_buf.append(item)
        for q in list(_subs):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                # 订阅端跟不上就丢给它，不能拖慢上报端
                pass

    def _auth(request: Request) -> None:
        if not CLINIC_KEY:
            return
        got = request.headers.get("x-clinic-key") or request.query_params.get("key") or ""
        if got != CLINIC_KEY:
            raise HTTPException(
                status_code=401,
                detail="上报密钥不正确。服务端通过环境变量 CLINIC_KEY 配置，SDK 里要传同一个值。",
            )

    def _persist_stream(items: list[dict]) -> None:
        """落盘留痕。失败不影响上报结果 —— 磁盘满不该让采集端挂掉。"""
        try:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            with (RUNTIME_DIR / "stream.jsonl").open("a", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @app.get("/api/stream/info")
    def stream_info() -> JSONResponse:
        """实时流的自述：能做什么、不能做什么。前端会把 limits 原样显示出来。"""
        return JSONResponse(
            {
                "ready": True,
                "key_required": bool(CLINIC_KEY),
                "rate_limit_per_min": STREAM_RATE_LIMIT,
                "buffer": len(_stream_buf),
                "subscribers": len(_subs),
                "endpoints": {
                    "step": "POST /api/stream/step   单步上报（数组或单个对象）",
                    "trace": "POST /api/stream/trace  整条轨迹上报，返回现场诊断",
                    "feed": "GET  /api/stream/feed   SSE 推送",
                    "replay": "POST /api/stream/replay 回放样本轨迹（演示用）",
                },
                "sdk": {
                    "url": "/sdk/agent-clinic.js",
                    "note": "零依赖 ES 模块，浏览器与 Node 18+ 都能用；wrap() 包一层工具函数即自动上报。",
                },
                "limits": [
                    "单进程内存缓冲，多副本部署时各副本各看各的。",
                    "只保留最近 400 条事件，超出后最老的被覆盖。",
                    "上报的轨迹用于现场诊断，不写入正式轨迹库；要进库请走 /api/intake 或存进回归集。",
                ],
            }
        )

    @app.post("/api/stream/step")
    async def stream_step(request: Request, payload: Any = Body(...)) -> JSONResponse:
        """接收单步事件。SDK 会批量发，所以这里同时接受数组与单个对象。"""
        _auth(request)
        if not _rate_ok():
            raise HTTPException(status_code=429, detail=f"超过 {STREAM_RATE_LIMIT} 条/分钟的上报上限")
        items = payload if isinstance(payload, list) else [payload]
        for it in items:
            if isinstance(it, dict):
                _broadcast({"type": "step", "source": "sdk", **it})
        _persist_stream([i for i in items if isinstance(i, dict)])
        return JSONResponse({"ok": True, "accepted": len(items)})

    @app.post("/api/stream/trace")
    async def stream_trace(request: Request, payload: Any = Body(...)) -> JSONResponse:
        """接收整条轨迹：现场诊断，并把结论推给所有正在看流的人。

        刻意不落进正式轨迹库（data/corpus.json）：
        那套语料是报告的数据源，混进陌生人的轨迹会让报告的每个数字都失去可比性。
        """
        _auth(request)
        if not _rate_ok():
            raise HTTPException(status_code=429, detail=f"超过 {STREAM_RATE_LIMIT} 条/分钟的上报上限")

        items = payload if isinstance(payload, list) else [payload]
        expects = _task_expects()
        out = []
        for raw in items:
            if not isinstance(raw, dict) or not raw.get("run_id"):
                continue
            try:
                run, diag, mini, ev = _diagnose_dict(raw, expects)
            except Exception as e:  # noqa: BLE001
                # 上报端格式不对不该让服务 500 —— 告诉它哪里不对更有用
                out.append({"run_id": raw.get("run_id"), "ok": False, "error": str(e)[:200]})
                continue
            out.append(
                {
                    "run_id": run.run_id,
                    "ok": True,
                    "success": run.success,
                    "success_known": run.success_known,
                    "steps": len(run.tool_steps),
                    "kind": diag.kind,
                    "kind_label": kind_label(diag.kind),
                    "confidence": round(diag.confidence, 2),
                    "evidence": diag.evidence,
                    "minimal_steps": (mini or {}).get("minimal_steps"),
                    "evaluation": {"overall": ev.get("overall"), "scope": ev.get("scope")},
                }
            )
            _broadcast(
                {
                    "type": "trace",
                    "source": "sdk",
                    "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "version": run.prompt_version,
                    "model": run.model,
                    "success": run.success,
                    "success_known": run.success_known,
                    "steps": len(run.tool_steps),
                    "kind": diag.kind,
                    "kind_label": kind_label(diag.kind),
                    "confidence": round(diag.confidence, 2),
                    "evidence": diag.evidence,
                    "tools": run.tools_used,
                }
            )
        _persist_stream([i for i in items if isinstance(i, dict)])
        return JSONResponse({"ok": True, "count": len(out), "results": out})

    @app.get("/api/stream/feed")
    async def stream_feed(request: Request) -> StreamingResponse:
        """SSE。用 SSE 而不是 WebSocket：单向推送、浏览器原生重连、不需要新依赖。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)

        async def gen():
            _subs.add(q)
            try:
                yield 'data: {"type":"hello","buffer":%d}\n\n' % len(_stream_buf)
                # 先补最近的历史：刚打开页面的人不该面对一块空白
                for it in list(_stream_buf)[-25:]:
                    yield f"data: {json.dumps(it, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        # 心跳，防止中间代理把空闲连接掐掉
                        yield ": ping\n\n"
            finally:
                _subs.discard(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/api/stream/replay")
    async def stream_replay(payload: dict = Body(default={})) -> JSONResponse:
        """把仓库里的真实轨迹回放进流里，供没有接 SDK 的人看效果。

        回放的是真轨迹，节奏是模拟的。事件带 source="replay"，
        界面上会与真实上报分开标注 —— 拿回放冒充实时数据，是这类工具最容易犯的错。
        """
        n = max(1, min(int((payload or {}).get("count") or 5), 20))
        gap = max(0.0, min(float((payload or {}).get("gap") or 0.45), 3.0))
        expects = _task_expects()
        raw_runs = _corpus_runs()
        # 挑有代表性的一组：成功与失败混着来，否则流上看不出诊断在做什么
        bad = [r for r in raw_runs if not r.get("success")][: n // 2 + n % 2]
        good = [r for r in raw_runs if r.get("success")][: n // 2]
        picked = [x for pair in zip(bad, good) for x in pair] + bad[len(good):] + good[len(bad):]
        picked = picked[:n]

        sent = 0
        for raw in picked:
            try:
                run, diag, _, _ = _diagnose_dict(raw, expects)
            except Exception:  # noqa: BLE001
                continue
            for s in run.steps:
                ev = {
                    "type": "step",
                    "source": "replay",
                    "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "version": run.prompt_version,
                    "model": run.model,
                    "step": {
                        "i": s.index,
                        "kind": s.kind,
                        "tool": s.tool,
                        "args": s.args,
                        "ok": s.ok,
                        "error": s.error,
                        "content": (s.content or "")[:240] or None,
                        "observation_brief": (s.observation_text or "")[:240] or None,
                        "latency_ms": s.latency_ms,
                    },
                }
                _broadcast(ev)
                sent += 1
                if gap:
                    await asyncio.sleep(gap)
            _broadcast(
                {
                    "type": "trace",
                    "source": "replay",
                    "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "version": run.prompt_version,
                    "model": run.model,
                    "success": run.success,
                    "success_known": run.success_known,
                    "steps": len(run.tool_steps),
                    "kind": diag.kind,
                    "kind_label": kind_label(diag.kind),
                    "confidence": round(diag.confidence, 2),
                    "evidence": diag.evidence,
                    "tools": run.tools_used,
                }
            )
            sent += 1
        return JSONResponse({"ok": True, "replayed": len(picked), "events": sent, "note": "回放的是仓库内真实轨迹，节奏为模拟。"})

    # ── 多轮会话：单轮测不出的那一层 ────────────────────────────────────────
    def _load_sessions() -> list:
        """优先读入库的会话（随仓库交付），其次读采集台的临时产出。"""
        lib = _load_json_name("sessions.json")
        if isinstance(lib, dict) and lib.get("sessions"):
            raw = lib["sessions"]
        elif isinstance(lib, list) and lib:
            raw = lib
        else:
            path = RUNTIME_DIR / "sessions.jsonl"
            if not path.exists():
                return []
            raw = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        out = []
        for d in raw:
            try:
                out.append(session_from_dict(d))
            except (KeyError, TypeError):
                continue
        return out

    @app.get("/api/sessions")
    def list_sessions() -> JSONResponse:
        sessions = _load_sessions()
        if not sessions:
            return JSONResponse(
                {
                    "ready": False,
                    "reason": "还没有多轮会话数据。可在采集台跑 tools/drive-session.mjs 生成。",
                    "summary": None,
                    "sessions": [],
                }
            )
        summary = summarize_sessions(sessions)
        rows = []
        for s in sessions:
            findings = diagnose_session(s)
            rows.append(
                {
                    "session_id": s.session_id,
                    "scenario_id": s.scenario_id,
                    "version": s.prompt_version,
                    "goal": s.goal,
                    "model": s.model,
                    "turns": len(s.turns),
                    "steps": s.total_steps,
                    "tokens": s.total_tokens,
                    "success": s.success,
                    "failed_turns": [t.turn_index for t in s.failed_turns],
                    "cross_kinds": sorted({f.kind for f in findings}),
                    "cross_labels": sorted({f.label for f in findings}),
                    "dependency": [r["tag"] for r in history_dependency(s)],
                }
            )
        rows.sort(key=lambda r: (r["session_id"], r["version"]))
        return JSONResponse({"ready": True, "summary": summary, "sessions": rows, "count": len(rows)})

    @app.get("/api/sessions/scenarios")
    def list_scenarios() -> JSONResponse:
        """场景定义。界面要能说清「这个会话是设计来测什么的」，否则失败看起来像偶然。"""
        raw = _load_json_name("scenarios.json")
        return JSONResponse(raw or {"scenarios": []})

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> JSONResponse:
        s = next((x for x in _load_sessions() if x.session_id == session_id), None)
        if s is None:
            raise HTTPException(status_code=404, detail=f"未找到会话 {session_id}")
        findings = diagnose_session(s)
        deps = {r["turn_index"]: r for r in history_dependency(s)}

        turns = []
        for t in s.turns:
            turns.append(
                {
                    "turn_index": t.turn_index,
                    "user": t.user,
                    "success": t.success,
                    "success_known": t.success_known,
                    "expect": t.expect,
                    "actual": t.actual,
                    "fail_reason": t.fail_reason,
                    "tokens": int(t.usage.get("total_tokens") or 0),
                    "final_content": (t.final_text or "")[:1200],
                    "dependency": (deps.get(t.turn_index) or {}).get("tag"),
                    "grounded": t.grounded,
                    "single_kind": t.single_kind,
                    "cross_kinds": [f.kind for f in findings if f.turn_index == t.turn_index],
                    "steps": [
                        {
                            "step_index": x.index,
                            "kind": x.kind,
                            "tool": x.tool,
                            "args": x.args,
                            "ok": x.ok,
                            "error": x.error,
                            "content": (x.content or "")[:600],
                            "observation": (
                                x.observation
                                if isinstance(x.observation, (dict, list))
                                else (x.observation_text[:600] if x.observation is not None else None)
                            ),
                        }
                        for x in t.steps
                    ],
                }
            )

        return JSONResponse(
            {
                "session_id": s.session_id,
                "scenario_id": s.scenario_id,
                "version": s.prompt_version,
                "goal": s.goal,
                "model": s.model,
                "started_at": s.started_at,
                "user_id": s.user_id,
                "success": s.success,
                "turns": turns,
                "findings": [f.to_dict() for f in findings],
                "patches": [
                    {"kind": k, "label": SESSION_KINDS.get(k, k), "text": SESSION_PATCH[k]}
                    for k in sorted({f.kind for f in findings})
                ],
                "caveat": (
                    "跨轮结论只依赖可见证据：哪一轮的哪句话、哪次调用的哪个参数。"
                    "不做「语义上好像不连贯」这类判断 —— 那种判断没法反驳，也就没法修。"
                ),
            }
        )


    # ── 现场接诊：把别人的轨迹拖进来 ────────────────────────────────────────
    @app.get("/api/intake/formats")
    def intake_formats() -> JSONResponse:
        return JSONResponse(
            {
                "formats": [{"key": k, "label": v, "sample": f"/api/intake/sample/{k}"} for k, v in FORMATS.items()],
                "limits": {"bytes": MAX_INTAKE_BYTES, "runs": MAX_INTAKE_RUNS},
            }
        )

    @app.get("/api/intake/sample/{fmt}")
    def intake_sample(fmt: str) -> JSONResponse:
        path = SAMPLES_DIR / f"{fmt}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"没有 {fmt} 的样例")
        return JSONResponse(_load_json(path))

    @app.post("/api/intake")
    def intake(payload: dict = Body(...)) -> JSONResponse:
        """接收任意支持的格式，现场归一化 + 诊断。

        无状态：解析完就返回，不落库。只有用户显式调用 /api/regression/add 才会写盘。
        """
        text = payload.get("text") or ""
        fmt = payload.get("format") or "auto"
        expects = payload.get("expectations") or {}
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="没有收到内容")
        if len(text.encode("utf-8")) > MAX_INTAKE_BYTES:
            raise HTTPException(status_code=413, detail=f"内容超过 {MAX_INTAKE_BYTES // 1024 // 1024}MB")

        # 先按整段 JSON 解析；失败则当 JSONL 逐行处理 —— 用户不会关心两者区别
        try:
            parsed = json.loads(text)
            result = ingest(parsed, None if fmt == "auto" else fmt)
        except ValueError:
            result = ingest_jsonl(text, None if fmt == "auto" else fmt)

        if not result.runs:
            return JSONResponse(
                {
                    "ok": False,
                    "format": result.fmt,
                    "warnings": result.warnings or ["没有解析出任何轨迹"],
                    "runs": [],
                },
                status_code=200,
            )

        if len(result.runs) > MAX_INTAKE_RUNS:
            result.warnings.append(
                f"只诊断前 {MAX_INTAKE_RUNS} 条（共 {len(result.runs)} 条），避免单个请求拖垮服务"
            )
            result.runs = result.runs[:MAX_INTAKE_RUNS]

        out = []
        for run in result.runs:
            if not run.expected and isinstance(expects, dict):
                run.expected = dict(expects.get(run.run_id) or expects.get(run.task_id) or {})
            run.expectation_missing = not bool(run.expected)
            diag = diagnose(run, run.expected)
            # 失败必须做最小化；成败未判定的也要做（那是「工具层没报错但不知道任务成没成」的情形）。
            # 只有明确成功、且成败已判定时，才跳过。
            mini = minimize(run, diag) if (not run.success or not run.success_known) else None
            out.append(
                {
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "task_prompt": run.task_prompt,
                    "model": run.model,
                    "success": run.success,
                    "success_known": run.success_known,
                    "expectation_missing": run.expectation_missing,
                    "steps": len(run.tool_steps),
                    "tokens": int(run.usage.get("total_tokens") or 0),
                    "tools": [s.tool for s in run.tool_steps],
                    "diagnosis": diag.to_dict(),
                    "minimal": mini,
                    "evaluation": evaluate(run),
                }
            )

        stats = roundtrip_stats(result.runs)
        return JSONResponse(
            {
                "ok": True,
                "format": result.fmt,
                "format_label": FORMATS.get(result.fmt, result.fmt),
                "stats": stats,
                "warnings": result.warnings,
                "runs": out,
                # 这句会显示在界面上：接入得到的结论强度，低于自采轨迹
                "caveat": (
                    "这些是你导入的轨迹。凡是来源不含任务期望的，任务级归因（是否该退款、该不该转人工）"
                    "自动降级为「未判定」，只保留工具层体检结果 —— 系统不会替你猜任务要求什么。"
                ),
            }
        )

    # ── Prompt 版本管理：从诊断到「下一版怎么写」的那一段 ──────────────────
    def _read_versions() -> dict[str, Version]:
        return load_versions(_load_json(PROMPTS))

    def _write_versions(versions: dict[str, Version]) -> None:
        PROMPTS.parent.mkdir(parents=True, exist_ok=True)
        PROMPTS.write_text(
            json.dumps(dump_versions(versions), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        # 版本一变，报告里的版本对照就旧了。这里不自动重编 —— 重编要跑全量归因，
        # 放在请求里会让一次点击变成几秒的等待。改为在前端显式提示「需要重新编译报告」。

    def _version_runs() -> list[dict]:
        report = _load_json_name("report.json")
        return list((report or {}).get("runs") or [])

    @app.get("/api/prompts")
    def list_prompts() -> JSONResponse:
        versions = _read_versions()
        if not versions:
            raise HTTPException(status_code=503, detail="data/prompts.json 缺失或为空")
        return JSONResponse(prompt_overview(versions, _version_runs()))

    @app.get("/api/prompts/diff")
    def prompts_diff(a: str, b: str) -> JSONResponse:
        """两个版本的正文逐行对照。

        刻意做成 GET：diff 是无副作用的纯读操作，放进 POST 里会让它没法被直接分享链接。
        """
        versions = _read_versions()
        for vid in (a, b):
            if vid not in versions:
                raise HTTPException(status_code=404, detail=f"没有版本 {vid}")
        return JSONResponse(prompt_diff(versions[a], versions[b]))

    @app.get("/api/prompts/diffable")
    def prompts_diffable() -> JSONResponse:
        """给 diff 页准备的所有版本对，按「血缘相近优先」排序。"""
        versions = _read_versions()
        ids = sorted(versions)
        pairs = []
        for i, x in enumerate(ids):
            for y in ids[i + 1 :]:
                lin = lineage(versions, y)
                related = x in lin
                d = prompt_diff(versions[x], versions[y])
                pairs.append(
                    {
                        "a": x,
                        "b": y,
                        "related": related,
                        "changed": d["stats"]["added"] + d["stats"]["removed"] + d["stats"]["changed"],
                    }
                )
        pairs.sort(key=lambda p: (not p["related"], p["changed"]))
        return JSONResponse({"pairs": pairs})

    @app.get("/api/prompts/compose/sources")
    def compose_sources() -> JSONResponse:
        """能拿来生成新版本的原料：单轮病种 + 跨轮病种 + 复核改判。

        这一步是「诊断」和「写 prompt」之间那个具体的接口。
        没有它，规则草稿就永远只是一段可以复制走的文字。
        """
        report = _load_json_name("report.json") or {}
        patterns = report.get("patterns") or []
        out_single = [
            {
                "kind": p["kind"],
                "label": p["label"],
                "count": p["count"],
                "tasks": p.get("tasks") or [],
                "advice": p.get("advice") or "",
                "patch": p.get("patch") or patch_for(p["kind"]),
                "recommended": p["count"] >= 3,
            }
            for p in patterns
        ]

        sessions = _load_sessions()
        cross: dict[str, dict] = {}
        for s in sessions:
            for f in diagnose_session(s):
                cur = cross.setdefault(
                    f.kind,
                    {
                        "kind": f.kind,
                        "label": f.label,
                        "count": 0,
                        "sessions": [],
                        "patch": SESSION_PATCH.get(f.kind, ""),
                    },
                )
                cur["count"] += 1
                if s.session_id not in cur["sessions"]:
                    cur["sessions"].append(s.session_id)

        reviews = _read_reviews()
        overrides = [
            {
                "run_id": k,
                "from": v.get("original_kind"),
                "to": v.get("kind"),
                "reason": v.get("reason") or "",
            }
            for k, v in reviews.items()
            if v.get("action") == "override"
        ]

        return JSONResponse(
            {
                "single": out_single,
                "cross": list(cross.values()),
                "overrides": overrides,
                "note": (
                    "推荐标记只看「出现次数 ≥ 3」这一条硬指标，不做重要性加权 —— "
                    "加权会让我替你决定哪条更值得修，而这件事该由你判断。"
                ),
            }
        )

    @app.post("/api/prompts/compose")
    def compose_prompt(payload: dict = Body(...)) -> JSONResponse:
        """按选中的病种，在指定基线版上追加规则，生成一个草稿版本。

        生成的是**草稿**：没有轨迹验证过，所以它的失败率是空的，界面上也不会显示 0%。
        """
        versions = _read_versions()
        if not versions:
            raise HTTPException(status_code=503, detail="还没有任何 Prompt 版本")
        # 不传 base 就取版本号最大的那个当基线。
        # 不能用字典序 max()：那样 "v9" 会排在 "v10" 后面，选错基线。
        base_id = payload.get("base") or max(
            versions, key=lambda k: int(k[1:]) if k[1:].isdigit() else -1
        )
        if base_id not in versions:
            raise HTTPException(status_code=400, detail=f"基线版本 {base_id} 不存在")

        kinds = [k for k in (payload.get("kinds") or []) if isinstance(k, str)]
        cross_kinds = [k for k in (payload.get("cross_kinds") or []) if isinstance(k, str)]
        if not kinds and not cross_kinds:
            raise HTTPException(status_code=400, detail="至少要选一条依据")

        report = _load_json_name("report.json") or {}
        pat = {p["kind"]: p for p in (report.get("patterns") or [])}
        entries: list[tuple[str, str, Source]] = []

        for k in kinds:
            body = (pat.get(k) or {}).get("patch") or patch_for(k)
            if not body:
                continue
            p = pat.get(k) or {}
            entries.append(
                (
                    body,
                    f"{p.get('label') or kind_label(k)}（{p.get('count', 0)} 次）",
                    Source(
                        kind="pattern",
                        ref=k,
                        label=p.get("label") or kind_label(k),
                        evidence=(p.get("advice") or "")[:300],
                    ),
                )
            )

        sess_by_kind: dict[str, list[str]] = {}
        for s in _load_sessions():
            for f in diagnose_session(s):
                sess_by_kind.setdefault(f.kind, [])
                if s.session_id not in sess_by_kind[f.kind]:
                    sess_by_kind[f.kind].append(s.session_id)

        for k in cross_kinds:
            body = SESSION_PATCH.get(k)
            if not body:
                continue
            entries.append(
                (
                    body,
                    f"{SESSION_KINDS.get(k, k)}（多轮会话）",
                    Source(
                        kind="session",
                        ref=k,
                        label=SESSION_KINDS.get(k, k),
                        evidence="涉及会话：" + "、".join(sess_by_kind.get(k, [])[:4]),
                    ),
                )
            )

        if not entries:
            raise HTTPException(status_code=400, detail="选中的依据都没有可用的规则草稿")

        new_id = payload.get("id") or next_id(versions)
        if new_id in versions:
            raise HTTPException(status_code=409, detail=f"版本 {new_id} 已存在")

        v = compose_patch(
            versions[base_id],
            entries,
            new_id,
            label=payload.get("label") or "",
            note=payload.get("note") or "",
        )
        versions[new_id] = v
        _write_versions(versions)
        return JSONResponse({"ok": True, "version": v.to_dict(), "lineage": lineage(versions, new_id)})

    @app.get("/api/prompts/{vid}")
    def get_prompt(vid: str) -> JSONResponse:
        versions = _read_versions()
        if vid not in versions:
            raise HTTPException(status_code=404, detail=f"没有版本 {vid}")
        v = versions[vid]
        st = prompt_overview(versions, _version_runs())["versions"]
        row = next((x for x in st if x["id"] == vid), None)
        return JSONResponse(
            {
                **v.to_dict(),
                "lineage": lineage(versions, vid),
                "stats": (row or {}).get("stats"),
                "children": [x for x in versions if versions[x].parent == vid],
            }
        )

    @app.patch("/api/prompts/{vid}")
    def edit_prompt(vid: str, payload: dict = Body(...)) -> JSONResponse:
        """就地编辑。每次编辑都落一条 changelog —— 版本表的价值在于能回看。"""
        versions = _read_versions()
        if vid not in versions:
            raise HTTPException(status_code=404, detail=f"没有版本 {vid}")
        try:
            v = apply_edit(
                versions,
                vid,
                system=payload.get("system"),
                label=payload.get("label"),
                note=payload.get("note"),
                status=payload.get("status"),
                changelog_entry=payload.get("changelog_entry"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        _write_versions(versions)
        return JSONResponse({"ok": True, "version": v.to_dict()})

    # ── 回归集：把最小复现用例钉住，让修复可回归 ────────────────────────────
    def _read_regression() -> list[dict]:
        data = _load_json(REGRESSION)
        return data.get("cases", []) if isinstance(data, dict) else []

    def _write_regression(cases: list[dict]) -> None:
        REGRESSION.parent.mkdir(parents=True, exist_ok=True)
        REGRESSION.write_text(
            json.dumps(
                {
                    "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "cases": cases,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @app.get("/api/regression")
    def list_regression() -> JSONResponse:
        cases = _read_regression()
        return JSONResponse({"total": len(cases), "cases": cases})

    @app.post("/api/regression/add")
    def add_regression(payload: dict = Body(...)) -> JSONResponse:
        case = payload.get("case")
        if not isinstance(case, dict) or not case.get("case_id"):
            raise HTTPException(status_code=400, detail="缺少 case 或 case_id")
        cases = _read_regression()
        if any(c.get("case_id") == case["case_id"] for c in cases):
            return JSONResponse({"ok": True, "duplicated": True, "total": len(cases)})
        case = {**case, "added_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}
        cases.insert(0, case)
        _write_regression(cases)
        return JSONResponse({"ok": True, "duplicated": False, "total": len(cases)})

    @app.delete("/api/regression/{case_id}")
    def delete_regression(case_id: str) -> JSONResponse:
        cases = [c for c in _read_regression() if c.get("case_id") != case_id]
        _write_regression(cases)
        return JSONResponse({"ok": True, "total": len(cases)})

    @app.post("/api/regression/verify")
    def verify_regression(payload: dict = Body(default={})) -> JSONResponse:
        """拿一批轨迹，逐条验收回归集。

        两种入口：
        - 传 text：现场解析用户上传的轨迹（改完 prompt 重跑的那一批）
        - 不传：用仓库内已有的轨迹（演示「修复前的用例 vs 修复后的轨迹」）

        刻意不重跑模型：验收只回答「这个病还在不在」，不回答「模型现在会不会犯」。
        """
        cases = _read_regression()
        if not cases:
            return JSONResponse({"total": 0, "counts": {}, "verdicts": [], "summary": "回归集是空的，先存几条用例。"})

        text = (payload or {}).get("text") or ""
        if text.strip():
            try:
                parsed = json.loads(text)
                res = ingest(parsed, None if (payload.get("format") or "auto") == "auto" else payload["format"])
            except ValueError:
                res = ingest_jsonl(text)
            if not res.runs:
                return JSONResponse(
                    {
                        "total": len(cases),
                        "counts": {"uncovered": len(cases)},
                        "verdicts": [],
                        "summary": "这批数据没能解析出任何轨迹：" + "；".join(res.warnings[:2]),
                    }
                )
            runs = res.runs
            expects = _task_expects()
        else:
            runs = []
            expects = _task_expects()
            for raw in _corpus_runs():
                run = run_from_dict(raw)
                if not run.expected:
                    run.expected = dict(expects.get(run.task_id) or {})
                runs.append(run)

        for run in runs:
            if not run.expected:
                run.expected = dict(expects.get(run.task_id) or {})

        result = verify(cases, runs)
        return JSONResponse(result)

    # ── 复核队列：机器判不准的，等人签字 ────────────────────────────────────
    def _read_reviews() -> dict[str, dict]:
        data = _load_json(REVIEWS)
        return data.get("reviews", {}) if isinstance(data, dict) else {}

    def _write_reviews(reviews: dict[str, dict]) -> None:
        REVIEWS.parent.mkdir(parents=True, exist_ok=True)
        REVIEWS.write_text(
            json.dumps(
                {
                    "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "reviews": reviews,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @app.get("/api/review")
    def review_queue(stats_only: bool = False) -> JSONResponse:
        """待复核队列 + 复核前后对照。

        `stats_only=1` 只回统计，给概览页用，省掉几十条明细。
        """
        diags = _report_diagnoses()
        if not diags:
            raise HTTPException(status_code=503, detail="报告尚未编译")
        out = build_queue(_corpus_run_objs(), diags, _read_reviews())
        if stats_only:
            out.pop("items", None)
        return JSONResponse(out)

    @app.post("/api/review/{run_id}")
    def submit_review(run_id: str, payload: dict = Body(...)) -> JSONResponse:
        """提交一条复核意见：确认 / 改判 / 驳回。

        改判必须写理由 —— 它是唯一会改写报告结论的动作。
        """
        diags = _report_diagnoses()
        diag = diags.get(run_id)
        if diag is None:
            raise HTTPException(status_code=404, detail=f"{run_id} 不是一条已判定的失败轨迹")

        report = _load_json_name("report.json") or {}
        allowed = {k["kind"] for k in (report.get("catalog") or {}).get("kinds", [])}
        try:
            rec = verify_review(payload, allowed)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # 记下机器原判。复核记录要能自证「改的是哪一条、从什么改成了什么」，
        # 否则过几个月把病种表改了，这条记录就再也读不懂了。
        rec["original_kind"] = diag.kind
        rec["original_confidence"] = round(diag.confidence, 2)

        reviews = _read_reviews()
        reviews[run_id] = rec
        _write_reviews(reviews)
        return JSONResponse({"ok": True, "run_id": run_id, "review": rec, "total": len(reviews)})

    @app.delete("/api/review/{run_id}")
    def cancel_review(run_id: str) -> JSONResponse:
        reviews = _read_reviews()
        reviews.pop(run_id, None)
        _write_reviews(reviews)
        return JSONResponse({"ok": True, "total": len(reviews)})

    # ── 采集台：浏览器端真实跑 Agent，每一步回收到这里 ──────────────────────
    @app.get("/api/lab/bootstrap")
    def lab_bootstrap() -> JSONResponse:
        world = _load_json_name("world.json")
        tasks = _load_json_name("tasks.json")
        prompts = _load_json_name("prompts.json")
        scenarios = _load_json_name("scenarios.json")
        if not (world and tasks and prompts):
            raise HTTPException(status_code=503, detail="世界数据尚未生成，先跑 collector/build_world.py")
        return JSONResponse(
            {
                "publicConfig": PUBLIC_CONFIG,
                "world": world,
                "tasks": tasks["tasks"],
                "prompts": prompts,
                "scenarios": (scenarios or {}).get("scenarios", []),
            }
        )

    def _append_jsonl(name: str, items: list[dict]) -> int:
        """采集落盘。按 id 去重，重复跑不会把同一批轨迹叠成两份。"""
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        path = RUNTIME_DIR / name
        key = "session_id" if "session" in name else "run_id"
        seen: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if row.get(key):
                        seen[row[key]] = row
        for it in items:
            if it.get(key):
                seen[it[key]] = it
        with path.open("w", encoding="utf-8") as f:
            for row in seen.values():
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return len(seen)

    def _read_jsonl(name: str) -> list[dict]:
        path = RUNTIME_DIR / name
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    @app.post("/api/lab/ingest")
    def lab_ingest(runs: list[dict] = Body(...)) -> JSONResponse:
        total = _append_jsonl("collected.jsonl", runs)
        return JSONResponse({"stored": len(runs), "total": total})

    @app.get("/api/lab/dump")
    def lab_dump() -> JSONResponse:
        return JSONResponse({"runs": _read_jsonl("collected.jsonl")})

    @app.post("/api/lab/reset")
    def lab_reset() -> JSONResponse:
        path = RUNTIME_DIR / "collected.jsonl"
        if path.exists():
            path.unlink()
        return JSONResponse({"total": 0})

    @app.get("/api/lab/count")
    def lab_count() -> JSONResponse:
        return JSONResponse({"total": len(_read_jsonl("collected.jsonl"))})

    # 多轮会话采集与上面同构，只是文件与主键不同。
    # 分开存而不是混在一个文件里：会话和单轮轨迹的分析路径完全不同，
    # 混存会让下游每读一次都要先判类型。
    @app.post("/api/lab/ingest-session")
    def lab_ingest_session(sessions: list[dict] = Body(...)) -> JSONResponse:
        total = _append_jsonl("sessions.jsonl", sessions)
        return JSONResponse({"stored": len(sessions), "total": total})

    @app.get("/api/lab/dump-sessions")
    def lab_dump_sessions() -> JSONResponse:
        return JSONResponse({"sessions": _read_jsonl("sessions.jsonl")})

    @app.post("/api/lab/reset-session")
    def lab_reset_session() -> JSONResponse:
        path = RUNTIME_DIR / "sessions.jsonl"
        if path.exists():
            path.unlink()
        return JSONResponse({"total": 0})

    if SDK_DIR.exists():
        # 必须在挂载 "/" 之前：StaticFiles 挂在根路径上会吞掉之后所有更具体的路径。
        app.mount("/sdk", StaticFiles(directory=str(SDK_DIR)), name="sdk")

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:

        @app.get("/")
        def missing_static() -> FileResponse | JSONResponse:
            return JSONResponse({"ok": True, "hint": "前端产物尚未构建", "version": VERSION})

    return app
