import { Button, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { engineLabel, formatTime, shotStatus, tagLabel } from '../display'
import type { Shot } from '../types'
import { ShotDetail } from './ShotDetail'
import { needsShotDecision, useShotActions } from './useShotActions'

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
  onChanged: () => void
}

export function ShotTable({ jobId, shots, onChanged }: Props) {
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
        needsShotDecision(shot.status) ? (
          // 待人工与历史失败镜头都提供立即重做入口。
          <QuickRepair jobId={jobId} shot={shot} onChanged={onChanged} />
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
      rowClassName={(shot) => (needsShotDecision(shot.status) ? 'row-actionable' : '')}
      expandable={{
        expandedRowRender: (shot) => (
          <ShotDetail jobId={jobId} shot={shot} onChanged={onChanged} />
        ),
        // 待人工或失败镜头默认展开，让重做与放行入口直接可见。
        defaultExpandedRowKeys: shots
          .filter((s) => needsShotDecision(s.status))
          .map((s) => s.shot_id),
      }}
    />
  )
}

/** 表格行内的快速放行按钮。 */
function QuickRepair({
  jobId,
  shot,
  onChanged,
}: {
  jobId: string
  shot: Shot
  onChanged: () => void
}) {
  const { reject, busy, canRepair } = useShotActions(jobId, shot, onChanged)
  return (
    <Space>
      <Button
        size="small"
        type="primary"
        loading={busy}
        disabled={!canRepair}
        onClick={() => void reject()}
      >
        自动修复
      </Button>
    </Space>
  )
}
