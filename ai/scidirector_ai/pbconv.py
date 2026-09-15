"""领域模型 ↔ protobuf 契约的双向转换。

为什么单独成模块（而不是留在 ``grpc_server.py`` 里）：
* 转换逻辑有**三个**消费者 —— gRPC 服务端、流水线节点（``plan`` 节点要把
  分镜表序列化成 ``payload_json`` 传给 Go）、以及 HITL 单点重做链路；
* 转换是纯函数，独立出来才能被单测直接覆盖（枚举拼错是低级但致命的 bug）；
* ``grpc_server`` 只需保留"适配"职责，不再混杂数据搬运。

命名约定：``x_from_pb`` / ``x_to_pb`` 表示方向，一眼可读。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .logging import get_logger
from .pb import _PB_ROOT  # noqa: F401 - 导入即完成 sys.path 注入，必须先于 pb 导入
from .schemas import (
    CriticFeedback,
    FeedbackSource,
    RenderArtifact,
    SceneTag,
    ShotSpec,
    StyleGuide,
)

# 生成代码使用绝对导入（from scidirector.v1 import ...），依赖 pb/__init__.py 的注入。
from scidirector.v1 import common_pb2 as common  # type: ignore[import-not-found]

logger = get_logger(__name__)


# ===========================================================================
# 枚举映射
# ===========================================================================

TAG_TO_PB: dict[SceneTag, int] = {
    SceneTag.MATH: common.SCENE_TAG_MATH,
    SceneTag.DATA: common.SCENE_TAG_DATA,
    SceneTag.CODE: common.SCENE_TAG_CODE,
    SceneTag.AMBIENCE: common.SCENE_TAG_AMBIENCE,
}
PB_TO_TAG: dict[int, SceneTag] = {v: k for k, v in TAG_TO_PB.items()}

ENGINE_TO_PB: dict[str, int] = {
    "manim": common.RENDER_ENGINE_MANIM,
    "d3": common.RENDER_ENGINE_D3,
    "echarts": common.RENDER_ENGINE_ECHARTS,
    "code_anim": common.RENDER_ENGINE_CODE_ANIM,
    "stock": common.RENDER_ENGINE_STOCK,
}
PB_TO_ENGINE: dict[int, str] = {v: k for k, v in ENGINE_TO_PB.items()}

STATUS_TO_PB: dict[str, int] = {
    "PENDING": common.SHOT_STATUS_PENDING,
    "GENERATING": common.SHOT_STATUS_GENERATING,
    "RENDERING": common.SHOT_STATUS_RENDERING,
    "CRITIQUING": common.SHOT_STATUS_CRITIQUING,
    "APPROVED": common.SHOT_STATUS_APPROVED,
    "REJECTED": common.SHOT_STATUS_REJECTED,
    "RETRYING": common.SHOT_STATUS_RETRYING,
    "FAILED": common.SHOT_STATUS_FAILED,
    "AWAITING_HUMAN": common.SHOT_STATUS_AWAITING_HUMAN,
}
PB_TO_STATUS: dict[int, str] = {v: k for k, v in STATUS_TO_PB.items()}

SOURCE_TO_PB: dict[FeedbackSource, int] = {
    FeedbackSource.VLM: common.FEEDBACK_SOURCE_VLM,
    FeedbackSource.HUMAN: common.FEEDBACK_SOURCE_HUMAN,
    FeedbackSource.SYSTEM: common.FEEDBACK_SOURCE_SYSTEM,
}


def tag_to_pb(tag: SceneTag) -> int:
    """场景标签 -> proto 枚举。未知标签降级为 UNSPECIFIED 而不是抛错。"""
    return TAG_TO_PB.get(tag, common.SCENE_TAG_UNSPECIFIED)


def tag_from_pb(value: int) -> SceneTag:
    """proto 枚举 -> 场景标签。

    ``UNSPECIFIED``（值 0）返回 ``AMBIENCE`` 而不是抛错：
    上游可能没给标签，此时让镜头退化为"氛围"至少能产出占位画面，
    比让整条流水线因为一个空字段停摆要好。
    """
    return PB_TO_TAG.get(value, SceneTag.AMBIENCE)


def engine_to_pb(engine: str | None) -> int:
    return ENGINE_TO_PB.get(engine or "", common.RENDER_ENGINE_UNSPECIFIED)


def engine_from_pb(value: int) -> str:
    return PB_TO_ENGINE.get(value, "")


def status_to_pb(status: str) -> int:
    """镜头状态（字符串）-> proto 枚举。未知状态降级为 UNSPECIFIED。"""
    return STATUS_TO_PB.get(status or "", common.SHOT_STATUS_UNSPECIFIED)


def status_from_pb(value: int) -> str:
    """proto 枚举 -> 镜头状态。

    ``UNSPECIFIED``（值 0）返回**空串**：任务级事件（全片进度、收场）
    不带状态，返回空串才能让上层用 ``if status:`` 明确地识别"未设置"，
    而不是拿到一个看起来合法的假值。
    """
    if value == common.SHOT_STATUS_UNSPECIFIED:
        return ""
    return PB_TO_STATUS.get(value, "")


# ===========================================================================
# 实体转换
# ===========================================================================


def shot_from_pb(message: Any) -> ShotSpec:
    """pb.ShotSpec -> 领域 ShotSpec。"""
    return ShotSpec(
        shot_id=message.shot_id,
        index=message.index,
        narration=message.narration,
        visual_brief=message.visual_brief,
        tag=tag_from_pb(message.tag),
        # engine 为 0（UNSPECIFIED）时传 None，让 ShotSpec 依标签重新推导 ——
        # 这保证了"引擎由标签确定性映射"这条铁律不会因为一个空字段被破坏。
        engine=engine_from_pb(message.engine) or None,
        duration_sec=message.duration_sec or 5.0,
        keywords=list(message.keywords),
        code=message.code,
        language=message.language,
        meta=dict(message.meta),
    )


def shot_to_pb(shot: ShotSpec) -> Any:
    """领域 ShotSpec -> pb.ShotSpec。"""
    return common.ShotSpec(
        shot_id=shot.shot_id,
        index=shot.index,
        narration=shot.narration,
        visual_brief=shot.visual_brief,
        tag=tag_to_pb(shot.tag),
        engine=engine_to_pb(shot.engine.value if shot.engine else None),
        duration_sec=shot.duration_sec,
        keywords=list(shot.keywords),
        code=shot.code,
        language=shot.language,
        meta=dict(shot.meta),
    )


def feedback_to_pb(feedback: CriticFeedback | None) -> Any:
    """领域 CriticFeedback -> pb.CriticFeedback。"""
    if feedback is None:
        return None
    return common.CriticFeedback(
        passed=feedback.passed,
        score=feedback.score,
        issues=list(feedback.issues),
        suggestions=list(feedback.suggestions),
        raw_response=feedback.raw_response,
        model=feedback.model,
        source=SOURCE_TO_PB.get(feedback.source, common.FEEDBACK_SOURCE_UNSPECIFIED),
        attempt=feedback.attempt,
        logic_score=feedback.logic_score,
        readability_score=feedback.readability_score,
        pacing_score=feedback.pacing_score,
        aesthetics_score=feedback.aesthetics_score,
        created_at_unix_ms=int(time.time() * 1000),
    )


def feedback_from_pb(message: Any) -> CriticFeedback:
    """pb.CriticFeedback -> 领域 CriticFeedback。

    注意：proto 侧允许 ``suggestions`` 为空（人类意见可能只给了 issues），
    而领域模型要求「未通过必须给建议」。这里做一次补齐，
    而不是让 Pydantic 抛异常把整条链路打断。
    """
    if message is None:
        return CriticFeedback(passed=True, score=1.0)

    suggestions = list(message.suggestions)
    passed = bool(message.passed)
    if not passed and not suggestions:
        suggestions = list(message.issues) or ["请根据上述问题调整画面（字号、节奏、元素布局）"]

    source = {
        common.FEEDBACK_SOURCE_HUMAN: FeedbackSource.HUMAN,
        common.FEEDBACK_SOURCE_SYSTEM: FeedbackSource.SYSTEM,
    }.get(message.source, FeedbackSource.VLM)

    return CriticFeedback(
        passed=passed,
        score=float(message.score),
        issues=list(message.issues),
        suggestions=suggestions,
        raw_response=message.raw_response,
        model=message.model,
        source=source,
        attempt=int(message.attempt),
        logic_score=float(message.logic_score),
        readability_score=float(message.readability_score),
        pacing_score=float(message.pacing_score),
        aesthetics_score=float(message.aesthetics_score),
    )


def artifact_to_pb(artifact: RenderArtifact | None) -> Any:
    """领域 RenderArtifact -> pb.RenderArtifact。"""
    if artifact is None:
        return None
    return common.RenderArtifact(
        artifact_id=artifact.artifact_id,
        shot_id=artifact.shot_id,
        video_path=artifact.video_path,
        audio_path=artifact.audio_path,
        subtitle_path=artifact.subtitle_path,
        duration_sec=artifact.duration_sec,
        width=artifact.width,
        height=artifact.height,
        fps=artifact.fps,
        attempt=artifact.attempt,
        engine=artifact.engine,
        frame_samples=list(artifact.frame_samples),
        rendered_at_unix_ms=artifact.rendered_at_unix_ms,
        render_cost_sec=artifact.render_cost_sec,
    )


def artifact_from_pb(message: Any) -> RenderArtifact:
    """pb.RenderArtifact -> 领域 RenderArtifact。"""
    if message is None:
        return RenderArtifact()
    return RenderArtifact(
        artifact_id=message.artifact_id,
        shot_id=message.shot_id,
        video_path=message.video_path,
        audio_path=message.audio_path,
        subtitle_path=message.subtitle_path,
        duration_sec=message.duration_sec,
        width=message.width,
        height=message.height,
        fps=message.fps,
        attempt=message.attempt,
        engine=message.engine,
        frame_samples=list(message.frame_samples),
        rendered_at_unix_ms=message.rendered_at_unix_ms,
        render_cost_sec=message.render_cost_sec,
    )


def style_guide_from_json(raw: str) -> StyleGuide:
    """把 Go 侧传来的 style_guide_json 解析为 StyleGuide。

    解析失败时**降级为默认风格**而不是让请求失败：
    风格是锦上添花，不该因为一个坏 JSON 把整次生成打掉。
    """
    if not raw or not raw.strip():
        return StyleGuide()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("style_guide_json 解析失败，使用默认风格", extra={"raw_prefix": raw[:200]})
        return StyleGuide()
    if not isinstance(payload, dict):
        return StyleGuide()
    try:
        return StyleGuide.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 校验失败一律降级
        logger.warning("style_guide 校验失败，使用默认风格", extra={"error": str(exc)[:300]})
        return StyleGuide()


# ===========================================================================
# 流水线事件
# ===========================================================================


def shots_payload_json(shots: list[ShotSpec], outline: str = "") -> str:
    """把分镜表序列化成 ``PipelineEvent.payload_json``。

    这是**跨语言约定**（见 docs/API.md 与 Agent.md §9）：
    Go 侧 ``worker.syncShotsFromPayload`` 会用 ``encoding/json`` 把它
    反序列化成 ``pb.ShotSpec``。因此这里必须使用 proto 的 **snake_case**
    字段名与**数值**枚举 —— 用 ``model_dump()`` 得到的 ``"MATH"`` 字符串
    在 Go 侧是解不出来的（Go 的枚举字段是 int32）。

    之所以用 JSON 而不是 proto 的 ``repeated`` 字段：分镜表结构仍在快速迭代，
    用 JSON 可以让它演进时不必每次都重新生成两侧代码。
    """
    return json.dumps(
        {
            "outline": outline,
            "shots": [
                {
                    "shot_id": s.shot_id,
                    "index": s.index,
                    "narration": s.narration,
                    "visual_brief": s.visual_brief,
                    "tag": tag_to_pb(s.tag),
                    "engine": engine_to_pb(s.engine.value if s.engine else None),
                    "duration_sec": s.duration_sec,
                    "keywords": list(s.keywords),
                }
                for s in shots
            ],
        },
        ensure_ascii=False,
    )


def event_to_pb(event: dict[str, Any]) -> Any:
    """内部事件字典 -> pb.PipelineEvent（流式响应的单条消息）。"""
    return common.PipelineEvent(
        job_id=event.get("job_id", ""),
        shot_id=event.get("shot_id", "") or "",
        node=event.get("node", "") or "",
        status=status_to_pb(event.get("status", "")),
        message=event.get("message", "") or "",
        attempt=int(event.get("attempt", 0) or 0),
        shot_index=int(event.get("shot_index", 0) or 0),
        total_shots=int(event.get("total_shots", 0) or 0),
        progress=float(event.get("progress", 0.0) or 0.0),
        artifact=artifact_to_pb(event.get("artifact")),
        feedback=feedback_to_pb(event.get("feedback")),
        error=event.get("error", "") or "",
        ts_unix_ms=int(event.get("ts_unix_ms", 0) or 0) or int(time.time() * 1000),
        payload_json=event.get("payload_json", "") or "",
    )
