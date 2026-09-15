"""LangGraph 的 checkpointer（状态持久化）。

作用：让长链路任务在**进程重启后可续跑**。一次生成可能持续数十分钟，
如果中途因为发布、OOM、容器迁移而中断，没有 checkpoint 就只能从头再来 ——
那意味着把已经花掉的渲染与 token 全部浪费掉。

两级策略：
    PostgresSaver —— 生产。跨进程、跨重启持久化。
    MemorySaver   —— 本地开发 / CI。仅进程内有效。

**降级是显式的**：拿不到 Postgres 时只告警不崩，但 ``durable`` 会如实
上报为 False，绝不假装已经持久化。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..logging import get_logger

logger = get_logger(__name__)


@dataclass
class CheckpointHandle:
    """checkpointer 及其元信息。"""

    saver: Any
    backend: str
    durable: bool
    detail: str = ""

    def close(self) -> None:
        """释放底层连接（MemorySaver 无需处理）。"""
        conn = getattr(self.saver, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 收尾阶段不因清理失败而中断
                logger.debug("关闭 checkpoint 连接时出错", exc_info=True)


def build_checkpointer(settings: Settings) -> CheckpointHandle:
    """构造 checkpointer。

    优先 Postgres；不可用时降级为内存实现并**明确告警** ——
    静默降级会让"断点续跑"在真正需要它的那一刻才被发现从未生效。
    """
    handle = _try_postgres(settings)
    if handle is not None:
        return handle

    from langgraph.checkpoint.memory import MemorySaver

    logger.warning(
        "使用内存 checkpointer：进程重启后无法续跑（生产环境应配置 Postgres）",
        extra={"dsn_configured": bool(settings.postgres_dsn)},
    )
    return CheckpointHandle(
        saver=MemorySaver(),
        backend="memory",
        durable=False,
        detail="仅进程内有效；重启后已完成的镜头需要重跑",
    )


def _try_postgres(settings: Settings) -> CheckpointHandle | None:
    """尝试构造 Postgres checkpointer。

    任何一步失败都返回 None（由调用方降级），并把原因写进日志 ——
    配置问题需要被发现，但不该让 AI 服务起不来。
    """
    dsn = (settings.postgres_dsn or "").strip()
    if not dsn:
        logger.info("未配置 SCID_POSTGRES_DSN，跳过 Postgres checkpointer")
        return None

    try:
        import psycopg  # type: ignore[import-not-found]
        from langgraph.checkpoint.postgres import PostgresSaver  # type: ignore[import-not-found]
    except ImportError as exc:
        logger.warning(
            "缺少 Postgres checkpointer 依赖，降级为内存实现",
            extra={"error": str(exc)[:200]},
        )
        return None

    try:
        # autocommit + 单条长连接即可：checkpointer 的写入频率远低于业务请求，
        # 引入连接池只会增加一层需要管理的生命周期。
        conn = psycopg.connect(dsn, autocommit=True)
        saver = PostgresSaver(conn)
        # setup() 创建 LangGraph 所需的表；幂等，可重复调用。
        saver.setup()
    except Exception as exc:  # noqa: BLE001 - 连接/建表失败都属于可降级情形
        logger.warning(
            "Postgres checkpointer 初始化失败，降级为内存实现",
            extra={"error": str(exc)[:300]},
        )
        return None

    logger.info("Postgres checkpointer 已就绪（支持断点续跑）")
    return CheckpointHandle(
        saver=saver,
        backend="postgres",
        durable=True,
        detail="状态持久化到 Postgres，进程重启后可从断点续跑",
    )
