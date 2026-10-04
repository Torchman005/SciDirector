import { Alert, Space, Tag, Tooltip, Typography } from 'antd'

import type { Feedback } from '../types'

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
 *  1. **标出未达硬性下限的维度**。通过条件是「加权总分 ≥ 0.75」**且**「逻辑 ≥ 0.70」
 *     且「可读 ≥ 0.60」—— 一个镜头完全可能总分 0.80 却因为可读性 0.55 被判负。
 *     不标出来，人只会看到"分数不低怎么会不过"。
 *  2. **指出"同一问题被反复提出"**。实测有镜头连着三次拿到一字不差的意见，
 *     说明编码智能体没有能力改掉它 —— 再打回一次只是重复消耗，
 *     这种时候放行（或改文案绕开）比继续重做更明智。
 */

/** 与 Python 侧 `critic.DIMENSION_FLOORS` / 加权公式保持一致。 */
const DIMENSIONS = [
  { key: 'logic_score', label: '逻辑', weight: 0.35, floor: 0.7 },
  { key: 'readability_score', label: '可读', weight: 0.3, floor: 0.6 },
  { key: 'pacing_score', label: '节奏', weight: 0.2, floor: 0 },
  { key: 'aesthetics_score', label: '美观', weight: 0.15, floor: 0 },
] as const

/** 通过线（`SCID_CRITIC_SCORE_THRESHOLD` 缺省 0.75）。 */
const SCORE_THRESHOLD = 0.75

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

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Space size={8} wrap>
        <Tooltip
          title={`通过需要同时满足：加权总分 ≥ ${SCORE_THRESHOLD}，逻辑 ≥ 0.70，可读 ≥ 0.60。权重：逻辑 35% / 可读 30% / 节奏 20% / 美观 15%。`}
        >
          <Text type="secondary" style={{ cursor: 'help' }}>
            审查记录（共 {list.length} 次）
          </Text>
        </Tooltip>
        {list.length > 1 && <Text type="secondary" style={{ fontSize: 12 }}>最新在上</Text>}
      </Space>

      {repeated && (
        <Alert
          type="info"
          showIcon
          message="最近两次的审查意见完全相同"
          description={
            '这说明编码智能体没能改掉这个问题 —— 再打回一次很可能得到同样结果，' +
            '而且会再消耗一次尝试额度。可以考虑：放行接受当前效果，或改文案绕开它' +
            '（见下方「编辑文案」）。'
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
        {typeof fb.score === 'number' && fb.score > 0 && (
          <Text>
            总分{' '}
            <Text strong type={fb.score >= SCORE_THRESHOLD ? undefined : 'danger'}>
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
