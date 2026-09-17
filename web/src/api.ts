/**
 * Go 网关的 REST 客户端。
 *
 * 统一在这里处理三件事，避免每个调用点各写一遍：
 *  1. 把后端的结构化错误体（APIError）转成带 code/detail 的异常，
 *     而不是让调用方去解析 Response；
 *  2. 带上 Accept 头并检查 content-type —— 反向代理返回 HTML 错误页时
 *     直接跳到「解析 JSON 失败」，会掩盖真正的原因；
 *  3. 空响应体（204）不当作错误。
 */

import type {
  APIError,
  ApproveResponse,
  DomainEvent,
  GenerateRequest,
  GenerateResponse,
  Job,
  PatchShotRequest,
  PatchShotResponse,
  RejectRequest,
  RejectResponse,
  Shot,
} from './types'

/** 后端不可用时的统一异常类型。 */
export class ApiError extends Error {
  readonly code: string
  readonly detail?: string
  readonly traceId?: string
  readonly status: number

  constructor(status: number, body: APIError) {
    // message 优先用后端给的中文提示；它是写给用户看的。
    super(body.message || `请求失败（HTTP ${status}）`)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code ?? 'UNKNOWN'
    this.detail = body.detail
    this.traceId = body.trace_id
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init?.headers ?? {}),
    },
  })

  const text = await resp.text()
  let parsed: unknown = null
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      // 网关或反代返回了 HTML（502/504 页面是最常见的）。
      // 直接说清楚，否则用户看到的是「Unexpected token <」这种无用信息。
      throw new ApiError(resp.status, {
        code: 'BAD_GATEWAY',
        message: `服务返回了非 JSON 响应（HTTP ${resp.status}）`,
        detail: text.slice(0, 300),
      })
    }
  }

  if (!resp.ok) {
    throw new ApiError(resp.status, (parsed as APIError) ?? { code: 'UNKNOWN', message: '' })
  }

  // 成功响应统一包在信封里：`{"ok": true, "data": ...}`。
  // 错误响应则是扁平的 `{"code": ..., "message": ...}`（上面已处理）。
  //
  // 必须显式拆信封：直接 `as T` 的话拿到的会是 `{ok, data}`，
  // 于是 `resp.job_id` 全是 undefined —— 页面不会报错，只会静静地什么都不显示，
  // 是最难定位的一类前端缺陷。（后端 respondOK 的约定，见 httpapi/errors.go。）
  const envelope = parsed as { ok?: unknown; data?: unknown }
  if (envelope && typeof envelope === 'object' && typeof envelope.ok === 'boolean') {
    return envelope.data as T
  }
  return parsed as T
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) })

export const api = {
  /** 提交生成任务。 */
  generate: (req: GenerateRequest) =>
    request<GenerateResponse>('/api/v1/generate', { method: 'POST', ...json(req) }),

  getJob: (jobId: string) => request<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}`),

  listShots: (jobId: string) =>
    request<Shot[]>(`/api/v1/jobs/${encodeURIComponent(jobId)}/shots`),

  /** 拉取事件历史。afterId 为 0 表示从头拉。 */
  listEvents: (jobId: string, afterId = 0) =>
    request<DomainEvent[]>(
      `/api/v1/jobs/${encodeURIComponent(jobId)}/events?after_id=${afterId}`,
    ),

  /** 打回：把意见回灌给编码智能体，重写代码并重渲。 */
  reject: (jobId: string, shotId: string, req: RejectRequest) =>
    request<RejectResponse>(
      `/api/v1/jobs/${encodeURIComponent(jobId)}/shots/${encodeURIComponent(shotId)}/reject`,
      { method: 'POST', ...json(req) },
    ),

  /** 放行：熔断镜头的人工出口。 */
  approve: (jobId: string, shotId: string) =>
    request<ApproveResponse>(
      `/api/v1/jobs/${encodeURIComponent(jobId)}/shots/${encodeURIComponent(shotId)}/approve`,
      { method: 'POST' },
    ),

  /** 成分镜编辑：改文案，可选立即重做。 */
  patchShot: (jobId: string, shotId: string, req: PatchShotRequest) =>
    request<PatchShotResponse>(
      `/api/v1/jobs/${encodeURIComponent(jobId)}/shots/${encodeURIComponent(shotId)}`,
      { method: 'PATCH', ...json(req) },
    ),

  /** 队列可观测（运维诊断）。 */
  queueStats: () => request<unknown>('/api/v1/queue/stats'),
}

/**
 * 把任意异常转成可展示的中文文案。
 *
 * 集中在这里的理由：`catch (err)` 拿到的永远是 `unknown`，
 * 每个调用点各写一遍 `instanceof` 判断既啰嗦又容易漏掉某类错误。
 * 最差情况也不要把 `[object Object]` 甩给用户 —— 那等于没报错。
 */
export function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    return err.detail ? `${err.message}（${err.detail}）` : err.message
  }
  if (err instanceof Error) return err.message
  if (typeof err === 'string') return err
  try {
    return JSON.stringify(err)
  } catch {
    return '未知错误'
  }
}

/**
 * 把渲染产物路径转换成可播放的 URL。
 *
 * 后端给的是**服务端文件系统路径**（Worker 与 API 可能在不同容器）。
 * 阶段四里 API 尚未提供产物下载路由，因此这里先直接返回路径，
 * 由部署层（nginx / 共享卷）决定如何暴露。返回 null 表示没有产物。
 *
 * 之所以不在这里编造一个 /media/... 的 URL：那会让人以为播放不了是前端的问题，
 * 而实际上缺的是服务端的暴露层 —— 把缺失明确地留在它该在的地方。
 */
export function artifactUrl(shot: Shot): string | null {
  const p = shot.artifact?.video_path
  return p ? p : null
}
