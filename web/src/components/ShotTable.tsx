import { Button, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { engineLabel, formatTime, shotStatus, tagLabel } from '../display'
import type { Shot } from '../types'
import { ShotDetail } from './ShotDetail'
import { useShotActions } from './useShotActions'

const { Text } = Typography

/**
 * 分镜表。
 *
 * 用表格而不是卡片列表：审核台最常见的动作是**扫一遍**「哪个镜头出问题了」，
 * 而横向对齐的列（标签/引擎/时长/状态/尝试）正是为扫读准备的。
 * 细节与操作收进展开行，默认视图只回答"现在怎么样"。
 */
interface Props {
  jobId: string
  shots: Shot[]
  /** 任务已进入终态时禁止再操作，避免发出注定被拒的请求。 */
  disabled: boolean
  onChanged: () => void
}

export function ShotTable({ jobId, shots, disabled, onChanged }: Props) {
  const columns: ColumnsType<Shot> = [
    {
      title: '#',
      dataIndex: 'index',
      width: 60,
      render: (index: number) => <Text strong>#{index}</Text>,
    },
    {
      title: '标签',
      dataIndex: 'tag',
      width: 100,
      render: (tag: string) => (
        <Tag color="geekblue" bordered={false}>
          {tagLabel(tag)}
        </Tag>
      ),
    },
    {
      title: '引擎',
      dataIndex: 'engine',
      width: 110,
      render: (engine: string) => <Text type="secondary">{engineLabel(engine)}</Text>,
    },
    {
      title: '时长',
      dataIndex: 'duration_sec',
      width: 90,
      render: (sec: number) => <Text type="secondary">{formatTime(sec)}</Text>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 130,
      render: (status: string) => {
        const st = shotStatus(status)
        return (
          <Tag color={st.tag} bordered={false}>
            {st.label}
          </Tag>
        )
      },
    },
    {
      title: '尝试',
      dataIndex: 'attempt',
      width: 80,
      render: (attempt: number) => <Text type="secondary">第 {attempt} 次</Text>,
    },
    {
      title: '画外音',
      dataIndex: 'narration',
      // 单行截断：完整内容在展开行里，这里只用来快速辨认是哪一镜。
      ellipsis: { showTitle: false },
      render: (narration: string) => (
        <Tooltip title={narration} placement="topLeft">
          <Text type="secondary">{narration || '（空）'}</Text>
        </Tooltip>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      fixed: 'right',
      render: (_, shot) =>
        shot.status === 'AWAITING_HUMAN' ? (
          // 唯一一个不需要读细节就能做的决定，所以给快捷入口。
          <QuickApprove jobId={jobId} shot={shot} disabled={disabled} onChanged={onChanged} />
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>
            —
          </Text>
        ),
    },
  ]

  return (
    <Table<Shot>
      rowKey="shot_id"
      columns={columns}
      dataSource={shots}
      size="middle"
      pagination={false}
      // 窄屏时横向滚动，而不是把列挤成一条缝（挤过之后中文列名会竖排）。
      scroll={{ x: 960 }}
      rowClassName={(shot) => (shot.status === 'AWAITING_HUMAN' ? 'row-actionable' : '')}
      expandable={{
        expandedRowRender: (shot) => (
          <ShotDetail jobId={jobId} shot={shot} disabled={disabled} onChanged={onChanged} />
        ),
        // 「待人工」的镜头默认展开：它是唯一需要人立刻处理的那一类，
        // 让用户还要再点一下才看得到操作，是最没必要的摩擦。
        defaultExpandedRowKeys: shots
          .filter((s) => s.status === 'AWAITING_HUMAN')
          .map((s) => s.shot_id),
      }}
    />
  )
}

/** 表格行内的快速放行按钮。 */
function QuickApprove({
  jobId,
  shot,
  disabled,
  onChanged,
}: {
  jobId: string
  shot: Shot
  disabled: boolean
  onChanged: () => void
}) {
  const { approve, busy } = useShotActions(jobId, shot, onChanged)
  return (
    <Space>
      <Button
        size="small"
        type="primary"
        loading={busy}
        disabled={disabled}
        onClick={() => void approve()}
      >
        放行
      </Button>
    </Space>
  )
}
