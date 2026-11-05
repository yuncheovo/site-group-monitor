"""FastAPI 应用工厂。

约束（对应架构文档 §7.11 安全默认值）：
    * **只绑** ``127.0.0.1``，任何非本地地址的配置都会被强制纠正；
    * 无鉴权（本地环回单用户），如需共享由上层反向代理负责；
    * 所有 ``/api/**`` 返回统一信封 ``{code, data, message}``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from site_group_monitor import __version__
from site_group_monitor.config.settings import Settings, _bundle_root, ensure_user_config
from site_group_monitor.models.enums import SiteHealth
from site_group_monitor.services.site_service import health_of
from site_group_monitor.utils.errors import SgmError
from site_group_monitor.utils.logging import get_logger
from site_group_monitor.web.deps import Container, build_container, get_container
from site_group_monitor.web.routers import baidu_submit, overview, reports, sites, system
from site_group_monitor.web.schemas import fail, ok

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "")


def create_app(container: Container | None = None, settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用。

    Args:
        container: 服务容器（缺省自动构造单例）。
        settings: 配置对象（缺省取当前单例）。

    Returns:
        ``FastAPI`` 应用实例。
    """
    logger = get_logger("web")
    settings = settings or (container.settings if container else get_container().settings)
    ensure_user_config()
    if settings.web.host not in LOCAL_HOSTS:
        logger.warning("检测到非本地绑定 %s，已强制纠正为 127.0.0.1", settings.web.host)
        settings.web.host = "127.0.0.1"

    app = FastAPI(
        title="site_group_monitor",
        description="站群百度收录 / TDK / 健康状态本地监控仪表盘",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    container_instance = container or build_container(settings)
    app.state.container = container_instance
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1", "http://localhost"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------------- 异常 → 统一信封
    @app.exception_handler(SgmError)
    async def handle_sgm_error(request: Request, exc: SgmError) -> JSONResponse:
        """业务异常统一转换。"""
        return JSONResponse(status_code=400, content=fail(exc.message, code=400, data=exc.to_dict()))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底异常处理（内部错误不外泄细节）。"""
        logger.exception("api: 未处理异常 %s", exc)
        return JSONResponse(status_code=500, content=fail("服务器内部错误", code=500))

    # ---------------- 路由
    api_prefix = "/api"
    app.include_router(overview.router, prefix=api_prefix)
    app.include_router(sites.router, prefix=api_prefix)
    app.include_router(reports.router, prefix=api_prefix)
    app.include_router(system.router, prefix=api_prefix)
    app.include_router(baidu_submit.router, prefix=api_prefix)

    # ---------------- 静态资源（最后挂载，避免覆盖 API）
    static_dir = _resolve_static_dir(settings)
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        async def index(request: Request) -> Any:
            """返回仪表盘单页。"""
            index_file = static_dir / "index.html"
            if not index_file.is_file():  # pragma: no cover
                return JSONResponse(content=fail("静态资源缺失", code=404), status_code=404)
            return FileResponse(str(index_file))
    else:  # pragma: no cover - 静态目录缺失时给出提示
        @app.get("/", include_in_schema=False)
        async def index_placeholder(request: Request) -> JSONResponse:
            """静态资源缺失时的占位响应。"""
            return JSONResponse(
                content=ok(
                    {
                        "message": f"静态资源目录不存在: {static_dir}，"
                                   f"请确认 {settings.web.static_dir} 已随安装包提供。",
                        "docs": "/api/docs",
                    }
                )
            )

    @app.get(f"{api_prefix}/health", include_in_schema=False, tags=["system"])
    async def health() -> dict[str, Any]:
        """健康检查。"""
        return ok(
            {
                "status": "ok",
                "version": __version__,
                "timezone": settings.timezone_name(),
                "db": str(settings.db_path),
            }
        )

    return app


def _resolve_static_dir(settings: Settings) -> Path:
    """定位静态资源目录（兼容源码目录与安装后的包目录）。

    Args:
        settings: 配置对象。

    Returns:
        静态目录路径。
    """
    candidates = [
        Path(settings.static_dir),
        Path(__file__).resolve().parent / "static",
        # PyInstaller 打包后：资源被解压到 sys._MEIPASS，且 settings.static_dir
        # 这类相对路径会拼到 exe 所在目录下（那里没有 static/），必须补一条
        # 指向打包资源根的候选，否则 exe 双击后仪表盘 404。
        _bundle_root() / "site_group_monitor" / "web" / "static",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def run(host: str | None = None, port: int | None = None, *, log_level: str = "info") -> None:
    """启动 uvicorn 服务器（仅本地环回）。

    Args:
        host: 监听地址，非本地会被纠正。
        port: 端口。
        log_level: uvicorn 日志级别。
    """
    import uvicorn

    settings = get_container().settings
    host = host or settings.web.host
    if host not in LOCAL_HOSTS:
        host = "127.0.0.1"
    uvicorn.run(
        create_app(),
        host=host,
        port=int(port or settings.web.port),
        log_level=log_level,
    )


__all__ = ["create_app", "run", "health_of", "SiteHealth"]
