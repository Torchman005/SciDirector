import bridge from './fyPreviewBridge.js?raw'
import { fySnapshotFromData, type FyDocument, type FySnapshot } from './fyEditor'

function parseSource(html: string): Document {
  if (!html || html.length > 2_000_000) throw new Error('镜头源码缺失或过大，暂不能预览')
  return new DOMParser().parseFromString(html, 'text/html')
}
function marker(doc: Document, id: string): unknown {
  const nodes = doc.querySelectorAll(`script#${id}[type="application/json"]`)
  if (nodes.length !== 1) throw new Error('该镜头缺少唯一的 fy 对象快照，请使用新版 fy 生成镜头')
  return JSON.parse(nodes[0].textContent || 'null')
}

export function readFySnapshot(html: string): FySnapshot {
  const doc = parseSource(html), snapshot = fySnapshotFromData(marker(doc, 'scid-fy-data'))
  if (typeof marker(doc, 'scid-fy-code') !== 'string') throw new Error('镜头画笔源码缺失')
  return snapshot
}

export function buildFyPreview(html: string, snapshot: FySnapshot, draft: FyDocument, channel: string): string {
  const source = parseSource(html), payload = marker(source, 'scid-fy-data') as Record<string, unknown>
  const doc = document.implementation.createHTMLDocument('对象草稿预览')
  const meta = doc.createElement('meta')
  meta.httpEquiv = 'Content-Security-Policy'
  meta.content = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; media-src 'none'; font-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'"
  doc.head.prepend(meta)
  for (const style of source.querySelectorAll('style')) doc.head.append(doc.importNode(style, true))
  const style = doc.createElement('style')
  style.textContent = 'html,body{margin:0!important;width:100%!important;height:100%!important;overflow:hidden!important}#film{transform-origin:0 0}'
  doc.head.append(style)
  const canvas = doc.createElement('canvas')
  canvas.id = 'film'
  doc.body.append(canvas)
  for (const script of source.querySelectorAll('script')) {
    if (script.hasAttribute('src') || script.type === 'module') throw new Error('该镜头含外部脚本，不能在对象预览中运行')
    const copy = doc.createElement('script')
    if (script.type) copy.type = script.type
    if (script.id) copy.id = script.id
    // This is a local draft, not a new server revision. The original version is retained only as its base.
    copy.textContent = script.id === 'scid-fy-data'
      ? JSON.stringify({ ...payload, object_document: draft }).replace(/</g, '\\u003c') : script.textContent
    doc.body.append(copy)
  }
  const config = doc.createElement('script')
  config.id = 'scid-editor-preview'; config.type = 'application/json'
  config.textContent = JSON.stringify({ channel, width: snapshot.width, height: snapshot.height, duration: snapshot.duration })
  doc.body.append(config)
  const bridgeScript = doc.createElement('script')
  bridgeScript.textContent = bridge
  doc.body.append(bridgeScript)
  return '<!doctype html>' + doc.documentElement.outerHTML
}

export interface FyGeometry {
  objects: { id: string; opacity: number; rect: { left: number; top: number; right: number; bottom: number } }[]
  relations: { id: string; status: string }[]
}
export function readFyGeometry(value: unknown, snapshot: FySnapshot): FyGeometry | null {
  if (!value || typeof value !== 'object') return null
  const v = value as FyGeometry
  if (!Array.isArray(v.objects) || v.objects.length !== snapshot.document.objects.length || !Array.isArray(v.relations) || v.relations.length > 48) return null
  const ids = new Set(snapshot.document.objects.map(o => o.id))
  for (const o of v.objects) {
    if (!ids.delete(o.id) || !Number.isFinite(o.opacity) || !o.rect ||
      ![o.rect.left, o.rect.top, o.rect.right, o.rect.bottom].every(n => Number.isFinite(n) && Math.abs(n) <= 100000)) return null
  }
  return v
}
