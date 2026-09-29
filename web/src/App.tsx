import { useEffect, useState } from 'react'

import { api, errorText } from './api'
import { EventTimeline } from './components/EventTimeline'
import { ShotRow } from './components/ShotRow'
import { deriveStat, shotsOf } from './stream'
import { useJobStream } from './useJobStream'
import { formatTime, jobStatus } from './display'
import type { ConnectionState, Effects } from './types'

/**
 * SciDirector 分镜审核台。
 *
 * 页面组织遵循「先看结论，再看细节，最后看过程」：
 *   ① 任务头（状态 + 进度 + 计数）—— 一眼知道整体怎么样；
 *   ② 待人工处理提示 —— 需要我做什么；
 *   ③ 分镜表 —— 逐个确认与操作；
 *   ④ 事件时间线 —— 出问题时再往下看。
 *
 * 连接状态始终可见（包括重连中）。断网时如果界面没有任何提示，
 * 用户只会以为「系统卡住了」，然后刷新页面 —— 而实际上它正在自动恢复。
 */

const CONNECTION_TEXT: Record<ConnectionState, { label: string; color: string }> = {
  connecting: { label: '连接中…', color: '#8a94a6' },
  open: { label: '实时', color: '#2ecc71' },
  reconnecting: { label: '重连中…', color: '#f1c40f' },
  closed: { label: '已断开', color: '#e74c3c' },
}

export function App() {
  const [script, setScript] = useState('')
  const [duration, setDuration] = useState(60)
  const [jobId, setJobId] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [refreshTick, setRefreshTick] = useState(0)

  // --- 风格（影响生成）与后期效果（影响合成）---
  //
  // 这两组配置刻意分开放：前者会被注入提示词、决定模型"画什么颜色"，
  // 后者是合成阶段的 ffmpeg 处理。混在一起用户就分不清自己改的是哪一段。
  const [preset, setPreset] = useState('default')
  const [grade, setGrade] = useState('none')
  const [gradeStrength, setGradeStrength] = useState(1)
  const [fadeIn, setFadeIn] = useState(0)
  const [fadeOut, setFadeOut] = useState(0)
  const [burnSubtitles, setBurnSubtitles] = useState(false)

  // BGM：上传后记住 asset_id。**只传 id，不传路径** ——
  // 路径由服务端从素材库解析，请求方拿不到"读服务端任意文件"的能力。
  const [bgm, setBgm] = useState<{ assetId: string; filename: string; durationSec: number } | null>(null)
  const [bgmVolume, setBgmVolume] = useState(-8)
  const [bgmBusy, setBgmBusy] = useState(false)
  const [bgmError, setBgmError] = useState('')

  const { state, connection, lastError, reconnect, mergeDetail } = useJobStream({
    jobId,
    onNeedJobRefresh: () => setRefreshTick((n) => n + 1),
  })

  // 回源任务明细。
  //
  // 触发时机有三类：
  //   - 实时事件到达时（事件里不含分镜明细，不回源就看不到推进）；
  //   - 人工操作之后（同上）；
  //   - WS 断线重连之后（断线期间分镜表可能已经变了）。
  // 用 tick 而不是把 setState 暴露出去：让「什么时候该回源」集中在 App 这一层，
  // hook 只负责连接本身与状态归约。
  useEffect(() => {
    if (!jobId || refreshTick === 0) return
    let cancelled = false
    void (async () => {
      try {
        const resp = await api.getJob(jobId)
        if (cancelled) return
        // 注意 `data` 这一层 —— 查询接口在信封之内还有一层（见 types.ts 的说明）。
        // 三样一起合并：只换 job 会让统计与进度条停在旧值上。
        mergeDetail({ job: resp.job, stat: resp.stat, progress: resp.progress })
      } catch {
        // 回源失败不影响实时流：下次重连时快照会补齐。
      }
    })()
    return () => {
      cancelled = true
    }
  }, [jobId, refreshTick, mergeDetail])

  const stat = deriveStat(state)
  const shots = shotsOf(state.job)
  const js = jobStatus(state.job?.status ?? 'PENDING')
  const conn = CONNECTION_TEXT[connection]
  const pending = stat.awaiting_human

  async function uploadBgm(file: File) {
    setBgmBusy(true)
    setBgmError('')
    try {
      const resp = await api.uploadAsset(file)
      setBgm({ assetId: resp.asset_id, filename: resp.filename, durationSec: resp.duration_sec })
    } catch (err) {
      setBgm(null)
      setBgmError(errorText(err))
    } finally {
      setBgmBusy(false)
    }
  }

  async function submit() {
    setSubmitting(true)
    setSubmitError('')
    try {
      const effects: Effects = {}
      if (grade !== 'none') {
        effects.grade = grade
        effects.grade_strength = gradeStrength
      }
      if (fadeIn > 0) effects.fade_in_sec = fadeIn
      if (fadeOut > 0) effects.fade_out_sec = fadeOut
      if (burnSubtitles) effects.burn_subtitles = true
      if (bgm) effects.bgm = { asset_id: bgm.assetId, volume_db: bgmVolume, loop: true }

      const resp = await api.generate({
        raw_script: script.trim(),
        target_duration_sec: duration,
        locale: 'zh-CN',
        style_guide: { preset },
        effects,
      })
      setJobId(resp.job_id)
    } catch (err) {
      setSubmitError(errorText(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="app">
      <header className="app-header">
        <div>
          <h1>SciDirector · 分镜审核台</h1>
          <p className="muted">导演 → 编码 → 渲染 → 视觉审查 → 人类反馈闭环</p>
        </div>
        <div className="conn" title={lastError || undefined}>
          <span className="dot" style={{ background: conn.color }} />
          <span style={{ color: conn.color }}>{conn.label}</span>
          {connection === 'reconnecting' && (
            <button className="btn btn-sm" onClick={reconnect}>
              立即重试
            </button>
          )}
        </div>
      </header>

      {lastError && connection !== 'open' && (
        <p className="banner banner-warn">连接提示：{lastError}</p>
      )}

      {!jobId && (
        <section className="card">
          <h2>提交脚本</h2>
          <textarea
            className="script-input"
            rows={6}
            value={script}
            onChange={(e) => setScript(e.target.value)}
            placeholder="把要讲解的科学内容贴进来，例如：用三分钟解释傅里叶变换的直觉……"
          />
          <div className="row">
            <label>
              目标时长（秒）
              <input
                type="number"
                min={10}
                max={600}
                value={duration}
                onChange={(e) => setDuration(Number(e.target.value) || 60)}
              />
            </label>
            <button
              className="btn btn-primary"
              disabled={submitting || script.trim().length < 20}
              onClick={() => void submit()}
            >
              {submitting ? '提交中…' : '开始生成'}
            </button>
            {script.trim().length > 0 && script.trim().length < 20 && (
              <span className="muted">脚本至少 20 字</span>
            )}
          </div>

          {/* 风格：影响**生成**（会进提示词）。 */}
          <details className="effects">
            <summary>风格与效果（可选）</summary>
            <div className="row">
              <label>
                风格预设
                <select value={preset} onChange={(e) => setPreset(e.target.value)}>
                  <option value="default">默认（蓝）</option>
                  <option value="tech">科技（青绿）</option>
                  <option value="warm">暖色（橙）</option>
                  <option value="minimal">极简（灰白）</option>
                  <option value="nature">自然（绿）</option>
                  <option value="sunset">日落（粉紫）</option>
                </select>
              </label>

              <label>
                后期色调
                <select value={grade} onChange={(e) => setGrade(e.target.value)}>
                  <option value="none">不调色</option>
                  <option value="warm">暖</option>
                  <option value="cool">冷</option>
                  <option value="high_contrast">高对比</option>
                  <option value="film">胶片感</option>
                </select>
              </label>

              <label>
                强度 {Math.round(gradeStrength * 100)}%
                <input
                  type="range"
                  min={10}
                  max={100}
                  value={Math.round(gradeStrength * 100)}
                  disabled={grade === 'none'}
                  onChange={(e) => setGradeStrength(Number(e.target.value) / 100)}
                />
              </label>
            </div>

            <div className="row">
              <label>
                片头淡入（秒）
                <input
                  type="number" min={0} max={5} step={0.5} value={fadeIn}
                  onChange={(e) => setFadeIn(Number(e.target.value) || 0)}
                />
              </label>
              <label>
                片尾淡出（秒）
                <input
                  type="number" min={0} max={5} step={0.5} value={fadeOut}
                  onChange={(e) => setFadeOut(Number(e.target.value) || 0)}
                />
              </label>
              <label className="check">
                <input
                  type="checkbox" checked={burnSubtitles}
                  onChange={(e) => setBurnSubtitles(e.target.checked)}
                />
                字幕烧进画面
              </label>
            </div>

            <div className="row">
              <label>
                背景音乐
                <input
                  type="file"
                  accept="audio/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus"
                  disabled={bgmBusy}
                  onChange={(e) => {
                    const f = e.target.files?.[0]
                    if (f) void uploadBgm(f)
                  }}
                />
              </label>
              <label>
                配乐音量 {bgmVolume} dB
                <input
                  type="range" min={-30} max={0} value={bgmVolume}
                  disabled={!bgm}
                  onChange={(e) => setBgmVolume(Number(e.target.value))}
                />
              </label>
            </div>

            {bgmBusy && <p className="muted">上传中…</p>}
            {bgm && (
              <p className="muted">
                已选择：{bgm.filename}（{formatTime(bgm.durationSec)}）
                {/* 比成片短时会自动循环，提前说清楚，免得用户以为是 bug。 */}
                <button className="btn btn-sm" onClick={() => setBgm(null)}>移除</button>
              </p>
            )}
            {bgmError && <p className="banner banner-error">{bgmError}</p>}
          </details>
          {submitError && <p className="banner banner-error">{submitError}</p>}
        </section>
      )}

      {jobId && (
        <>
          <section className="card">
            <div className="row row-between">
              <h2>
                任务 <code>{jobId}</code>
              </h2>
              <div className="row">
                <span className="pill" style={{ background: js.color }}>
                  {js.label}
                </span>
                <button className="btn btn-sm" onClick={() => setJobId(null)}>
                  新建任务
                </button>
              </div>
            </div>

            {!state.loaded && <p className="muted">正在获取任务快照…</p>}

            <div className="progress">
              <div className="progress-bar" style={{ width: `${Math.round(state.progress * 100)}%` }} />
            </div>
            <div className="row row-stat">
              <span>进度 {Math.round(state.progress * 100)}%</span>
              <span>共 {stat.total} 个分镜</span>
              <span className="ok">已通过 {stat.approved}</span>
              <span className="warn">待人工 {stat.awaiting_human}</span>
              <span className="danger">失败 {stat.failed}</span>
              <span>进行中 {stat.in_progress}</span>
            </div>

            {state.job?.final_video_path && (
              <div className="film">
                {/* 直接播，而不是只给一个文件路径让用户自己去文件夹里找。
                    artifact 接口支持 Range，所以进度条能拖动。 */}
                <video controls preload="metadata" src={api.artifactUrl(String(jobId))} />
                <p className="shot-artifact">
                  成片：<code>{state.job.final_video_path}</code>
                </p>
              </div>
            )}
            {state.job?.error && <p className="banner banner-error">{state.job.error}</p>}
          </section>

          {pending > 0 && (
            <p className="banner banner-action">
              有 {pending} 个镜头自动重试已达上限，需要你确认：可以「打回重做」给出具体修改意见，
              也可以「放行并继续」接受当前效果。
            </p>
          )}

          <section className="card">
            <h2>分镜表</h2>
            {shots.length === 0 ? (
              <p className="muted">
                {state.loaded ? '导演智能体尚未拆解出分镜。' : '加载中…'}
              </p>
            ) : (
              <div className="shot-list">
                {shots.map((s) => (
                  <ShotRow
                    key={s.shot_id}
                    jobId={jobId}
                    shot={s}
                    // 任务已进入终态时禁止再操作，避免发出注定被拒的请求。
                    disabled={js.terminal === true}
                    onChanged={() => setRefreshTick((n) => n + 1)}
                  />
                ))}
              </div>
            )}
          </section>

          <section className="card">
            <div className="row row-between">
              <h2>事件时间线</h2>
              <span className="muted">
                共 {state.events.length} 条 · 已同步至 #{state.lastSeq}
              </span>
            </div>
            <EventTimeline events={state.events} />
          </section>

          {state.job && (
            <section className="card card-quiet">
              <h2>任务概览</h2>
              <p className="muted">
                目标时长 {formatTime(state.job.target_duration_sec)} · 语言 {state.job.locale} ·
                创建于 {new Date(state.job.created_at).toLocaleString('zh-CN')}
              </p>
              <details>
                <summary>原始脚本</summary>
                <pre className="script-view">{state.job.raw_script}</pre>
              </details>
            </section>
          )}
        </>
      )}
    </main>
  )
}
