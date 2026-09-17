/**
 * 任务事件流的**纯函数**状态归约。
 *
 * 这个文件是阶段四最值得单测的地方：C2（刷新不丢上下文）与
 * C3（断线重连后进度不回跳）的**全部逻辑**都在这里，
 * 而它们出问题时的表现是「偶尔少一条事件」「进度条退一格」——
 * 靠肉眼点页面几乎不可能稳定复现。
 *
 * 三条不变式（下面每条都有对应测试）：
 *
 *  1. **事件按 seq 去重**。重连补发与实时推送可能重叠，
 *     重复应用同一条事件会让「已通过数」之类的派生量算错。
 *
 *  2. **seq 单调不回退**。丢弃 seq <= lastSeq 的事件。
 *     这正是「进度回跳」的根因：重连后若不丢弃旧事件，
 *     一条早先的 RENDERING 事件会覆盖掉后来的 APPROVED。
 *
 *  3. **按 seq 排序后应用**。补发的一批事件可能与实时事件交错到达，
 *     乱序应用同样会让最终状态与事件顺序不一致。
 */

import type { DomainEvent, Job, JobStat, SnapshotData } from './types'

export interface StreamState {
  job: Job | null
  stat: JobStat | null
  /** 服务端给出的进度（0~1）。**不做本地钳制** —— 见下方说明。 */
  progress: number
  events: DomainEvent[]
  /** 已应用到的事件序号上界，重连时作为 after_id 回传。 */
  lastSeq: number
  /** 是否已经收到过快照。未收到时不应把空状态当成「任务没有分镜」。 */
  loaded: boolean
}

export function initialState(): StreamState {
  return { job: null, stat: null, progress: 0, events: [], lastSeq: 0, loaded: false }
}

/** 事件列表的最大长度。 */
const MAX_EVENTS = 500

/**
 * 应用一份快照。
 *
 * 快照是**权威基线**：它整体替换本地状态，而不是与本地增量合并。
 * 合并会让「刷新后看到的状态」取决于刷新前本地残留了什么 ——
 * 那正是 C2 要避免的不确定性。
 */
export function applySnapshot(state: StreamState, data: unknown): StreamState {
  const snap = data as Partial<SnapshotData> | null
  if (!snap || !snap.job) return state

  const events = normalizeEvents(snap.events ?? [])
  // lastSeq 取事件上界与快照 seq 的较大者：
  // 服务端下发的 seq 与事件列表可能因截断而不一致，取大者才不会重复拉取。
  const maxSeq = events.reduce((m, e) => Math.max(m, e.event_id ?? 0), 0)

  return {
    job: snap.job,
    stat: snap.stat ?? null,
    progress: clamp01(snap.progress ?? 0),
    events,
    lastSeq: maxSeq,
    loaded: true,
  }
}

/**
 * 应用一批增量事件（来自实时推送或重连补发）。
 *
 * 去重、排序、更新 lastSeq 全在这里完成，因此调用方不需要关心
 * 「这批事件是从哪来的」—— 实时与补发走**完全相同的路径**，
 * 这样两条路径的行为不可能不一致。
 */
export function applyEvents(state: StreamState, incoming: DomainEvent[]): StreamState {
  // 注意把 state.job 透传下去而不是 null：
  // 空批次路径会原样写回这个参数，传 null 等于**用空任务覆盖已有任务** ——
  // 一次心跳、一次空补发就会把整个页面清空。
  return applyEventsInternal(state, incoming, state.job)
}

/** 应用事件，并可选地同步一份任务快照（事件里不含分镜明细）。 */
export function applyEventsWithJob(
  state: StreamState,
  incoming: DomainEvent[],
  job: Job | null,
): StreamState {
  return applyEventsWithDetail(state, incoming, { job })
}

/**
 * 查询接口 `data` 的完整明细：任务 + 统计 + 进度。
 *
 * 三者必须一起合并。曾经只合并 `job`，后果是一处很隐蔽的自相矛盾：
 * `deriveStat` 优先返回服务端的 `stat`，而 `stat` 只在**连接建立时的快照**里
 * 到达过 —— 那一刻任务刚提交、导演还没拆解，于是 `total = 0`。
 * 此后事件会不断更新分镜表，却没有人再更新 `stat` 与 `progress`，
 * 界面就长期显示「分镜表里 4 行，统计写着共 0 个分镜、进度 0%」。
 * `stream.ts` 里那句注释警告过这种界面（用户会彻底不信任这个页面），
 * 而它恰好由「只合并一半」制造了出来。
 */
export interface JobDetail {
  job?: Job | null
  stat?: JobStat | null
  progress?: number
}

/** 合并一份回源得到的完整明细（不含新事件）。 */
export function applyEventsWithDetail(
  state: StreamState,
  incoming: DomainEvent[],
  detail: JobDetail,
): StreamState {
  const next = applyEventsInternal(state, incoming, detail.job ?? state.job)
  return {
    ...next,
    stat: detail.stat ?? next.stat,
    progress: detail.progress ?? next.progress,
    loaded: true,
  }
}

function applyEventsInternal(
  state: StreamState,
  incoming: DomainEvent[],
  job: Job | null,
): StreamState {
  if (!incoming || incoming.length === 0) {
    // 即使没有新事件也要允许任务快照更新（例如人工放行后状态变了但没发事件）。
    return job === state.job ? state : { ...state, job }
  }

  const fresh = incoming.filter((e) => (e.event_id ?? 0) > state.lastSeq)
  if (fresh.length === 0) {
    return job === state.job ? state : { ...state, job }
  }

  // 按 seq 排序后再应用：补发与实时可能交错到达。
  const sorted = [...fresh].sort((a, b) => (a.event_id ?? 0) - (b.event_id ?? 0))

  let lastSeq = state.lastSeq
  let progress = state.progress
  const events = [...state.events]

  for (const ev of sorted) {
    const id = ev.event_id ?? 0
    if (id <= lastSeq) continue // 二次防御：排序后仍可能出现等值
    lastSeq = id
    events.push(ev)
    if (typeof ev.progress === 'number' && ev.progress > 0) {
      progress = clamp01(ev.progress)
    }
  }

  // 只保留最近的若干条：事件流是**诊断视图**，
  // 让它无限增长会拖垮长跑任务的页面。
  const trimmed = events.length > MAX_EVENTS ? events.slice(events.length - MAX_EVENTS) : events

  return { ...state, job, events: trimmed, lastSeq, progress }
}

/** 排序并丢弃明显无效的事件。 */
function normalizeEvents(raw: unknown): DomainEvent[] {
  if (!Array.isArray(raw)) return []
  const out = (raw as DomainEvent[])
    .filter((e) => e && typeof e === 'object')
    .sort((a, b) => (a.event_id ?? 0) - (b.event_id ?? 0))
  return out.length > MAX_EVENTS ? out.slice(out.length - MAX_EVENTS) : out
}

function clamp01(v: number): number {
  if (!Number.isFinite(v)) return 0
  return Math.min(1, Math.max(0, v))
}

// ---------------------------------------------------------------------------
// 派生视图
// ---------------------------------------------------------------------------

/** 分镜列表。`shots` 为 null（尚未拆解）时返回空数组，避免到处判空。 */
export function shotsOf(job: Job | null) {
  return job?.shots ?? []
}

/**
 * 统计各状态镜头数。
 *
 * 优先用服务端给的 stat（它是权威的），本地仅在没有时兜底计算。
 * 两处口径必须一致，否则会出现「进度条 100% 但表格里还有未完成镜头」这种
 * 自相矛盾的界面 —— 用户会彻底不信任这个页面。
 */
export function deriveStat(state: StreamState): JobStat {
  if (state.stat) return state.stat
  const shots = shotsOf(state.job)
  const stat: JobStat = { total: 0, approved: 0, failed: 0, awaiting_human: 0, in_progress: 0 }
  for (const s of shots) {
    stat.total++
    switch (s.status) {
      case 'APPROVED':
        stat.approved++
        break
      case 'FAILED':
      case 'REJECTED':
        stat.failed++
        break
      case 'AWAITING_HUMAN':
        stat.awaiting_human++
        break
      default:
        stat.in_progress++
    }
  }
  return stat
}

/** 熔断待人工处理的镜头。这是审核台最需要被看见的一组。 */
export function shotsAwaitingHuman(state: StreamState) {
  return shotsOf(state.job).filter((s) => s.status === 'AWAITING_HUMAN')
}
