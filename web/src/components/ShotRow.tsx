import { useState } from 'react'

import { api, errorText } from '../api'
import { engineLabel, formatTime, shotStatus, tagLabel } from '../display'
import type { Shot } from '../types'

/**
 * 单个分镜行：预览、状态、以及三种人工操作。
 *
 * 三种操作的分工（这是审核台的核心心智模型）：
 *   - **放行**：熔断镜头的出口。接受当前效果继续走，不再自动重试。
 *     只在 AWAITING_HUMAN 时出现 —— 别的状态下放行没有意义。
 *   - **打回**：画面不对。意见会回灌给编码智能体重写代码再重渲。
 *   - **编辑文案**：narration/visual_brief 不对。直接改分镜字段再重渲，
 *     比整镜重写更省，也保留导演的原始意图。
 */

interface Props {
  jobId: string
  shot: Shot
  disabled: boolean
  onChanged: () => void
}

type Editor = 'none' | 'reject' | 'patch'

export function ShotRow({ jobId, shot, disabled, onChanged }: Props) {
  const [editor, setEditor] = useState<Editor>('none')
  const [comment, setComment] = useState('')
  const [narration, setNarration] = useState(shot.narration)
  const [visualBrief, setVisualBrief] = useState(shot.visual_brief)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const st = shotStatus(shot.status)
  const isAwaiting = shot.status === 'AWAITING_HUMAN'

  async function run(fn: () => Promise<string>) {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const msg = await fn()
      setNotice(msg)
      setEditor('none')
      setComment('')
      // 操作成功后立刻回源任务明细：事件里不含分镜状态，
      // 不等回源的话用户点完按钮会觉得「没反应」。
      onChanged()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const openEditor = (kind: Editor) => {
    setError('')
    setNotice('')
    if (kind === 'patch') {
      // 每次打开都用当前服务端值重置，避免上一次的编辑残留被误提交。
      setNarration(shot.narration)
      setVisualBrief(shot.visual_brief)
    }
    setEditor((prev) => (prev === kind ? 'none' : kind))
  }

  return (
    <div className="shot-row">
      <div className="shot-main">
        <div className="shot-head">
          <span className="shot-index">#{shot.index}</span>
          <span className="chip">{tagLabel(shot.tag)}</span>
          <span className="chip chip-quiet">{engineLabel(shot.engine)}</span>
          <span className="chip chip-quiet">{formatTime(shot.duration_sec)}</span>
          <span className="pill" style={{ background: st.color }}>
            {st.label}
          </span>
          <span className="shot-attempt">第 {shot.attempt} 次</span>
          {isAwaiting && <span className="hint">已达重试上限，等待人工决策</span>}
        </div>

        <p className="shot-narration">
          <span className="label">画外音</span>
          {shot.narration || <em className="muted">（空）</em>}
        </p>
        <p className="shot-brief">
          <span className="label">画面</span>
          {shot.visual_brief || <em className="muted">（空）</em>}
        </p>

        {shot.error && <p className="shot-error">错误：{shot.error}</p>}

        {shot.artifact?.video_path ? (
          <p className="shot-artifact">
            产物：<code>{shot.artifact.video_path}</code>
          </p>
        ) : (
          <p className="shot-artifact muted">尚未产出视频</p>
        )}

        <div className="shot-actions">
          {isAwaiting && (
            <button
              className="btn btn-primary"
              disabled={disabled || busy}
              onClick={() =>
                void run(async () => {
                  const resp = await api.approve(jobId, shot.shot_id)
                  return resp.compose_enqueued
                    ? '已放行。全部镜头都已通过，成片开始合成。'
                    : '已放行该镜头。'
                })
              }
            >
              放行并继续
            </button>
          )}
          <button
            className="btn"
            disabled={disabled || busy || !shot.artifact?.video_path}
            onClick={() => openEditor('reject')}
            title={shot.artifact?.video_path ? '' : '尚未产出视频，无法打回重做'}
          >
            打回重做
          </button>
          <button className="btn" disabled={disabled || busy} onClick={() => openEditor('patch')}>
            编辑文案
          </button>
        </div>

        {editor === 'reject' && (
          <div className="editor">
            <label className="editor-label">
              打回意见（会回灌给编码智能体，必须可执行）
            </label>
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              rows={3}
              placeholder="例如：坐标轴标签重叠，请把 x 轴标签旋转 45 度并缩小字号"
            />
            <div className="editor-actions">
              <button
                className="btn btn-primary"
                disabled={busy || comment.trim().length < 2}
                onClick={() =>
                  void run(async () => {
                    await api.reject(jobId, shot.shot_id, { comment: comment.trim() })
                    return '已打回，正在重写并重渲该镜头。'
                  })
                }
              >
                确认打回
              </button>
              <button className="btn" disabled={busy} onClick={() => setEditor('none')}>
                取消
              </button>
            </div>
          </div>
        )}

        {editor === 'patch' && (
          <div className="editor">
            <label className="editor-label">画外音（narration）</label>
            <textarea
              value={narration}
              onChange={(e) => setNarration(e.target.value)}
              rows={2}
            />
            <label className="editor-label">画面说明（visual_brief）</label>
            <textarea
              value={visualBrief}
              onChange={(e) => setVisualBrief(e.target.value)}
              rows={2}
            />
            <label className="editor-label">随重做一起回灌的意见（可选）</label>
            <input value={comment} onChange={(e) => setComment(e.target.value)} />
            <div className="editor-actions">
              <button
                className="btn btn-primary"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const resp = await api.patchShot(jobId, shot.shot_id, {
                      narration,
                      visual_brief: visualBrief,
                      redo: true,
                      comment: comment.trim() || undefined,
                    })
                    const changed = resp.changed.length ? resp.changed.join('、') : '无'
                    return `已保存（${changed}）并开始重做。`
                  })
                }
              >
                保存并重做
              </button>
              <button
                className="btn"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const resp = await api.patchShot(jobId, shot.shot_id, {
                      narration,
                      visual_brief: visualBrief,
                      redo: false,
                    })
                    const changed = resp.changed.length ? resp.changed.join('、') : '无'
                    return `已保存（${changed}），未触发重做。`
                  })
                }
              >
                仅保存
              </button>
              <button className="btn" disabled={busy} onClick={() => setEditor('none')}>
                取消
              </button>
            </div>
          </div>
        )}

        {error && <p className="shot-error">{error}</p>}
        {notice && <p className="shot-notice">{notice}</p>}
      </div>
    </div>
  )
}
