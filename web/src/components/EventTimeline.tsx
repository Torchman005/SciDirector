import { Space, Tag, Timeline, Tooltip, Typography } from 'antd'

import { formatClock, shotStatus } from '../display'
import type { DomainEvent } from '../types'

const { Text } = Typography

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
}

export function EventTimeline({ events }: Props) {
  if (events.length === 0) {
    return (
      <Text type="secondary">暂无事件。提交任务后这里会实时出现进展。</Text>
    )
  }

  // 倒序：最新的事件在最上面。
  const ordered = [...events].reverse()

  return (
    <Timeline
      // 时间线本身要给足左边距：节点圆点与内容之间挤在一起时，
      // 长消息会显得像挂在圆点上的尾巴。
      style={{ paddingTop: 8 }}
      items={ordered.map((ev) => {
        const st = ev.status ? shotStatus(ev.status) : null
        return {
          // 有错误的节点用红色，让"出问题的那几条"在几百条里一眼可见。
          color: ev.error ? 'red' : st?.color ?? 'gray',
          children: (
            <div className="event-item">
              <Space size={8} wrap>
                <Tooltip title={`事件序号 #${ev.event_id}`}>
                  <Text type="secondary" className="mono">
                    #{ev.event_id}
                  </Text>
                </Tooltip>
                <Text type="secondary" className="mono">
                  {formatClock(ev.timestamp)}
                </Text>
                <Tag bordered={false}>{ev.node}</Tag>
                {ev.shot_id && (
                  <Text type="secondary" className="mono" style={{ fontSize: 12 }}>
                    {ev.shot_id}
                  </Text>
                )}
                {st && (
                  <Tag color={st.tag} bordered={false}>
                    {st.label}
                  </Tag>
                )}
                {typeof ev.progress === 'number' && ev.progress > 0 && (
                  <Tag color="blue" bordered={false}>
                    {Math.round(ev.progress * 100)}%
                  </Tag>
                )}
              </Space>
              <div className="event-message">{ev.message}</div>
              {ev.error && (
                <Text type="danger" className="mono" style={{ fontSize: 12 }}>
                  {ev.error}
                </Text>
              )}
            </div>
          ),
        }
      })}
    />
  )
}
