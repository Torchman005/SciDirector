import { Alert, Space, Tag, Tooltip, Typography } from 'antd'

import type { Feedback, RepairTask } from '../types'

const { Text, Paragraph } = Typography

/**
 * 审查记录：每一次尝试的分数、四维分解与意见。
 *
 * 为什么必须显示它：审核员要决定「放行还是打回」，而**依据全在这里**。
 * 只看"待人工处理"这四个字，人是无法判断该放行还是该重做的 ——
 * 而打回是要**消耗一次尝试额度**的（`HandleRejectShot` 里 `shot.Attempt++`），
 * 靠猜着打回等于烧钱。
 *
 * 两个刻意做出来的细节：
 *  1. **标出未达硬性下限的维度**。默认通过条件是「加权总分 ≥ 0.70」**且**「逻辑 ≥ 0.70」
 *     且「可读 ≥ 0.60」—— 一个镜头完全可能总分 0.80 却因为可读性 0.55 被判负。
 *     不标出来，人只会看到"分数不低怎么会不过"。
 *  2. **指出"同一问题被反复提出"**。实测有镜头连着三次拿到一字不差的意见，
 *     提醒核对实际画面和未关闭任务；重复意见本身不是放行证据。
 */

/** 与 Python 侧 `critic.DIMENSION_FLOORS` / 加权公式保持一致。 */
const DIMENSIONS = [
  { key: 'logic_score', label: '逻辑', weight: 0.35, floor: 0.7 },
  { key: 'readability_score', label: '可读', weight: 0.3, floor: 0.6 },
  { key: 'pacing_score', label: '节奏', weight: 0.2, floor: 0 },
  { key: 'aesthetics_score', label: '美观', weight: 0.15, floor: 0 },
] as const

const REPAIR_STATUS: Record<RepairTask['status'], string> = {
  open: '未修复', partial: '部分改善', resolved: '已验证解决', unverified: '证据不足',
}
const SEVERITY: Record<RepairTask['severity'], string> = { blocking: '阻断', major: '主要', advisory: '建议' }

const SOURCE: Record<string, { label: string; color: string }> = {
  VLM: { label: '机器审查', color: 'blue' },
  HUMAN: { label: '人工打回', color: 'orange' },
  SYSTEM: { label: '渲染错误', color: 'red' },
}

export function ReviewHistory({ feedbacks }: { feedbacks?: Feedback[] }) {
  const list = feedbacks ?? []
  if (list.length === 0) {
    return <Text type="secondary">还没有审查记录（该镜头尚未经过审查）。</Text>
  }

  const repeated = detectRepeatedIssue(list)
  const reviews = list.filter(f => f.source === 'VLM')
  const latest = reviews[reviews.length - 1]
  const previous = reviews[reviews.length - 2]
  const tasks = latest?.repair_tasks ?? []
  const closed = tasks.filter(t => t.status === 'resolved').length
  const blockers = tasks.filter(t => t.severity !== 'advisory' && t.status !== 'resolved').length
  const delta = typeof latest?.score === 'number' && typeof previous?.score === 'number'
    ? latest.score - previous.score : null

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Space size={8} wrap>
        <Tooltip
          title="默认通过线为 0.70（部署可配置），逻辑 ≥ 0.70，可读 ≥ 0.60。致命问题和未解决的阻断/主要问题仍会拦截；以服务端最终结论为准。"
        >
          <Text type="secondary" style={{ cursor: 'help' }}>
            审查记录（共 {list.length} 次）
          </Text>
        </Tooltip>
        {list.length > 1 && <Text type="secondary" style={{ fontSize: 12 }}>最新在上</Text>}
      </Space>

      {latest && <Space wrap>
        {delta !== null && <Text>较上次评分 {delta >= 0 ? '+' : ''}{delta.toFixed(2)}</Text>}
        {tasks.length > 0 && <>
          <Tag color="success">已验证解决 {closed}/{tasks.length}</Tag>
          <Tag color={blockers ? 'error' : 'default'}>待解决的阻断/主要问题 {blockers}</Tag>
        </>}
        <Text type="secondary">评分变化不能代替修复验收。</Text>
      </Space>}

      {repeated && (
        <Alert
          type="info"
          showIcon
          message="最近两次的审查意见完全相同"
          description={
            '请核对实际画面与修复任务的验收条件。重复意见可能来自修改无效或审核不稳定；' +
            '补充具体对象、时间点和期望变化后再重做，不能仅凭意见重复放行。'
          }
        />
      )}

      {[...list].reverse().map((fb, i) => (
        <FeedbackItem key={`${fb.attempt}-${i}`} fb={fb} />
      ))}
    </Space>
  )
}

function FeedbackItem({ fb }: { fb: Feedback }) {
  const src = SOURCE[fb.source] ?? { label: fb.source || '未知来源', color: 'default' }
  const dims = DIMENSIONS.filter((d) => typeof fb[d.key] === 'number')

  return (
    <div className="feedback-item">
      <Space size={8} wrap>
        <Tag color={fb.passed ? 'success' : 'error'} bordered={false}>
          {fb.passed ? '通过' : '未通过'}
        </Tag>
        <Text type="secondary">第 {fb.attempt} 次</Text>
        <Tag color={src.color} bordered={false}>
          {src.label}
        </Tag>
        {typeof fb.score === 'number' && fb.source === 'VLM' && (
          <Text>
            总分{' '}
            <Text strong>
              {fb.score.toFixed(2)}
            </Text>
          </Text>
        )}
      </Space>

      {dims.length > 0 && (
        <div className="feedback-dims">
          {dims.map((d) => {
            const v = fb[d.key] as number
            const belowFloor = d.floor > 0 && v < d.floor
            return (
              <Tooltip
                key={d.key}
                title={
                  d.floor > 0
                    ? `${d.label}权重 ${Math.round(d.weight * 100)}%，硬性下限 ${d.floor}`
                    : `${d.label}权重 ${Math.round(d.weight * 100)}%（无硬性下限）`
                }
              >
                <span className={belowFloor ? 'dim dim-below' : 'dim'}>
                  {d.label} {v.toFixed(2)}
                  {/* 低于硬性下限时给出"低于"字样：它才是判负的直接原因，
                      而总分可能仍然不低。 */}
                  {belowFloor && <em> 低于下限</em>}
                </span>
              </Tooltip>
            )
          })}
        </div>
      )}

      {(fb.issues?.length ?? 0) > 0 && (
        <Paragraph style={{ margin: '6px 0 0', fontSize: 13 }}>
          <Text type="danger">问题：</Text>
          {fb.issues!.join('；')}
        </Paragraph>
      )}
      {(fb.suggestions?.length ?? 0) > 0 && (
        <Paragraph style={{ margin: '2px 0 0', fontSize: 13 }}>
          <Text type="secondary">建议：</Text>
          {fb.suggestions!.join('；')}
        </Paragraph>
      )}
      {!!fb.fatal_issues?.length && <Alert type="error" showIcon message="致命问题" description={fb.fatal_issues.join('；')} />}
      {!!fb.repair_tasks?.length && <ul className="repair-ledger" aria-label={`第 ${fb.attempt} 次修复任务`}>
        {fb.repair_tasks.map(task => <li key={task.task_id}>
          <Space wrap size={6}>
            <Text strong>{task.target || task.task_id}</Text>
            <Tag color={task.status === 'resolved' ? 'success' : task.severity === 'advisory' ? 'default' : 'warning'}>
              {REPAIR_STATUS[task.status] || task.status}
            </Tag>
            <Text type="secondary">{SEVERITY[task.severity] || task.severity} · {task.start_sec.toFixed(1)}–{task.end_sec.toFixed(1)} 秒</Text>
          </Space>
          <Paragraph style={{ margin: '4px 0' }}>证据：{task.evidence}</Paragraph>
          <Paragraph style={{ margin: '4px 0' }}>修改：{task.instruction}</Paragraph>
          <Paragraph style={{ margin: '4px 0' }}>验收：{task.acceptance}</Paragraph>
          {task.resolution_evidence && <Paragraph style={{ margin: '4px 0' }}>复核：{task.resolution_evidence}</Paragraph>}
        </li>)}
      </ul>}
    </div>
  )
}

/**
 * 判断"最近两次审查意见是否完全相同"。
 *
 * 只看 VLM 的意见（人工打回的意见本来就该和机器不同），并且要求两条都非空 ——
 * 空意见之间"相同"没有意义。
 */
function detectRepeatedIssue(list: Feedback[]): boolean {
  const vlm = list.filter((f) => f.source === 'VLM' && (f.issues?.length ?? 0) > 0)
  if (vlm.length < 2) return false
  const a = vlm[vlm.length - 1].issues!.join('|')
  const b = vlm[vlm.length - 2].issues!.join('|')
  return a === b
}
