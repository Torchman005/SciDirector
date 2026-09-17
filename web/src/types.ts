/**
 * 与 Go 侧 DTO 一一对应的类型定义。
 *
 * 为什么手写而不是从后端生成：契约仍在小步演进（阶段四还在加字段），
 * 生成器会引入一次构建期依赖与一套版本对齐问题。
 * 这里唯一的纪律是：**改 Go 的 dto.go 必须同步改本文件**。
 * 两侧字段名不一致时，前端拿到的是 undefined —— 不会报错，只会静静地显示空白，
 * 因此下面每个可选字段都显式加了 `?`，让编译器帮忙盯住误用。
 */

// ---------------------------------------------------------------------------
// 领域模型
// ---------------------------------------------------------------------------

/** 与 domain.ShotStatus 一致。 */
export type ShotStatus =
  | 'PENDING'
  | 'GENERATING'
  | 'RENDERING'
  | 'CRITIQUING'
  | 'APPROVED'
  | 'REJECTED'
  | 'RETRYING'
  | 'FAILED'
  | 'AWAITING_HUMAN'

/** 与 domain.JobStatus 一致。 */
export type JobStatus =
  | 'PENDING'
  | 'PLANNING'
  | 'RENDERING'
  | 'COMPOSING'
  | 'COMPLETED'
  | 'PARTIAL'
  | 'FAILED'
  | 'CANCELLED'

export interface Artifact {
  artifact_id: string
  shot_id: string
  video_path: string
  duration_sec: number
  width: number
  height: number
  fps: number
  attempt: number
  engine: string
  frame_samples?: string[]
  render_cost_sec?: number
}

export interface Feedback {
  passed: boolean
  source: string
  attempt: number
  issues?: string[]
  suggestions?: string[]
  created_at: string
}

export interface Shot {
  shot_id: string
  index: number
  tag: string
  engine: string
  duration_sec: number
  narration: string
  visual_brief: string
  keywords?: string[]
  status: ShotStatus
  attempt: number
  code?: string
  language?: string
  artifact?: Artifact | null
  feedbacks?: Feedback[]
  error?: string
  updated_at?: string
}

export interface Job {
  job_id: string
  raw_script: string
  target_duration_sec: number
  locale: string
  status: JobStatus
  shots: Shot[] | null
  final_video_path?: string
  error?: string
  created_at: string
  updated_at?: string
}

export interface JobStat {
  total: number
  approved: number
  failed: number
  awaiting_human: number
  in_progress: number
}

export interface DomainEvent {
  event_id: number
  job_id: string
  shot_id?: string
  node: string
  status?: ShotStatus
  attempt?: number
  message: string
  progress?: number
  error?: string
  payload?: Record<string, unknown>
  timestamp: string
}

// ---------------------------------------------------------------------------
// WebSocket 协议（与 ws.Message / ws.InboundMessage 一致）
// ---------------------------------------------------------------------------

export interface ServerMessage {
  type: 'snapshot' | 'event' | 'events' | 'error' | 'ack' | 'pong'
  seq?: number
  data?: unknown
}

export interface SnapshotData {
  job: Job
  stat: JobStat
  progress: number
  events: DomainEvent[]
}

/** 连接状态。断网时前端必须**看得见**，否则用户只会以为「卡住了」。 */
export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'closed'

// ---------------------------------------------------------------------------
// REST 请求/响应
// ---------------------------------------------------------------------------

export interface GenerateRequest {
  raw_script: string
  target_duration_sec: number
  locale: string
}

export interface GenerateResponse {
  job_id: string
  status: JobStatus
  task_id?: string
}

export interface RejectRequest {
  comment: string
}

export interface RejectResponse {
  job_id: string
  shot_id: string
  status: ShotStatus
  attempt: number
  task_id?: string
}

export interface PatchShotRequest {
  narration?: string
  visual_brief?: string
  redo?: boolean
  comment?: string
}

export interface PatchShotResponse {
  job_id: string
  shot_id: string
  status: ShotStatus
  attempt?: number
  task_id?: string
  changed: string[]
}

export interface ApproveResponse {
  job_id: string
  shot_id: string
  status: ShotStatus
  compose_enqueued: boolean
}

/** 后端统一错误体。 */
export interface APIError {
  code: string
  message: string
  detail?: string
  trace_id?: string
}
