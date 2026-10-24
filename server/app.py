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

import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.diagnose import diagnose  # noqa: E402
from analysis.evaluate import EvalConfig, evaluate  # noqa: E402
from analysis.ingest import FORMATS, ingest, ingest_jsonl, roundtrip_stats  # noqa: E402
from analysis.minimize import minimize  # noqa: E402
from analysis.trace import run_from_dict  # noqa: E402

DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "server" / "static"
RUNTIME_DIR = ROOT / "runtime"
SAMPLES_DIR = DATA_DIR / "samples"
REGRESSION = DATA_DIR / "regression.json"

VERSION = "0.3.0"
MAX_INTAKE_BYTES = 4 * 1024 * 1024
MAX_INTAKE_RUNS = 60

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


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent 会诊室",
        version=VERSION,
        description="把 Agent 的失败轨迹归纳成失败模式，生成最小复现用例，并让修复可回归。",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
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

    # ── 采集台：浏览器端真实跑 Agent，每一步回收到这里 ──────────────────────
    @app.get("/api/lab/bootstrap")
    def lab_bootstrap() -> JSONResponse:
        world = _load_json_name("world.json")
        tasks = _load_json_name("tasks.json")
        prompts = _load_json_name("prompts.json")
        if not (world and tasks and prompts):
            raise HTTPException(status_code=503, detail="世界数据尚未生成，先跑 collector/build_world.py")
        return JSONResponse(
            {
                "publicConfig": PUBLIC_CONFIG,
                "world": world,
                "tasks": tasks["tasks"],
                "prompts": prompts,
            }
        )

    @app.post("/api/lab/ingest")
    def lab_ingest(runs: list[dict] = Body(...)) -> JSONResponse:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        path = RUNTIME_DIR / "collected.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for run in runs:
                f.write(json.dumps(run, ensure_ascii=False) + "\n")
        count = sum(1 for _ in path.open("r", encoding="utf-8"))
        return JSONResponse({"stored": len(runs), "total": count})

    @app.get("/api/lab/dump")
    def lab_dump() -> JSONResponse:
        path = RUNTIME_DIR / "collected.jsonl"
        if not path.exists():
            return JSONResponse({"runs": []})
        runs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return JSONResponse({"runs": runs})

    @app.post("/api/lab/reset")
    def lab_reset() -> JSONResponse:
        path = RUNTIME_DIR / "collected.jsonl"
        if path.exists():
            path.unlink()
        return JSONResponse({"total": 0})

    @app.get("/api/lab/count")
    def lab_count() -> JSONResponse:
        path = RUNTIME_DIR / "collected.jsonl"
        if not path.exists():
            return JSONResponse({"total": 0})
        return JSONResponse({"total": sum(1 for _ in path.open("r", encoding="utf-8"))})

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    else:

        @app.get("/")
        def missing_static() -> FileResponse | JSONResponse:
            return JSONResponse({"ok": True, "hint": "前端产物尚未构建", "version": VERSION})

    return app
