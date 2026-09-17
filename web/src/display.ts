/**
 * 分镜状态与任务状态的展示映射。
 *
 * 集中在一处的理由：状态色/文案散落在各处时，同一个状态在不同组件里
 * 很容易出现两种说法（「待人工」vs「熔断」），用户会以为是两回事。
 */

import type { JobStatus, ShotStatus } from './types'

export interface StatusStyle {
  label: string
  color: string
  /** 是否为「需要人处理」的状态 —— 审核台要高亮这类。 */
  actionable?: boolean
  /** 是否为终态（不会再变）。 */
  terminal?: boolean
}

export const SHOT_STATUS: Record<ShotStatus, StatusStyle> = {
  PENDING: { label: '待处理', color: '#8a94a6' },
  GENERATING: { label: '生成中', color: '#4F8CFF' },
  RENDERING: { label: '渲染中', color: '#4F8CFF' },
  CRITIQUING: { label: '审查中', color: '#9b8cff' },
  APPROVED: { label: '已通过', color: '#2ecc71', terminal: true },
  REJECTED: { label: '已打回', color: '#e67e22' },
  RETRYING: { label: '重试中', color: '#f1c40f' },
  FAILED: { label: '失败', color: '#e74c3c', terminal: true },
  // 「熔断待人工」是整条流水线最重要的一个状态：
  // 它不是失败，而是一个**等待人类决策**的出口。
  AWAITING_HUMAN: { label: '待人工处理', color: '#ff9f43', actionable: true },
}

export const JOB_STATUS: Record<JobStatus, StatusStyle> = {
  PENDING: { label: '排队中', color: '#8a94a6' },
  PLANNING: { label: '拆解脚本', color: '#4F8CFF' },
  RENDERING: { label: '渲染中', color: '#4F8CFF' },
  COMPOSING: { label: '合成中', color: '#9b8cff' },
  COMPLETED: { label: '已完成', color: '#2ecc71', terminal: true },
  // PARTIAL 不是失败：部分镜头产出、部分待人工。
  // 把它显示成红色「失败」会让人误以为整批白做了。
  PARTIAL: { label: '部分完成', color: '#ff9f43' },
  FAILED: { label: '失败', color: '#e74c3c', terminal: true },
  CANCELLED: { label: '已取消', color: '#8a94a6', terminal: true },
}

/** 未知状态一律原样显示并标灰，绝不猜一个近似的。 */
export function shotStatus(s: string): StatusStyle {
  return SHOT_STATUS[s as ShotStatus] ?? { label: s, color: '#8a94a6' }
}

export function jobStatus(s: string): StatusStyle {
  return JOB_STATUS[s as JobStatus] ?? { label: s, color: '#8a94a6' }
}

/** 引擎的展示名。 */
export const ENGINE_LABEL: Record<string, string> = {
  manim: 'Manim',
  d3: 'D3',
  echarts: 'ECharts',
  code_anim: '代码动画',
  stock: '环境镜头',
}

export function engineLabel(e: string): string {
  return ENGINE_LABEL[e] ?? e ?? '—'
}

/** 场景标签的展示名。 */
export const TAG_LABEL: Record<string, string> = {
  SCENE_TAG_MATH: '数学',
  SCENE_TAG_DATA: '数据',
  SCENE_TAG_CODE: '代码',
  SCENE_TAG_AMBIENCE: '氛围',
  MATH: '数学',
  DATA: '数据',
  CODE: '代码',
  AMBIENCE: '氛围',
}

export function tagLabel(t: string): string {
  return TAG_LABEL[t] ?? t ?? '—'
}

/** 把秒格式化成 mm:ss。 */
export function formatTime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return '00:00'
  const total = Math.floor(sec)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/** 把 ISO 时间格式化成 HH:MM:SS（本地时区）。 */
export function formatClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--:--:--'
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}
