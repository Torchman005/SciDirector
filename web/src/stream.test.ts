import { describe, expect, it } from 'vitest'

import {
  applyEvents,
  applyEventsWithJob,
  applySnapshot,
  deriveStat,
  initialState,
  shotsAwaitingHuman,
  shotsOf,
} from './stream'
import type { DomainEvent, Job, Shot } from './types'

function ev(id: number, over: Partial<DomainEvent> = {}): DomainEvent {
  return {
    event_id: id,
    job_id: 'job-1',
    node: 'render',
    message: `事件 ${id}`,
    timestamp: new Date(2026, 0, 1, 0, 0, id).toISOString(),
    ...over,
  }
}

function shot(over: Partial<Shot> = {}): Shot {
  return {
    shot_id: 'job-1-s000',
    index: 0,
    tag: 'AMBIENCE',
    engine: 'stock',
    duration_sec: 4,
    narration: '画外音',
    visual_brief: '画面说明',
    status: 'PENDING',
    attempt: 1,
    ...over,
  }
}

function job(over: Partial<Job> = {}): Job {
  return {
    job_id: 'job-1',
    raw_script: '脚本',
    target_duration_sec: 60,
    locale: 'zh-CN',
    status: 'RENDERING',
    shots: [shot()],
    created_at: new Date(2026, 0, 1).toISOString(),
    ...over,
  }
}

// ---------------------------------------------------------------------------
// C3：断线重连后进度不回跳
// ---------------------------------------------------------------------------

describe('applyEvents —— 断线重连', () => {
  it('丢弃 seq 不大于 lastSeq 的事件（这是「进度回跳」的根因）', () => {
    let state = applySnapshot(initialState(), {
      job: job(), stat: null, progress: 0.8, events: [ev(10)],
    })
    expect(state.lastSeq).toBe(10)
    expect(state.progress).toBe(0.8)

    // 重连后补发了一批旧事件：其中 seq=5 的进度只有 0.2。
    // 若不过滤，界面会从 80% 掉回 20%。
    const replayed = [ev(5, { progress: 0.2 }), ev(11, { progress: 0.9 })]
    state = applyEvents(state, replayed)

    expect(state.lastSeq).toBe(11)
    expect(state.progress).toBe(0.9)
    const ids = state.events.map((e) => e.event_id)
    expect(ids).not.toContain(5)
    expect(ids).toContain(10)
    expect(ids).toContain(11)
  })

  it('完全重复的补发批次不产生任何变化', () => {
    let state = applySnapshot(initialState(), {
      job: job(), stat: null, progress: 0.5, events: [ev(1), ev(2)],
    })
    const before = state
    state = applyEvents(state, [ev(1), ev(2)])

    // 引用相等即可证明没有多做工作（也是 React 避免重渲染的依据）。
    expect(state).toBe(before)
    expect(state.events).toHaveLength(2)
  })

  it('乱序到达的事件按 seq 排序后应用', () => {
    let state = applySnapshot(initialState(), {
      job: job(), stat: null, progress: 0, events: [],
    })
    // 补发与实时交错：3 先到，1、2 后到。
    state = applyEvents(state, [ev(3), ev(1), ev(2)])

    expect(state.events.map((e) => e.event_id)).toEqual([1, 2, 3])
    expect(state.lastSeq).toBe(3)
  })

  it('空批次不会破坏已有状态', () => {
    const state = applySnapshot(initialState(), {
      job: job(), stat: null, progress: 0.4, events: [ev(1)],
    })
    expect(applyEvents(state, [])).toBe(state)
  })

  it('进度被钳制在 0~1', () => {
    let state = initialState()
    state = applyEvents(state, [ev(1, { progress: 5 })])
    expect(state.progress).toBe(1)

    state = applyEvents(state, [ev(2, { progress: -3 })])
    // 负数（无效值）应被忽略而不是把进度拉回 0：
    // 服务端不写 progress 时字段缺失，写成 0 会把进度条清零。
    expect(state.progress).toBe(1)
  })

  it('事件数量有上限，不会无限增长', () => {
    let state = initialState()
    const many = Array.from({ length: 600 }, (_, i) => ev(i + 1))
    state = applyEvents(state, many)

    expect(state.events.length).toBeLessThanOrEqual(500)
    // 保留的是**最近**的：末尾必须还在。
    expect(state.events[state.events.length - 1].event_id).toBe(600)
  })
})

// ---------------------------------------------------------------------------
// C2：刷新不丢上下文
// ---------------------------------------------------------------------------

describe('applySnapshot —— 刷新后恢复上下文', () => {
  it('快照整体替换状态，且带上历史事件', () => {
    const state = applySnapshot(initialState(), {
      job: job({ status: 'COMPOSING' }),
      stat: { total: 4, approved: 4, failed: 0, awaiting_human: 0, in_progress: 0 },
      progress: 1,
      events: [ev(1), ev(2), ev(3)],
    })

    expect(state.loaded).toBe(true)
    expect(state.job?.status).toBe('COMPOSING')
    expect(state.events).toHaveLength(3)
    expect(state.lastSeq).toBe(3)
    expect(state.progress).toBe(1)
  })

  it('快照是权威基线：本地残留的增量不会被合并进来', () => {
    let state = initialState()
    // 刷新前本地有一堆旧事件。
    state = applyEvents(state, [ev(1), ev(2), ev(3), ev(4), ev(5)])

    // 刷新后服务端给出的历史可能更短（事件已被裁剪）。
    state = applySnapshot(state, {
      job: job(), stat: null, progress: 0.3, events: [ev(1), ev(2)],
    })

    // 必须是 2 条而不是 5 条 —— 合并会让「刷新后看到什么」取决于刷新前的残留。
    expect(state.events).toHaveLength(2)
    expect(state.lastSeq).toBe(2)
  })

  it('非法快照不改变状态（服务端抖动时页面不应被清空）', () => {
    const before = applySnapshot(initialState(), {
      job: job(), stat: null, progress: 0.5, events: [ev(1)],
    })
    expect(applySnapshot(before, null)).toBe(before)
    expect(applySnapshot(before, { stat: null })).toBe(before)
  })

  it('未收到快照时 loaded 为 false（不能把空状态当成「没有分镜」）', () => {
    const state = initialState()
    expect(state.loaded).toBe(false)
    expect(shotsOf(state.job)).toEqual([])
  })
})

describe('applyEventsWithJob —— 事件与任务快照同步更新', () => {
  it('用新任务覆盖分镜明细，同时按规则应用事件', () => {
    let state = applySnapshot(initialState(), {
      job: job({ shots: [shot({ status: 'RENDERING' })] }),
      stat: null, progress: 0.2, events: [ev(1)],
    })

    const updated = job({ shots: [shot({ status: 'APPROVED' })] })
    state = applyEventsWithJob(state, [ev(2, { progress: 0.5 })], updated)

    expect(state.job?.shots?.[0].status).toBe('APPROVED')
    expect(state.lastSeq).toBe(2)
  })

  it('没有新事件时仍能更新任务快照', () => {
    let state = applySnapshot(initialState(), {
      job: job(), stat: null, progress: 0.2, events: [ev(1)],
    })
    const updated = job({ status: 'PARTIAL' })
    state = applyEventsWithJob(state, [], updated)

    expect(state.job?.status).toBe('PARTIAL')
    expect(state.events).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// 派生视图
// ---------------------------------------------------------------------------

describe('deriveStat', () => {
  it('优先使用服务端统计', () => {
    const state = applySnapshot(initialState(), {
      job: job({ shots: [shot({ status: 'PENDING' })] }),
      stat: { total: 9, approved: 8, failed: 1, awaiting_human: 0, in_progress: 0 },
      progress: 1, events: [],
    })
    expect(deriveStat(state).total).toBe(9)
  })

  it('没有服务端统计时本地兜底，且口径一致', () => {
    const state = applySnapshot(initialState(), {
      job: job({
        shots: [
          shot({ shot_id: 'a', status: 'APPROVED' }),
          shot({ shot_id: 'b', status: 'AWAITING_HUMAN' }),
          shot({ shot_id: 'c', status: 'FAILED' }),
          shot({ shot_id: 'd', status: 'RENDERING' }),
        ],
      }),
      stat: null, progress: 0.5, events: [],
    })

    const stat = deriveStat(state)
    expect(stat.total).toBe(4)
    expect(stat.approved).toBe(1)
    expect(stat.awaiting_human).toBe(1)
    expect(stat.failed).toBe(1)
    expect(stat.in_progress).toBe(1)
    // 四项之和必须等于总数，否则界面会出现自相矛盾的计数。
    expect(stat.approved + stat.awaiting_human + stat.failed + stat.in_progress).toBe(stat.total)
  })
})

describe('shotsAwaitingHuman', () => {
  it('只挑出熔断待人工处理的镜头', () => {
    const state = applySnapshot(initialState(), {
      job: job({
        shots: [
          shot({ shot_id: 'a', status: 'APPROVED' }),
          shot({ shot_id: 'b', status: 'AWAITING_HUMAN' }),
          shot({ shot_id: 'c', status: 'AWAITING_HUMAN' }),
        ],
      }),
      stat: null, progress: 0.5, events: [],
    })

    expect(shotsAwaitingHuman(state).map((s) => s.shot_id)).toEqual(['b', 'c'])
  })

  it('任务尚未拆解时返回空数组而不是抛错', () => {
    const state = applySnapshot(initialState(), {
      job: job({ shots: null }), stat: null, progress: 0, events: [],
    })
    expect(shotsAwaitingHuman(state)).toEqual([])
  })
})
