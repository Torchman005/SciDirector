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
  /**
   * antd Tag 的预设色名。
   *
   * 单独给一个字段而不是复用 `color`：预设色在暗色主题下会自动调整明度与边框，
   * 而把十六进制直接塞给 Tag 只会得到一个固定的实心块 —— 在深色底上会显得很闷。
   */
  tag: string
  /** 是否为「需要人处理」的状态 —— 审核台要高亮这类。 */
  actionable?: boolean
  /** 是否为终态（不会再变）。 */
  terminal?: boolean
}

export const SHOT_STATUS: Record<ShotStatus, StatusStyle> = {
  PENDING: { label: '待处理', color: '#8a94a6', tag: 'default' },
  GENERATING: { label: '生成中', color: '#4F8CFF', tag: 'processing' },
  RENDERING: { label: '渲染中', color: '#4F8CFF', tag: 'processing' },
  CRITIQUING: { label: '审查中', color: '#9b8cff', tag: 'purple' },
  APPROVED: { label: '已通过', color: '#2ecc71', tag: 'success', terminal: true },
  REJECTED: { label: '已打回', color: '#e67e22', tag: 'warning' },
  RETRYING: { label: '重试中', color: '#f1c40f', tag: 'gold' },
  FAILED: { label: '失败', color: '#e74c3c', tag: 'error', terminal: true },
  // 「熔断待人工」是整条流水线最重要的一个状态：
  // 它不是失败，而是一个**等待人类决策**的出口。
  AWAITING_HUMAN: { label: '待人工处理', color: '#ff9f43', tag: 'orange', actionable: true },
}

export const JOB_STATUS: Record<JobStatus, StatusStyle> = {
  PENDING: { label: '排队中', color: '#8a94a6', tag: 'default' },
  PLANNING: { label: '拆解脚本', color: '#4F8CFF', tag: 'processing' },
  RENDERING: { label: '渲染中', color: '#4F8CFF', tag: 'processing' },
  COMPOSING: { label: '合成中', color: '#9b8cff', tag: 'purple' },
  COMPLETED: { label: '已完成', color: '#2ecc71', tag: 'success', terminal: true },
  // PARTIAL 不是失败：部分镜头产出、部分待人工。
  // 把它显示成红色「失败」会让人误以为整批白做了。
  PARTIAL: { label: '部分完成', color: '#ff9f43', tag: 'orange' },
  FAILED: { label: '失败', color: '#e74c3c', tag: 'error', terminal: true },
  CANCELLED: { label: '已取消', color: '#8a94a6', tag: 'default', terminal: true },
}

/** 未知状态一律原样显示并标灰，绝不猜一个近似的。 */
export function shotStatus(s: string): StatusStyle {
  return SHOT_STATUS[s as ShotStatus] ?? { label: s, color: '#8a94a6', tag: 'default' }
}

export function jobStatus(s: string): StatusStyle {
  return JOB_STATUS[s as JobStatus] ?? { label: s, color: '#8a94a6', tag: 'default' }
}

/** 引擎的展示名。 */
export const ENGINE_LABEL: Record<string, string> = {
  manim: 'Manim',
  d3: 'D3',
  echarts: 'ECharts',
  code_anim: '代码动画',
  // 刻意不叫「动效」：MOTION **标签**的中文已经是「动效」，
  // 两列显示同一个词会让人以为表格渲染错了。引擎这一列回答的是
  // "用什么画的"，所以写上实现方式。
  motion: 'HTML 动效',
  stock: '环境镜头',
}

export function engineLabel(e: string): string {
  return ENGINE_LABEL[e] ?? e ?? '—'
}

/**
 * 配色预设（`style_guide.preset`）。
 *
 * 唯一真源在 Python 的 `STYLE_PRESETS`（它给出具体色值与提示词描述）。
 * 与下面的 `BACKGROUND_STYLES` 是**两件不同的事**：
 * 这个决定"用哪套颜色"，那个决定"背景长什么样"，两者可以自由组合。
 */
export const STYLE_PRESETS: { value: string; label: string }[] = [
  { value: 'default', label: '默认（蓝）' },
  { value: 'tech', label: '科技（青绿）' },
  { value: 'warm', label: '暖色（橙）' },
  { value: 'minimal', label: '极简（灰白）' },
  { value: 'nature', label: '自然（绿）' },
  { value: 'sunset', label: '日落（粉紫）' },
]

/**
 * 背景样式（**图案**，不是配色）。
 *
 * 与配色是两件事，别混：
 *   - `style_guide.preset` 决定**用哪套颜色**（default/tech/warm/…）；
 *   - `style_guide.background_style` 决定**背景长什么样**（纯色/网格/扫描线/…）。
 *
 * id 与中文名都以 Python 侧 `scidirector_ai/backgrounds.py` 为**唯一真源**
 * （Go 侧 `media.BackgroundStyleIDs()` 与它有测试互相钉住）。
 * 这里写错的表现是：用户选了某个样式，保存时被后端 400 拒掉 ——
 * 因此这张表**必须**与那两处逐字一致。
 */
export const BACKGROUND_STYLES: { value: string; label: string }[] = [
  { value: 'auto', label: '自动（按内容决定）' },
  { value: 'solid', label: '纯色' },
  { value: 'gradient', label: '渐变' },
  { value: 'grid', label: '坐标纸网格' },
  { value: 'vignette', label: '暗角聚光' },
  { value: 'noise', label: '细颗粒' },
  { value: 'scanlines', label: '扫描线' },
]

/** 逐镜头背景样式的可选项（含"跟随全片"）。 */
export const SHOT_BACKGROUND_OPTIONS = [
  { value: '', label: '跟随全片' },
  ...BACKGROUND_STYLES,
]

/** 背景样式 id 的中文名（用于展示逐镜头的覆盖值）。 */
export function backgroundLabel(id: string): string {
  return BACKGROUND_STYLES.find((o) => o.value === id)?.label ?? id
}

/** 场景标签的展示名。 */
export const TAG_LABEL: Record<string, string> = {
  SCENE_TAG_MATH: '数学',
  SCENE_TAG_DATA: '数据',
  SCENE_TAG_CODE: '代码',
  SCENE_TAG_AMBIENCE: '氛围',
  SCENE_TAG_MOTION: '动效',
  MATH: '数学',
  DATA: '数据',
  CODE: '代码',
  AMBIENCE: '氛围',
  MOTION: '动效',
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
