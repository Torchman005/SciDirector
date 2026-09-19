from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SceneTag(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SCENE_TAG_UNSPECIFIED: _ClassVar[SceneTag]
    SCENE_TAG_MATH: _ClassVar[SceneTag]
    SCENE_TAG_DATA: _ClassVar[SceneTag]
    SCENE_TAG_CODE: _ClassVar[SceneTag]
    SCENE_TAG_AMBIENCE: _ClassVar[SceneTag]

class RenderEngine(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RENDER_ENGINE_UNSPECIFIED: _ClassVar[RenderEngine]
    RENDER_ENGINE_MANIM: _ClassVar[RenderEngine]
    RENDER_ENGINE_D3: _ClassVar[RenderEngine]
    RENDER_ENGINE_ECHARTS: _ClassVar[RenderEngine]
    RENDER_ENGINE_CODE_ANIM: _ClassVar[RenderEngine]
    RENDER_ENGINE_STOCK: _ClassVar[RenderEngine]

class ShotStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SHOT_STATUS_UNSPECIFIED: _ClassVar[ShotStatus]
    SHOT_STATUS_PENDING: _ClassVar[ShotStatus]
    SHOT_STATUS_GENERATING: _ClassVar[ShotStatus]
    SHOT_STATUS_RENDERING: _ClassVar[ShotStatus]
    SHOT_STATUS_CRITIQUING: _ClassVar[ShotStatus]
    SHOT_STATUS_APPROVED: _ClassVar[ShotStatus]
    SHOT_STATUS_REJECTED: _ClassVar[ShotStatus]
    SHOT_STATUS_RETRYING: _ClassVar[ShotStatus]
    SHOT_STATUS_FAILED: _ClassVar[ShotStatus]
    SHOT_STATUS_AWAITING_HUMAN: _ClassVar[ShotStatus]

class FeedbackSource(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FEEDBACK_SOURCE_UNSPECIFIED: _ClassVar[FeedbackSource]
    FEEDBACK_SOURCE_VLM: _ClassVar[FeedbackSource]
    FEEDBACK_SOURCE_HUMAN: _ClassVar[FeedbackSource]
    FEEDBACK_SOURCE_SYSTEM: _ClassVar[FeedbackSource]
SCENE_TAG_UNSPECIFIED: SceneTag
SCENE_TAG_MATH: SceneTag
SCENE_TAG_DATA: SceneTag
SCENE_TAG_CODE: SceneTag
SCENE_TAG_AMBIENCE: SceneTag
RENDER_ENGINE_UNSPECIFIED: RenderEngine
RENDER_ENGINE_MANIM: RenderEngine
RENDER_ENGINE_D3: RenderEngine
RENDER_ENGINE_ECHARTS: RenderEngine
RENDER_ENGINE_CODE_ANIM: RenderEngine
RENDER_ENGINE_STOCK: RenderEngine
SHOT_STATUS_UNSPECIFIED: ShotStatus
SHOT_STATUS_PENDING: ShotStatus
SHOT_STATUS_GENERATING: ShotStatus
SHOT_STATUS_RENDERING: ShotStatus
SHOT_STATUS_CRITIQUING: ShotStatus
SHOT_STATUS_APPROVED: ShotStatus
SHOT_STATUS_REJECTED: ShotStatus
SHOT_STATUS_RETRYING: ShotStatus
SHOT_STATUS_FAILED: ShotStatus
SHOT_STATUS_AWAITING_HUMAN: ShotStatus
FEEDBACK_SOURCE_UNSPECIFIED: FeedbackSource
FEEDBACK_SOURCE_VLM: FeedbackSource
FEEDBACK_SOURCE_HUMAN: FeedbackSource
FEEDBACK_SOURCE_SYSTEM: FeedbackSource

class TimeRange(_message.Message):
    __slots__ = ("start_sec", "end_sec")
    START_SEC_FIELD_NUMBER: _ClassVar[int]
    END_SEC_FIELD_NUMBER: _ClassVar[int]
    start_sec: float
    end_sec: float
    def __init__(self, start_sec: _Optional[float] = ..., end_sec: _Optional[float] = ...) -> None: ...

class ShotSpec(_message.Message):
    __slots__ = ("shot_id", "index", "narration", "visual_brief", "tag", "engine", "duration_sec", "keywords", "code", "language", "meta")
    class MetaEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    SHOT_ID_FIELD_NUMBER: _ClassVar[int]
    INDEX_FIELD_NUMBER: _ClassVar[int]
    NARRATION_FIELD_NUMBER: _ClassVar[int]
    VISUAL_BRIEF_FIELD_NUMBER: _ClassVar[int]
    TAG_FIELD_NUMBER: _ClassVar[int]
    ENGINE_FIELD_NUMBER: _ClassVar[int]
    DURATION_SEC_FIELD_NUMBER: _ClassVar[int]
    KEYWORDS_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    META_FIELD_NUMBER: _ClassVar[int]
    shot_id: str
    index: int
    narration: str
    visual_brief: str
    tag: SceneTag
    engine: RenderEngine
    duration_sec: float
    keywords: _containers.RepeatedScalarFieldContainer[str]
    code: str
    language: str
    meta: _containers.ScalarMap[str, str]
    def __init__(self, shot_id: _Optional[str] = ..., index: _Optional[int] = ..., narration: _Optional[str] = ..., visual_brief: _Optional[str] = ..., tag: _Optional[_Union[SceneTag, str]] = ..., engine: _Optional[_Union[RenderEngine, str]] = ..., duration_sec: _Optional[float] = ..., keywords: _Optional[_Iterable[str]] = ..., code: _Optional[str] = ..., language: _Optional[str] = ..., meta: _Optional[_Mapping[str, str]] = ...) -> None: ...

class RenderArtifact(_message.Message):
    __slots__ = ("artifact_id", "shot_id", "video_path", "audio_path", "subtitle_path", "duration_sec", "width", "height", "fps", "attempt", "engine", "frame_samples", "rendered_at_unix_ms", "render_cost_sec")
    ARTIFACT_ID_FIELD_NUMBER: _ClassVar[int]
    SHOT_ID_FIELD_NUMBER: _ClassVar[int]
    VIDEO_PATH_FIELD_NUMBER: _ClassVar[int]
    AUDIO_PATH_FIELD_NUMBER: _ClassVar[int]
    SUBTITLE_PATH_FIELD_NUMBER: _ClassVar[int]
    DURATION_SEC_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    FPS_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    ENGINE_FIELD_NUMBER: _ClassVar[int]
    FRAME_SAMPLES_FIELD_NUMBER: _ClassVar[int]
    RENDERED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    RENDER_COST_SEC_FIELD_NUMBER: _ClassVar[int]
    artifact_id: str
    shot_id: str
    video_path: str
    audio_path: str
    subtitle_path: str
    duration_sec: float
    width: int
    height: int
    fps: int
    attempt: int
    engine: str
    frame_samples: _containers.RepeatedScalarFieldContainer[str]
    rendered_at_unix_ms: int
    render_cost_sec: float
    def __init__(self, artifact_id: _Optional[str] = ..., shot_id: _Optional[str] = ..., video_path: _Optional[str] = ..., audio_path: _Optional[str] = ..., subtitle_path: _Optional[str] = ..., duration_sec: _Optional[float] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., fps: _Optional[int] = ..., attempt: _Optional[int] = ..., engine: _Optional[str] = ..., frame_samples: _Optional[_Iterable[str]] = ..., rendered_at_unix_ms: _Optional[int] = ..., render_cost_sec: _Optional[float] = ...) -> None: ...

class CriticFeedback(_message.Message):
    __slots__ = ("passed", "score", "issues", "suggestions", "raw_response", "model", "source", "attempt", "logic_score", "readability_score", "pacing_score", "aesthetics_score", "created_at_unix_ms")
    PASSED_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    ISSUES_FIELD_NUMBER: _ClassVar[int]
    SUGGESTIONS_FIELD_NUMBER: _ClassVar[int]
    RAW_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    LOGIC_SCORE_FIELD_NUMBER: _ClassVar[int]
    READABILITY_SCORE_FIELD_NUMBER: _ClassVar[int]
    PACING_SCORE_FIELD_NUMBER: _ClassVar[int]
    AESTHETICS_SCORE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    passed: bool
    score: float
    issues: _containers.RepeatedScalarFieldContainer[str]
    suggestions: _containers.RepeatedScalarFieldContainer[str]
    raw_response: str
    model: str
    source: FeedbackSource
    attempt: int
    logic_score: float
    readability_score: float
    pacing_score: float
    aesthetics_score: float
    created_at_unix_ms: int
    def __init__(self, passed: _Optional[bool] = ..., score: _Optional[float] = ..., issues: _Optional[_Iterable[str]] = ..., suggestions: _Optional[_Iterable[str]] = ..., raw_response: _Optional[str] = ..., model: _Optional[str] = ..., source: _Optional[_Union[FeedbackSource, str]] = ..., attempt: _Optional[int] = ..., logic_score: _Optional[float] = ..., readability_score: _Optional[float] = ..., pacing_score: _Optional[float] = ..., aesthetics_score: _Optional[float] = ..., created_at_unix_ms: _Optional[int] = ...) -> None: ...

class PipelineEvent(_message.Message):
    __slots__ = ("job_id", "shot_id", "node", "status", "message", "attempt", "shot_index", "total_shots", "progress", "artifact", "feedback", "error", "ts_unix_ms", "payload_json")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    SHOT_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    SHOT_INDEX_FIELD_NUMBER: _ClassVar[int]
    TOTAL_SHOTS_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    FEEDBACK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    TS_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_JSON_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    shot_id: str
    node: str
    status: ShotStatus
    message: str
    attempt: int
    shot_index: int
    total_shots: int
    progress: float
    artifact: RenderArtifact
    feedback: CriticFeedback
    error: str
    ts_unix_ms: int
    payload_json: str
    def __init__(self, job_id: _Optional[str] = ..., shot_id: _Optional[str] = ..., node: _Optional[str] = ..., status: _Optional[_Union[ShotStatus, str]] = ..., message: _Optional[str] = ..., attempt: _Optional[int] = ..., shot_index: _Optional[int] = ..., total_shots: _Optional[int] = ..., progress: _Optional[float] = ..., artifact: _Optional[_Union[RenderArtifact, _Mapping]] = ..., feedback: _Optional[_Union[CriticFeedback, _Mapping]] = ..., error: _Optional[str] = ..., ts_unix_ms: _Optional[int] = ..., payload_json: _Optional[str] = ...) -> None: ...

class JobProgress(_message.Message):
    __slots__ = ("job_id", "total_shots", "approved_shots", "failed_shots", "awaiting_human_shots", "progress", "started_at_unix_ms", "updated_at_unix_ms")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TOTAL_SHOTS_FIELD_NUMBER: _ClassVar[int]
    APPROVED_SHOTS_FIELD_NUMBER: _ClassVar[int]
    FAILED_SHOTS_FIELD_NUMBER: _ClassVar[int]
    AWAITING_HUMAN_SHOTS_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_UNIX_MS_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    total_shots: int
    approved_shots: int
    failed_shots: int
    awaiting_human_shots: int
    progress: float
    started_at_unix_ms: int
    updated_at_unix_ms: int
    def __init__(self, job_id: _Optional[str] = ..., total_shots: _Optional[int] = ..., approved_shots: _Optional[int] = ..., failed_shots: _Optional[int] = ..., awaiting_human_shots: _Optional[int] = ..., progress: _Optional[float] = ..., started_at_unix_ms: _Optional[int] = ..., updated_at_unix_ms: _Optional[int] = ...) -> None: ...

class HealthRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HealthResponse(_message.Message):
    __slots__ = ("healthy", "version", "llm_provider", "vlm_model", "sandbox_ready", "capabilities", "uptime_sec")
    HEALTHY_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    LLM_PROVIDER_FIELD_NUMBER: _ClassVar[int]
    VLM_MODEL_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_READY_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    UPTIME_SEC_FIELD_NUMBER: _ClassVar[int]
    healthy: bool
    version: str
    llm_provider: str
    vlm_model: str
    sandbox_ready: bool
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    uptime_sec: int
    def __init__(self, healthy: _Optional[bool] = ..., version: _Optional[str] = ..., llm_provider: _Optional[str] = ..., vlm_model: _Optional[str] = ..., sandbox_ready: _Optional[bool] = ..., capabilities: _Optional[_Iterable[str]] = ..., uptime_sec: _Optional[int] = ...) -> None: ...
