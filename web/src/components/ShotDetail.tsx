import { useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
} from 'antd'

import { SHOT_BACKGROUND_OPTIONS, backgroundLabel, shotStatus } from '../display'
import type { Shot } from '../types'
import { ReviewHistory } from './ReviewHistory'
import { ShotMedia } from './ShotMedia'
import { needsShotDecision, useShotActions } from './useShotActions'

const { Text, Paragraph } = Typography

/**
 * 展开行：分镜的完整信息与三种人工操作。
 *
 * 操作放在展开区而不是常驻在行上：审核台一行只该回答「这个镜头现在怎么样」，
 * 而「我要怎么改它」需要看完整描述才能决定 —— 把两者塞在同一行会让人在
 * 还没读清楚画面说明时就先点了按钮。
 *
 * 唯一的例外是「待人工」，它在表格行里有一个快速放行按钮（见 ShotTable）：
 * 那是唯一一个"不需要读细节就能做"的决定。
 */
interface Props {
  jobId: string
  shot: Shot
  onChanged: () => void
}

type Editor = 'none' | 'reject' | 'patch'

export function ShotDetail({ jobId, shot, onChanged }: Props) {
  const { approve, reject, patch, busy, canApprove, canRepair } = useShotActions(jobId, shot, onChanged)
  const [editor, setEditor] = useState<Editor>('none')
  const [comment, setComment] = useState('')
  const [narration, setNarration] = useState(shot.narration)
  const [visualBrief, setVisualBrief] = useState(shot.visual_brief)
  const [backgroundStyle, setBackgroundStyle] = useState(shot.background_style ?? '')
  const [redo, setRedo] = useState(true)

  const st = shotStatus(shot.status)
  const needsDecision = needsShotDecision(shot.status)

  function openEditor(kind: Editor) {
    if (kind === 'patch') {
      // 每次打开都用当前服务端值重置，避免上一次的编辑残留被误提交。
      setNarration(shot.narration)
      setVisualBrief(shot.visual_brief)
      setBackgroundStyle(shot.background_style ?? '')
    }
    if (kind === 'reject') setComment('')
    setEditor((prev) => (prev === kind ? 'none' : kind))
  }

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {needsDecision && (
        <Alert
          type="warning"
          showIcon
          message="这段镜头需要处理"
          description={shot.artifact?.video_path
            ? '可以打回重做；若确认预览中的现有视频可用，也可以人工放行。放行前请核对画面和审核意见。'
            : '尚无视频可放行。请点击自动诊断修复，系统会读取报错和审核记录并重做镜头。'}
        />
      )}

      {shot.error && <Alert type="error" showIcon message="该镜头报错" description={shot.error} />}

      {shot.artifact?.video_path && <ShotMedia key={shot.artifact.artifact_id} jobId={jobId}
        shotId={shot.shot_id} artifact={shot.artifact} onRefresh={onChanged} />}

      <Descriptions
        size="small"
        column={1}
        bordered
        // 标签列定宽并禁止换行：默认宽度会让「标签/引擎」折成两行，
        // 而折行后每一行的 label 高度都不一样，整块看起来像没对齐。
        // 用 styles.label 而不是已废弃的 labelStyle。
        styles={{ label: { width: 104, whiteSpace: 'nowrap' } }}
        items={[
        {
          key: 'narration',
          label: '画外音',
          children: shot.narration ? (
            <Paragraph style={{ margin: 0 }}>{shot.narration}</Paragraph>
          ) : (
            <Text type="secondary">（空）</Text>
          ),
        },
        {
          key: 'brief',
          label: '画面',
          children: shot.visual_brief ? (
            <Paragraph style={{ margin: 0 }}>{shot.visual_brief}</Paragraph>
          ) : (
            <Text type="secondary">（空）</Text>
          ),
        },
        {
          key: 'tag',
          label: '标签/引擎',
          children: (
            <Space size={6}>
              <Tag color="geekblue" bordered={false}>
                {shot.tag}
              </Tag>
              <Text code>{shot.engine}</Text>
              <Tag color={st.tag} bordered={false}>
                {st.label}
              </Tag>
            </Space>
          ),
        },
        {
          key: 'background',
          label: '背景样式',
          children: shot.background_style ? (
            <Space size={6}>
              <Tag color="cyan" bordered={false}>
                {backgroundLabel(shot.background_style)}
              </Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                逐镜头覆盖（全片默认为 style_guide.background_style）
              </Text>
            </Space>
          ) : (
            <Text type="secondary">跟随全片</Text>
          ),
        },
        {
          key: 'artifact',
          label: '产物',
          children: shot.artifact?.video_path ? (
            // 路径用 code 排版：出问题时它是要被复制出去排查的东西。
            <Text code copyable style={{ fontSize: 12 }}>
              {shot.artifact.video_path}
            </Text>
          ) : (
            <Text type="secondary">尚未产出视频</Text>
          ),
        },
      ]} />

      <Space wrap>
        <Button type="primary" loading={busy} disabled={!canRepair} onClick={() => void reject()}>
          自动诊断修复
        </Button>
        {needsDecision && (
          <Button loading={busy} disabled={!canApprove} onClick={() => void approve()}>
            放行并继续
          </Button>
        )}
        <Button disabled={busy || !canRepair} onClick={() => openEditor('reject')}>
          补充意见并修复
        </Button>
        <Button disabled={busy || !canRepair} onClick={() => openEditor('patch')}>
          编辑文案
        </Button>
      </Space>

      {/* 审查依据放在**操作按钮之后**：先让审核员看到"能做什么"，
          再往下读"凭什么这样判断"。反过来会让人在还没看清按钮时就先读了细节。 */}
      <Divider orientation="left" plain style={{ margin: '4px 0' }}>
        审查依据
      </Divider>
      <ReviewHistory feedbacks={shot.feedbacks} />

      {/* --- 打回：把意见回灌给编码智能体 --- */}
      <Modal
        title="补充意见并修复"
        open={editor === 'reject'}
        onCancel={() => setEditor('none')}
        okText="开始修复"
        cancelText="取消"
        confirmLoading={busy}
        onOk={async () => {
          // 成功后由 hook 弹提示、回源；这里只负责关窗。
          if (await reject(comment)) setEditor('none')
        }}
        okButtonProps={{ disabled: !canRepair }}
      >
        <Paragraph type="secondary" style={{ fontSize: 13 }}>
          只需描述看到的问题或期望，也可以留空。程序会结合完整报错和 AI 审核记录定位原因，
          在最多 3 轮内修复、渲染和复审；反复失败时重建布局。原视频保留，审核标准不变。
        </Paragraph>
        <Input.TextArea
          rows={4}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          aria-label="补充修复意见（可选）"
          maxLength={2000}
          placeholder="例如：字被挡住了、图示不清楚；也可留空交给程序诊断"
        />
      </Modal>

      {/* --- 编辑文案：改分镜字段，比整镜重写更省 --- */}
      <Modal
        title="编辑文案"
        open={editor === 'patch'}
        onCancel={() => setEditor('none')}
        width={640}
        footer={[
          <Button key="cancel" onClick={() => setEditor('none')}>
            取消
          </Button>,
          <Button
            key="save"
            loading={busy}
            onClick={async () => {
              if (await patch(narration, visualBrief, false, '', backgroundStyle)) setEditor('none')
            }}
          >
            仅保存
          </Button>,
          <Button
            key="redo"
            type="primary"
            loading={busy}
            onClick={async () => {
              if (await patch(narration, visualBrief, true, comment, backgroundStyle)) setEditor('none')
            }}
          >
            保存并重做
          </Button>,
        ]}
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <div>
            <Text type="secondary">画外音</Text>
            <Input.TextArea
              rows={3}
              value={narration}
              onChange={(e) => setNarration(e.target.value)}
              placeholder="这一镜要念出来的话"
            />
          </div>
          <div>
            <Text type="secondary">画面说明</Text>
            <Input.TextArea
              rows={4}
              value={visualBrief}
              onChange={(e) => setVisualBrief(e.target.value)}
              placeholder="这一镜画面要画什么 —— 它是渲染提示词的直接来源"
            />
          </div>
          <div>
            <Text type="secondary">背景样式</Text>
            <Select
              style={{ width: '100%' }}
              value={backgroundStyle}
              onChange={setBackgroundStyle}
              options={SHOT_BACKGROUND_OPTIONS}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              只影响这一镜的配色；「跟随全片」表示不覆盖。
            </Text>
          </div>
          <Space>
            <Switch checked={redo} onChange={setRedo} />
            <Text type="secondary">
              立即重做（关闭则只保存文案，不动已渲染的视频）
            </Text>
          </Space>
          {redo && (
            <Input.TextArea
              rows={2}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="可选：给这次重做的额外说明"
            />
          )}
        </Space>
      </Modal>
    </Space>
  )
}
