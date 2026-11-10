import type { Shot } from './types'

export const FY_REVISION_PREFIX = 'SCID_FY_REVISION_V1:'
export const FY_COMMENT_LIMIT = 2000
export const FY_PORTS = ['auto', 'left', 'right', 'top', 'bottom'] as const
export const FY_COLORS = ['ink', 'mute', 'line', 'q', 'k', 'v', 'gold', 'blue'] as const
export type FyPort = typeof FY_PORTS[number]
export type FyColor = typeof FY_COLORS[number]
export interface FyBox { x: number; y: number; width: number; height: number }
export interface FyFrame { time: number; dx: number; dy: number; opacity: number; scale: number; easing: 'linear' | 'smooth' }
export interface FyObject { id: string; name: string; box: FyBox; layer: number; overlap_group: string; role?: 'content' | 'backdrop'; keyframes: FyFrame[] }
export interface FyRelation {
  id: string; source: string; target: string; source_port: FyPort; target_port: FyPort
  color: FyColor; stroke_width: number; arrow: boolean; flow: boolean
}
export interface FyDocument { version: 1; objects: FyObject[]; relations: FyRelation[] }
export interface FySnapshot {
  revision: string; document: FyDocument; width: number; height: number; duration: number; fps: number
  palette: Record<string, string>; styleId: string
  supportsBackdrop: boolean
}

const idPattern = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/
const hashPattern = /^[0-9a-f]{64}$/
const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
export const cloneDocument = (doc: FyDocument): FyDocument => JSON.parse(JSON.stringify(doc))
const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('对象数据格式无效')
  return value as Record<string, unknown>
}
const number = (v: unknown, min: number, max: number): number => {
  if (typeof v !== 'number' || !Number.isFinite(v) || v < min || v > max) throw new Error('对象数值超出范围')
  return v
}
const text = (v: unknown, max: number): string => {
  if (typeof v !== 'string' || [...v].length > max) throw new Error('对象文字格式无效')
  return v
}
const identifier = (v: unknown): string => {
  const id = text(v, 64)
  if (!idPattern.test(id)) throw new Error('对象编号格式无效')
  return id
}
const option = <T extends string>(v: unknown, options: readonly T[]): T => {
  if (!options.includes(v as T)) throw new Error('对象选项格式无效')
  return v as T
}
const bool = (v: unknown): boolean => {
  if (typeof v !== 'boolean') throw new Error('连线开关格式无效')
  return v
}

export function validateFyDocument(value: unknown): FyDocument {
  const d = record(value)
  if (d.version !== 1 || !Array.isArray(d.objects) || !d.objects.length || d.objects.length > 32 ||
    !Array.isArray(d.relations) || d.relations.length > 48) throw new Error('不支持的对象文档版本或数量')
  const objects = d.objects.map(value => {
    const o = record(value), b = record(o.box)
    const box = { x: number(b.x, 0, 1), y: number(b.y, 0, 1), width: number(b.width, Number.MIN_VALUE, 1), height: number(b.height, Number.MIN_VALUE, 1) }
    if (box.x + box.width > 1.000001 || box.y + box.height > 1.000001) throw new Error('对象边界超出画布')
    if (!Array.isArray(o.keyframes) || o.keyframes.length > 32) throw new Error('对象运动数据无效')
    const keyframes = o.keyframes.map(value => {
      const k = record(value)
      return { time: number(k.time, 0, 1), dx: number(k.dx, -1, 1), dy: number(k.dy, -1, 1),
        opacity: number(k.opacity, 0, 1), scale: number(k.scale, .25, 4), easing: option(k.easing, ['linear', 'smooth'] as const) }
    })
    if (keyframes.some((k, i) => i > 0 && k.time <= keyframes[i - 1].time)) throw new Error('运动时间点顺序无效')
    const layer = number(o.layer, -100, 100)
    if (!Number.isInteger(layer)) throw new Error('层级必须为整数')
    const role = o.role === undefined ? {} : { role: option(o.role, ['content', 'backdrop'] as const) }
    return { id: identifier(o.id), name: text(o.name, 160), box, layer, overlap_group: text(o.overlap_group, 64), ...role, keyframes }
  })
  const relations = d.relations.map(value => {
    const r = record(value)
    return { id: identifier(r.id), source: identifier(r.source), target: identifier(r.target),
      source_port: option(r.source_port, FY_PORTS), target_port: option(r.target_port, FY_PORTS), color: option(r.color, FY_COLORS),
      stroke_width: number(r.stroke_width, 1, 12), arrow: bool(r.arrow), flow: bool(r.flow) }
  })
  const ids = [...objects, ...relations].map(o => o.id), objectIds = new Set(objects.map(o => o.id))
  if (new Set(ids).size !== ids.length) throw new Error('对象与连线编号重复')
  if (relations.some(r => r.source === r.target || !objectIds.has(r.source) || !objectIds.has(r.target))) throw new Error('连线端点无效')
  if (relations.some(r => objects.some(o => o.role === 'backdrop' && (o.id === r.source || o.id === r.target)))) throw new Error('连线端点不能是背景对象')
  return { version: 1, objects, relations }
}

export function fySnapshotFromData(value: unknown): FySnapshot {
  const d = record(value)
  if (!d.object_document) throw new Error('该镜头尚无可编辑对象，请先使用新版 fy 生成镜头')
  if (typeof d.revision !== 'string' || !hashPattern.test(d.revision) || d.chapter_chrome !== false ||
    d.explicit_palette !== true || !equal(d.first_ids, ['shot'])) throw new Error('该镜头不支持单镜对象编辑')
  const width = number(d.width, 16, 8192), height = number(d.height, 9, 8192), fps = number(d.fps, 1, 60)
  if (![width, height, fps].every(Number.isInteger) || width * 9 !== height * 16) throw new Error('镜头画布规格无效')
  const palette = record(d.palette)
  for (const key of ['paper', 'card', ...FY_COLORS]) {
    if (typeof palette[key] !== 'string' || !/^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(palette[key] as string)) throw new Error('镜头配色不完整')
  }
  const style = d.style_source ? record(d.style_source) : {}
  const supportsBackdrop = Array.isArray(d.object_features) && d.object_features.includes('backdrop-v1')
  const document = validateFyDocument(d.object_document)
  if (!supportsBackdrop && document.objects.some(o => o.role === 'backdrop')) throw new Error('镜头运行时不支持背景对象')
  return { revision: d.revision, document, width, height, fps, supportsBackdrop,
    duration: number(record(d.timing).dur, .01, 3600), palette: palette as Record<string, string>, styleId: String(style.id ?? '') }
}

export function buildFyRevision(base: FySnapshot, value: FyDocument) {
  const draft = validateFyDocument(value)
  if (!equal(base.document.objects.map(o => [o.id, o.name]), draft.objects.map(o => [o.id, o.name]))) throw new Error('不能增删对象或修改画笔')
  const update_objects = draft.objects.flatMap(o => {
    const before = base.document.objects.find(b => b.id === o.id)!
    const patch: Record<string, unknown> = { id: o.id }
    for (const key of ['box', 'layer', 'overlap_group', 'keyframes'] as const) if (!equal(before[key], o[key])) patch[key] = o[key]
    if ((before.role || 'content') !== (o.role || 'content')) {
      if (!base.supportsBackdrop) throw new Error('旧镜头运行时不支持背景对象，请重新生成后再调整')
      patch.role = o.role || 'content'
    }
    return Object.keys(patch).length > 1 ? [patch] : []
  })
  const upsert_relations = draft.relations.filter(r => !equal(base.document.relations.find(b => b.id === r.id), r))
  const remove_relations = base.document.relations.filter(r => !draft.relations.some(d => d.id === r.id)).map(r => r.id)
  const changed = update_objects.length + upsert_relations.length + remove_relations.length
  const patch = { expect_revision: base.revision,
    ...(update_objects.length ? { update_objects } : {}), ...(upsert_relations.length ? { upsert_relations } : {}),
    ...(remove_relations.length ? { remove_relations } : {}) }
  const comment = FY_REVISION_PREFIX + JSON.stringify(patch)
  return { patch, comment, changed, length: [...comment].length }
}

export interface FyChange { id: string; field: string; before: string; after: string }
export function fyChanges(base: FyDocument, draft: FyDocument): FyChange[] {
  const result: FyChange[] = []
  for (const o of draft.objects) {
    const b = base.objects.find(b => b.id === o.id)!
    for (const [key, label] of [['x', '横向位置'], ['y', '纵向位置'], ['width', '宽度'], ['height', '高度']] as const) {
      if (o.box[key] !== b.box[key]) result.push({ id: o.id, field: label, before: `${+(b.box[key] * 100).toFixed(2)}%`, after: `${+(o.box[key] * 100).toFixed(2)}%` })
    }
    if (o.layer !== b.layer) result.push({ id: o.id, field: '层级', before: String(b.layer), after: String(o.layer) })
    if (o.overlap_group !== b.overlap_group) result.push({ id: o.id, field: '叠放组', before: b.overlap_group || '无', after: o.overlap_group || '无' })
    const roleLabel = (role: FyObject['role']) => role === 'backdrop' ? '背景' : '内容'
    if ((o.role || 'content') !== (b.role || 'content')) result.push({ id: o.id, field: '绘制角色', before: roleLabel(b.role), after: roleLabel(o.role) })
  }
  for (const r of base.relations) if (!draft.relations.some(d => d.id === r.id)) result.push({ id: r.id, field: '连线', before: `${r.source} → ${r.target}`, after: '删除' })
  for (const r of draft.relations) {
    const b = base.relations.find(b => b.id === r.id)
    if (!b) result.push({ id: r.id, field: '连线', before: '无', after: `${r.source} → ${r.target}` })
    else for (const key of ['source', 'target', 'source_port', 'target_port', 'color', 'stroke_width', 'arrow', 'flow'] as const) {
      const labels = { source: '起点', target: '终点', source_port: '起点端口', target_port: '终点端口', color: '颜色', stroke_width: '线宽', arrow: '箭头', flow: '流动' }
      const format = (value: string | number | boolean) => {
        if (key === 'source_port' || key === 'target_port') return { auto: '自动', left: '左', right: '右', top: '上', bottom: '下' }[value as FyPort]
        if (typeof value === 'boolean') return value ? '开启' : '关闭'
        return String(value)
      }
      if (r[key] !== b[key]) result.push({ id: r.id, field: labels[key], before: format(b[key]), after: format(r[key]) })
    }
  }
  return result
}

export interface FyHistory { past: FyDocument[]; present: FyDocument; future: FyDocument[] }
export type FyHistoryAction = { type: 'edit'; document: FyDocument } | { type: 'undo' | 'redo' }
export function fyHistory(state: FyHistory, action: FyHistoryAction): FyHistory {
  if (action.type === 'edit') {
    const doc = validateFyDocument(action.document)
    if (equal(doc, state.present)) return state
    return { past: [...state.past, state.present].slice(-60), present: doc, future: [] }
  }
  if (action.type === 'undo' && state.past.length) return { past: state.past.slice(0, -1), present: state.past.at(-1)!, future: [state.present, ...state.future] }
  if (action.type === 'redo' && state.future.length) return { past: [...state.past, state.present], present: state.future[0], future: state.future.slice(1) }
  return state
}

export function fyBaseIdentity(s: Shot): string {
  return JSON.stringify([s.shot_id, s.code, s.artifact?.artifact_id, s.narration, s.visual_brief, s.duration_sec,
    s.engine, s.language, s.attempt, s.background_style, s.keywords, s.repair_tasks, s.feedbacks])
}

export function moveFyBox(box: FyBox, dx: number, dy: number): FyBox {
  const round = (n: number) => +n.toFixed(5)
  return { ...box, x: round(Math.min(1 - box.width, Math.max(0, box.x + dx))), y: round(Math.min(1 - box.height, Math.max(0, box.y + dy))) }
}
