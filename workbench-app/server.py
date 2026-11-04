#!/usr/bin/env python3
"""Jarvis Workbench — standalone kanban UI server.

Serves the workbench front-end and mounts the *same* FastAPI router the
Hermes dashboard kanban plugin uses, so every read/write goes through
``hermes_cli.kanban_db`` (one source of truth: kanban.db).

Run with the Hermes runtime Python (see run.sh). Binds loopback by default.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", Path.home() / ".hermes" / "hermes-agent")).resolve()
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

# hermes_bootstrap wires the runtime dependency paths (fastapi, uvicorn, ...).
import hermes_bootstrap  # noqa: F401,E402

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent / "web"


def load_kanban_router():
    """Load the dashboard plugin's router without importing the dashboard app."""
    path = HERMES_ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    if not path.is_file():
        raise SystemExit(f"kanban plugin API not found: {path}")
    spec = importlib.util.spec_from_file_location("workbench_kanban_plugin_api", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Jarvis Workbench",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(load_kanban_router(), prefix="/api/plugins/kanban")

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    @app.get("/workbench")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        return JSONResponse(
            {
                "name": "Jarvis 工作台",
                "short_name": "工作台",
                "description": "待办 / 看板 / 已完成，读写本机 kanban.db",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0b1110",
                "theme_color": "#0b1110",
                "icons": [
                    {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}
                ],
            }
        )

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis Workbench standalone server")
    parser.add_argument("--host", default=os.environ.get("WORKBENCH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORKBENCH_PORT", "8787")))
    args = parser.parse_args()

    import uvicorn

    print(f"Jarvis Workbench → http://{args.host}:{args.port}", flush=True)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()