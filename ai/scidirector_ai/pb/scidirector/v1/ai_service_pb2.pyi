from scidirector.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RunPipelineRequest(_message.Message):
    __slots__ = ("job_id", "raw_script", "style_guide_json", "target_duration_sec", "max_attempts_per_shot", "locale", "resume", "checkpoint_thread_id")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    RAW_SCRIPT_FIELD_NUMBER: _ClassVar[int]
    STYLE_GUIDE_JSON_FIELD_NUMBER: _ClassVar[int]
    TARGET_DURATION_SEC_FIELD_NUMBER: _ClassVar[int]
    MAX_ATTEMPTS_PER_SHOT_FIELD_NUMBER: _ClassVar[int]
    LOCALE_FIELD_NUMBER: _ClassVar[int]
    RESUME_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    raw_script: str
    style_guide_json: str
    target_duration_sec: float
    max_attempts_per_shot: int
    locale: str
    resume: bool
    checkpoint_thread_id: str
    def __init__(self, job_id: _Optional[str] = ..., raw_script: _Optional[str] = ..., style_guide_json: _Optional[str] = ..., target_duration_sec: _Optional[float] = ..., max_attempts_per_shot: _Optional[int] = ..., locale: _Optional[str] = ..., resume: bool = ..., checkpoint_thread_id: _Optional[str] = ...) -> None: ...

class PlanScriptRequest(_message.Message):
    __slots__ = ("job_id", "raw_script", "style_guide_json", "target_duration_sec", "locale")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    RAW_SCRIPT_FIELD_NUMBER: _ClassVar[int]
    STYLE_GUIDE_JSON_FIELD_NUMBER: _ClassVar[int]
    TARGET_DURATION_SEC_FIELD_NUMBER: _ClassVar[int]
    LOCALE_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    raw_script: str
    style_guide_json: str
    target_duration_sec: float
    locale: str
    def __init__(self, job_id: _Optional[str] = ..., raw_script: _Optional[str] = ..., style_guide_json: _Optional[str] = ..., target_duration_sec: _Optional[float] = ..., locale: _Optional[str] = ...) -> None: ...

class PlanScriptResponse(_message.Message):
    __slots__ = ("shots", "outline", "total_tokens", "elapsed_sec")
    SHOTS_FIELD_NUMBER: _ClassVar[int]
    OUTLINE_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TOKENS_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_SEC_FIELD_NUMBER: _ClassVar[int]
    shots: _containers.RepeatedCompositeFieldContainer[_common_pb2.ShotSpec]
    outline: str
    total_tokens: int
    elapsed_sec: float
    def __init__(self, shots: _Optional[_Iterable[_Union[_common_pb2.ShotSpec, _Mapping]]] = ..., outline: _Optional[str] = ..., total_tokens: _Optional[int] = ..., elapsed_sec: _Optional[float] = ...) -> None: ...

class GenerateShotRequest(_message.Message):
    __slots__ = ("job_id", "shot", "attempt", "feedback", "style_guide_json", "draft_only", "output_dir", "range_start_sec", "range_end_sec")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    SHOT_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    FEEDBACK_FIELD_NUMBER: _ClassVar[int]
    STYLE_GUIDE_JSON_FIELD_NUMBER: _ClassVar[int]
    DRAFT_ONLY_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_DIR_FIELD_NUMBER: _ClassVar[int]
    RANGE_START_SEC_FIELD_NUMBER: _ClassVar[int]
    RANGE_END_SEC_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    shot: _common_pb2.ShotSpec
    attempt: int
    feedback: _common_pb2.CriticFeedback
    style_guide_json: str
    draft_only: bool
    output_dir: str
    range_start_sec: float
    range_end_sec: float
    def __init__(self, job_id: _Optional[str] = ..., shot: _Optional[_Union[_common_pb2.ShotSpec, _Mapping]] = ..., attempt: _Optional[int] = ..., feedback: _Optional[_Union[_common_pb2.CriticFeedback, _Mapping]] = ..., style_guide_json: _Optional[str] = ..., draft_only: bool = ..., output_dir: _Optional[str] = ..., range_start_sec: _Optional[float] = ..., range_end_sec: _Optional[float] = ...) -> None: ...

class GenerateShotResponse(_message.Message):
    __slots__ = ("shot", "artifact", "success", "error", "total_tokens", "elapsed_sec", "partial_range_honored")
    SHOT_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TOKENS_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_SEC_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_RANGE_HONORED_FIELD_NUMBER: _ClassVar[int]
    shot: _common_pb2.ShotSpec
    artifact: _common_pb2.RenderArtifact
    success: bool
    error: str
    total_tokens: int
    elapsed_sec: float
    partial_range_honored: bool
    def __init__(self, shot: _Optional[_Union[_common_pb2.ShotSpec, _Mapping]] = ..., artifact: _Optional[_Union[_common_pb2.RenderArtifact, _Mapping]] = ..., success: bool = ..., error: _Optional[str] = ..., total_tokens: _Optional[int] = ..., elapsed_sec: _Optional[float] = ..., partial_range_honored: bool = ...) -> None: ...

class CritiqueShotRequest(_message.Message):
    __slots__ = ("job_id", "shot", "artifact", "attempt", "style_guide_json")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    SHOT_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    STYLE_GUIDE_JSON_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    shot: _common_pb2.ShotSpec
    artifact: _common_pb2.RenderArtifact
    attempt: int
    style_guide_json: str
    def __init__(self, job_id: _Optional[str] = ..., shot: _Optional[_Union[_common_pb2.ShotSpec, _Mapping]] = ..., artifact: _Optional[_Union[_common_pb2.RenderArtifact, _Mapping]] = ..., attempt: _Optional[int] = ..., style_guide_json: _Optional[str] = ...) -> None: ...

class CritiqueShotResponse(_message.Message):
    __slots__ = ("feedback", "degraded", "total_tokens", "elapsed_sec")
    FEEDBACK_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TOKENS_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_SEC_FIELD_NUMBER: _ClassVar[int]
    feedback: _common_pb2.CriticFeedback
    degraded: bool
    total_tokens: int
    elapsed_sec: float
    def __init__(self, feedback: _Optional[_Union[_common_pb2.CriticFeedback, _Mapping]] = ..., degraded: bool = ..., total_tokens: _Optional[int] = ..., elapsed_sec: _Optional[float] = ...) -> None: ...

class ReviseShotRequest(_message.Message):
    __slots__ = ("job_id", "shot", "human_comment", "attempt", "style_guide_json", "output_dir", "range_start_sec", "range_end_sec")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    SHOT_FIELD_NUMBER: _ClassVar[int]
    HUMAN_COMMENT_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    STYLE_GUIDE_JSON_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_DIR_FIELD_NUMBER: _ClassVar[int]
    RANGE_START_SEC_FIELD_NUMBER: _ClassVar[int]
    RANGE_END_SEC_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    shot: _common_pb2.ShotSpec
    human_comment: str
    attempt: int
    style_guide_json: str
    output_dir: str
    range_start_sec: float
    range_end_sec: float
    def __init__(self, job_id: _Optional[str] = ..., shot: _Optional[_Union[_common_pb2.ShotSpec, _Mapping]] = ..., human_comment: _Optional[str] = ..., attempt: _Optional[int] = ..., style_guide_json: _Optional[str] = ..., output_dir: _Optional[str] = ..., range_start_sec: _Optional[float] = ..., range_end_sec: _Optional[float] = ...) -> None: ...

class ReviseShotResponse(_message.Message):
    __slots__ = ("shot", "artifact", "feedback", "success", "error", "total_tokens", "elapsed_sec", "partial_range_honored")
    SHOT_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    FEEDBACK_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TOKENS_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_SEC_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_RANGE_HONORED_FIELD_NUMBER: _ClassVar[int]
    shot: _common_pb2.ShotSpec
    artifact: _common_pb2.RenderArtifact
    feedback: _common_pb2.CriticFeedback
    success: bool
    error: str
    total_tokens: int
    elapsed_sec: float
    partial_range_honored: bool
    def __init__(self, shot: _Optional[_Union[_common_pb2.ShotSpec, _Mapping]] = ..., artifact: _Optional[_Union[_common_pb2.RenderArtifact, _Mapping]] = ..., feedback: _Optional[_Union[_common_pb2.CriticFeedback, _Mapping]] = ..., success: bool = ..., error: _Optional[str] = ..., total_tokens: _Optional[int] = ..., elapsed_sec: _Optional[float] = ..., partial_range_honored: bool = ...) -> None: ...
