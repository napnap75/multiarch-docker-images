from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import load_config, load_templates
from app.db import SQLiteFileRepository
from app.storage import get_storage_backend


def _build_storage(config) -> object:
    backend_name = getattr(config, "storage_backend", "s3").lower()

    if backend_name == "s3":
        return get_storage_backend(
            "s3",
            endpoint_url=config.s3.endpoint_url,
            aws_access_key_id=config.s3.access_key,
            aws_secret_access_key=config.s3.secret_key,
            bucket_name=config.s3.bucket_name,
            region_name=config.s3.region,
        )

    storage_dir = getattr(config, "local_storage_dir", os.environ.get("LOCAL_STORAGE_DIR", "./storage"))
    Path(storage_dir).mkdir(parents=True, exist_ok=True)
    return get_storage_backend("file", base_dir=storage_dir)


def create_app() -> FastAPI:
    config = load_config()
    registry = load_templates(os.environ.get("TEMPLATE_PATH", "./config.jsonc"))

    app = FastAPI(
        title="Automatic PDF Renamer",
        version="0.1.0",
        description="Offline API for PDF classification, validation, retraining, and consistency operations.",
    )
    app.state.config = config
    app.state.template_registry = registry
    app.state.storage = None
    app.state.db = SQLiteFileRepository(db_path=config.sqlite_path)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def startup_event() -> None:
        app.state.storage = _build_storage(config)
        app.state.template_registry = load_templates(
            os.environ.get("TEMPLATE_PATH", "./config.jsonc")
        )
        app.state.db = SQLiteFileRepository(db_path=config.sqlite_path)

    app.include_router(router)

    web_dir = Path("./web")
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="web_static")

    dist_dir = Path("./web/dist")
    if dist_dir.exists():
        app.mount("/static", StaticFiles(directory=str(dist_dir)), name="static")

    return app


app = create_app()
