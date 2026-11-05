"""搜索引擎收录检测抽象基类与证据留存。

为 R-019（搜狗 / 360）预留多态扩展：新增引擎只需实现 :class:`SearchEngineChecker` 子类，
编排层（CheckService / CheckWorker）无需改动。
"""

from __future__ import annotations

import gzip
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from site_group_monitor.models.entities import IndexResult, ProxyEndpoint


@dataclass(frozen=True)
class IndexCheckContext:
    """一次收录检测的上下文（不携带可变状态，便于并发安全）。"""

    task_id: str = ""
    attempt: int = 1
    proxy: ProxyEndpoint | None = None
    engine: str = "baidu"
    run_mode: str = "full"


class EvidenceStore:
    """百度结果页证据仓库（``data/evidence/{domain}/{ts}.html.gz``）。

    Args:
        base_dir: 证据根目录绝对路径。
        keep_days: 保留天数（由 retention 任务清理）。
        logger: 日志记录器。
    """

    def __init__(self, base_dir: str | Path, keep_days: int = 30, logger: Any | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.keep_days = max(1, int(keep_days))
        self.logger = logger

    def save(self, domain: str, html: str, meta: dict[str, Any] | None = None) -> str:
        """落盘一份 gzip 压缩的证据快照。

        Args:
            domain: 站点域名（目录名）。
            html: 原始 HTML。
            meta: 附加元信息（proxy/UA/status/params）。

        Returns:
            证据文件路径；失败返回空串。
        """
        try:
            directory = self.base_dir / EvidenceStore._safe_dirname(domain)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = directory / f"{stamp}.html.gz"
            with gzip.open(path, "wb") as handle:
                handle.write((html or "").encode("utf-8", errors="replace"))
            if meta:
                meta_path = directory / f"{stamp}.meta.json"
                meta_path.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return str(path)
        except OSError as exc:
            if self.logger is not None:
                self.logger.warning("evidence: 保存失败 %s (%s)", domain, exc)
            return ""

    def save_png(self, domain: str, png: bytes, meta: dict[str, Any] | None = None) -> str:
        """落盘百度结果页截图。"""
        if not png:
            return ""
        try:
            directory = self.base_dir / EvidenceStore._safe_dirname(domain)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = directory / f"{stamp}.png"
            path.write_bytes(png)
            if meta:
                meta_path = directory / f"{stamp}.meta.json"
                if not meta_path.exists():
                    meta_path.write_text(
                        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            return str(path)
        except OSError as exc:
            if self.logger is not None:
                self.logger.warning("evidence: 截图保存失败 %s (%s)", domain, exc)
            return ""

    @staticmethod
    def _safe_dirname(domain: str) -> str:
        """域名转安全目录名。

        Args:
            domain: 域名。

        Returns:
            目录名。
        """
        safe = "".join(char if (char.isalnum() or char in ".-_") else "_" for char in str(domain))
        return safe or "unknown"


class SearchEngineChecker(ABC):
    """搜索引擎收录检测器抽象基类。

    Attributes:
        engine: 引擎标识（``baidu`` / ``sogou`` / ``so360``）。
    """

    engine: str = "abstract"

    def __init__(
        self,
        http: Any,
        proxies: Any,
        limiter: Any,
        settings: Any,
        logger: Any | None = None,
    ) -> None:
        """构造检测器。

        Args:
            http: HTTP 客户端。
            proxies: 代理池。
            limiter: 限速器。
            settings: 配置对象。
            logger: 日志记录器。
        """
        self.http = http
        self.proxies = proxies
        self.limiter = limiter
        self.settings = settings
        self.logger = logger
        self.evidence = EvidenceStore(
            self._evidence_dir(settings), keep_days=self._evidence_days(settings), logger=logger
        )

    # ---------------------------------------------------------------- 配置
    @staticmethod
    def _evidence_dir(settings: Any) -> Path:
        """证据目录绝对路径。"""
        if settings is None:
            return Path("data/evidence")
        return Path(settings.evidence_dir)

    @staticmethod
    def _evidence_days(settings: Any) -> int:
        """证据保留天数。"""
        if settings is None:
            return 30
        return int(getattr(settings.engine.baidu, "evidence_days", 30) or 30)

    # ---------------------------------------------------------------- 契约
    @abstractmethod
    async def check(self, domain: str, ctx: IndexCheckContext) -> IndexResult:
        """执行一次收录检测。

        Args:
            domain: 归一化域名。
            ctx: 检测上下文。

        Returns:
            :class:`IndexResult`；**任何异常都必须在内部降级为 unknown**，不得抛出。
        """

    # ---------------------------------------------------------------- 辅助
    def unknown(
        self,
        reason: Any,
        *,
        message: str = "",
        evidence_path: str = "",
        http_status: int | None = None,
        elapsed_ms: int = 0,
    ) -> IndexResult:
        """构造一个 unknown 结果（安全降级统一出口）。

        Args:
            reason: 原因码枚举。
            message: 补充说明。
            evidence_path: 证据路径。
            http_status: HTTP 状态码。
            elapsed_ms: 耗时。

        Returns:
            :class:`IndexResult`。
        """
        from site_group_monitor.models.enums import IndexReasonCode, IndexStatus

        return IndexResult(
            status=IndexStatus.UNKNOWN,
            reason=reason if isinstance(reason, IndexReasonCode) else IndexReasonCode.PARSE_FAILED,
            evidence_path=evidence_path,
            http_status=http_status,
            elapsed_ms=elapsed_ms,
            error_msg=message,
            engine=self.engine,
        )
