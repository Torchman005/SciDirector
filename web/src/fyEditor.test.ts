import { describe, expect, it } from 'vitest'
import {
  buildFyRevision, cloneDocument, FY_COMMENT_LIMIT, FY_REVISION_PREFIX, fyBaseIdentity,
  fyChanges, fyHistory, fySnapshotFromData, moveFyBox, validateFyDocument,
  type FyDocument, type FySnapshot,
} from './fyEditor'
import type { Shot } from './types'

// Isolated editor test data; never published as a real task or review.
const doc: FyDocument = { version: 1, objects: [
  { id: 'left', name: '输入', box: { x: .1, y: .2, width: .2, height: .2 }, layer: 0, overlap_group: '', keyframes: [] },
  { id: 'right', name: '输出', box: { x: .6, y: .2, width: .2, height: .2 }, layer: 1, overlap_group: '',
    keyframes: [{ time: 0, dx: 0, dy: 0, opacity: 1, scale: 1, easing: 'smooth' }] },
], relations: [{ id: 'link', source: 'left', target: 'right', source_port: 'right', target_port: 'left', color: 'ink', stroke_width: 3, arrow: true, flow: false }] }
const palette = { paper: '#fff', card: '#fff', ink: '#111', mute: '#aaa', line: '#ccc', q: '#f00', k: '#0f0', v: '#00f', gold: '#fc0', blue: '#08f' }
const payload = { revision: 'a'.repeat(64), object_document: doc, width: 1920, height: 1080, fps: 30,
  timing: { dur: 6 }, palette, chapter_chrome: false, explicit_palette: true, first_ids: ['shot'], style_source: { id: 'X17' } }
const base: FySnapshot = fySnapshotFromData(payload)

describe('fy object editing contract', () => {
  it('uses the persisted version and preserves the complete motion data', () => {
    expect(base.revision).toBe(payload.revision)
    expect(base.document).toEqual(doc)
    expect(base.styleId).toBe('X17')
  })
  it('rejects legacy fy without an object document', () => {
    expect(() => fySnapshotFromData({ ...payload, object_document: null })).toThrow('尚无可编辑对象')
  })
  it.each([{ revision: '' }, { first_ids: ['chapter'] }, { chapter_chrome: true }, { fps: 61 }, { width: 100 }, { palette: {} }])('rejects unsupported snapshot %o', change => {
    expect(() => fySnapshotFromData({ ...payload, ...change })).toThrow()
  })
  it('sends only the changed object field and the original expected revision', () => {
    const draft = cloneDocument(doc)
    draft.objects[0].box.x = .12
    const result = buildFyRevision(base, draft)
    expect(result.patch).toEqual({ expect_revision: base.revision, update_objects: [{ id: 'left', box: draft.objects[0].box }] })
    expect(result.comment.startsWith(FY_REVISION_PREFIX)).toBe(true)
    expect(JSON.parse(result.comment.slice(FY_REVISION_PREFIX.length))).toEqual(result.patch)
    expect(doc.objects[0].box.x).toBe(.1)
    expect(result.changed).toBe(1)
  })
  it('does not submit unchanged fields, empty edits or painters', () => {
    const result = buildFyRevision(base, cloneDocument(doc))
    expect(result.changed).toBe(0)
    expect(result.patch).toEqual({ expect_revision: base.revision })
    expect(result.comment).not.toContain('code')
    expect(result.comment).not.toContain('narration')
  })
  it('encodes full relation replacements and removals without touching objects', () => {
    const draft = cloneDocument(doc)
    draft.relations[0].target_port = 'top'
    expect(buildFyRevision(base, draft).patch).toEqual({ expect_revision: base.revision, upsert_relations: draft.relations })
    draft.relations = []
    expect(buildFyRevision(base, draft).patch).toEqual({ expect_revision: base.revision, remove_relations: ['link'] })
  })
  it('supports a new uniquely named relation', () => {
    const draft = cloneDocument(doc)
    draft.relations.push({ ...doc.relations[0], id: 'new-link' })
    expect(buildFyRevision(base, draft).patch.upsert_relations).toEqual([draft.relations[1]])
  })
  it('preserves explicit backdrop roles and emits role-only revisions', () => {
    const data = { ...payload, object_features: ['backdrop-v1', 'text-block-v1'],
      object_document: { ...cloneDocument(doc), relations: [] } }
    const snapshot = fySnapshotFromData(data)
    const draft = cloneDocument(snapshot.document)
    draft.objects[0].role = 'backdrop'
    expect(validateFyDocument(draft).objects[0].role).toBe('backdrop')
    expect(buildFyRevision(snapshot, draft).patch.update_objects).toEqual([{ id: 'left', role: 'backdrop' }])
    expect(fyChanges(snapshot.document, draft)).toEqual([{ id: 'left', field: '绘制角色', before: '内容', after: '背景' }])
  })
  it('does not silently enable backdrop edits on old inline runtimes', () => {
    const snapshot = fySnapshotFromData({ ...payload, object_document: { ...cloneDocument(doc), relations: [] } })
    const draft = cloneDocument(snapshot.document)
    draft.objects[0].role = 'backdrop'
    expect(snapshot.supportsBackdrop).toBe(false)
    expect(() => buildFyRevision(snapshot, draft)).toThrow('旧镜头运行时不支持')
    expect(() => fySnapshotFromData({ ...payload, object_document: draft })).toThrow('运行时不支持')
  })
  it('rejects backdrop endpoints rather than dropping their relations', () => {
    const draft = cloneDocument(doc)
    draft.objects[0].role = 'backdrop'
    expect(() => validateFyDocument(draft)).toThrow('端点不能是背景对象')
    expect(draft.relations).toHaveLength(1)
  })
  it('cannot add/remove painter objects or silently rename them', () => {
    const draft = cloneDocument(doc)
    draft.objects[0].name = 'renamed'
    expect(() => buildFyRevision(base, draft)).toThrow('不能增删对象')
    draft.objects.pop()
    draft.relations = []
    expect(() => buildFyRevision(base, draft)).toThrow()
  })
  it.each(['missing', 'left'])('rejects invalid relation targets %s', target => {
    const draft = cloneDocument(doc)
    draft.relations[0].target = target
    expect(() => validateFyDocument(draft)).toThrow('连线端点无效')
  })
  it('rejects duplicate IDs across objects and relations', () => {
    const draft = cloneDocument(doc)
    draft.relations[0].id = 'left'
    expect(() => validateFyDocument(draft)).toThrow('编号重复')
  })
  it.each([NaN, Infinity, -1, 2])('rejects invalid geometry %s', x => {
    const draft = cloneDocument(doc)
    draft.objects[0].box.x = x
    expect(() => validateFyDocument(draft)).toThrow()
  })
  it('rejects unordered frames and overflowing boxes', () => {
    const draft = cloneDocument(doc)
    draft.objects[1].keyframes.push({ ...draft.objects[1].keyframes[0] })
    expect(() => validateFyDocument(draft)).toThrow('时间点顺序')
    draft.objects[1].keyframes.pop()
    draft.objects[0].box.width = 1
    expect(() => validateFyDocument(draft)).toThrow('边界超出')
  })
  it('keeps large patches intact and reports the actual character limit', () => {
    const draft = cloneDocument(doc)
    draft.objects[0].keyframes = Array.from({ length: 32 }, (_, i) => ({ time: i / 32, dx: .1234567, dy: .1234567, scale: 1.123456, opacity: 1, easing: 'smooth' }))
    const result = buildFyRevision(base, draft)
    expect(result.length).toBe([...result.comment].length)
    expect(result.length).toBeGreaterThan(FY_COMMENT_LIMIT)
    expect(JSON.parse(result.comment.slice(FY_REVISION_PREFIX.length)).update_objects[0].keyframes).toHaveLength(32)
  })
  it('clamps dragging to the canvas without changing size or motion', () => {
    expect(moveFyBox(doc.objects[0].box, -5, 10)).toEqual({ x: 0, y: .8, width: .2, height: .2 })
    expect(moveFyBox(doc.objects[0].box, .013333333, 0).x).toBe(.11333)
  })
  it('reports human-readable differences', () => {
    const draft = cloneDocument(doc)
    draft.objects[0].box.x = .15; draft.objects[0].layer = 2; draft.relations = []
    expect(fyChanges(doc, draft)).toEqual([
      { id: 'left', field: '横向位置', before: '10%', after: '15%' },
      { id: 'left', field: '层级', before: '0', after: '2' },
      { id: 'link', field: '连线', before: 'left → right', after: '删除' },
    ])
  })
})

describe('draft history and server changes', () => {
  it('undoes/redoes documents and clears redo after a new edit', () => {
    let state = { past: [] as FyDocument[], present: cloneDocument(doc), future: [] as FyDocument[] }
    const changed = cloneDocument(doc); changed.objects[0].layer = 3
    state = fyHistory(state, { type: 'edit', document: changed })
    expect(state.past).toHaveLength(1)
    state = fyHistory(state, { type: 'undo' })
    expect(state.present).toEqual(doc)
    state = fyHistory(state, { type: 'redo' })
    expect(state.present).toEqual(changed)
    state = fyHistory(state, { type: 'undo' })
    changed.objects[0].layer = 5
    state = fyHistory(state, { type: 'edit', document: changed })
    expect(state.future).toHaveLength(0)
    expect(state.present.objects[0].layer).toBe(5)
  })
  it('bounds history and does not record no-ops', () => {
    let state = { past: [] as FyDocument[], present: cloneDocument(doc), future: [] as FyDocument[] }
    expect(fyHistory(state, { type: 'edit', document: cloneDocument(doc) })).toBe(state)
    for (let i = 1; i <= 70; i++) {
      const changed = cloneDocument(state.present); changed.objects[0].layer = i
      state = fyHistory(state, { type: 'edit', document: changed })
    }
    expect(state.past).toHaveLength(60)
  })
  it('detects changed artifacts, source, narration and attempt before submit', () => {
    const shot = { shot_id: 'diagnostic', code: 'source', narration: 'text', artifact: { artifact_id: 'v1' }, attempt: 1 } as Shot
    for (const edit of [{ code: 'new' }, { narration: 'new' }, { artifact: { artifact_id: 'v2' } }, { attempt: 2 }]) {
      expect(fyBaseIdentity({ ...shot, ...edit } as Shot)).not.toBe(fyBaseIdentity(shot))
    }
    expect(fyBaseIdentity({ ...shot, status: 'GENERATING', error: 'new', updated_at: 'new' })).toBe(fyBaseIdentity(shot))
  })
})
