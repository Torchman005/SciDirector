import { formatClock, shotStatus } from '../display'
import type { DomainEvent } from '../types'

/**
 * 事件时间线。
 *
 * 它是排查问题的**唯一可信来源**：分镜表只反映「当前状态」，
 * 而这里能看到「怎么变成这样的」—— 尤其是熔断之前那几次尝试。
 *
 * 按 seq 倒序展示（最新在上）：长跑任务的事件会有几百条，
 * 顺序展示意味着每次都要滚到底部才能看到最新进展。
 */

interface Props {
  events: DomainEvent[]
  /** 是否自动滚动（正序展示时才有意义；当前为倒序，保留给未来切换）。 */
  compact?: boolean
}

export function EventTimeline({ events, compact }: Props) {
  if (events.length === 0) {
    return <p className="muted">暂无事件。提交任务后这里会实时出现进展。</p>
  }

  // 倒序：最新的事件在最上面。
  const ordered = [...events].reverse()

  return (
    <ol className={`timeline${compact ? ' timeline-compact' : ''}`}>
      {ordered.map((ev) => {
        const st = ev.status ? shotStatus(ev.status) : null
        return (
          <li key={ev.event_id} className={ev.error ? 'timeline-item has-error' : 'timeline-item'}>
            <span className="timeline-seq">#{ev.event_id}</span>
            <span className="timeline-clock">{formatClock(ev.timestamp)}</span>
            <span className="timeline-node">{ev.node}</span>
            {ev.shot_id && <span className="timeline-shot">{ev.shot_id}</span>}
            {st && (
              <span className="pill pill-sm" style={{ background: st.color }}>
                {st.label}
              </span>
            )}
            <span className="timeline-msg">{ev.message}</span>
            {typeof ev.progress === 'number' && ev.progress > 0 && (
              <span className="timeline-progress">{Math.round(ev.progress * 100)}%</span>
            )}
          </li>
        )
      })}
    </ol>
  )
}
