"""FastAPI 应用装配。

设计原则：
- **运行时零外部依赖**：所有分析都基于已采集并入库的轨迹快照，服务启动不调用任何模型 API。
  这样线上演示是确定性的、免费的和可复现的 —— 也是这个项目能被别人反复打开的前提。
- **算法与 HTTP 分离**：`analysis/` 是纯函数库，可独立单测；这里只负责取数、调用、序列化。
- **采集与诊断分离**：`/api/lab/*` 只负责把真实轨迹收进来；诊断永远读同一份数据，不重新问模型。
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "server" / "static"
RUNTIME_DIR = ROOT / "runtime"

VERSION = "0.2.0"

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

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        corpus = _load_json_name("corpus.json")
        if not isinstance(corpus, dict):
            raise HTTPException(status_code=503, detail="数据集尚未采集")
        for run in corpus.get("runs", []):
            if run.get("run_id") == run_id:
                return JSONResponse(run)
        raise HTTPException(status_code=404, detail=f"未找到轨迹 {run_id}")

    # ── 采集台：浏览器端真实跑 Agent，每一步回收到这里 ──────────────────
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
