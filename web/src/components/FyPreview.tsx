import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Spin } from 'antd'
import { buildFyPreview, readFyGeometry, type FyGeometry } from '../fyPreview'
import { type FyDocument, type FySnapshot } from '../fyEditor'

interface Props {
  code: string; snapshot: FySnapshot; document: FyDocument; time: number; selected: string
  editable: boolean; onSelect: (id: string) => void; onMove: (id: string, dx: number, dy: number) => void
  onIssues: (issues: string[]) => void; onReady: (ready: boolean) => void
}

export function FyPreview(props: Props) {
  const { code, snapshot, document: draft, time, selected, editable, onSelect, onMove, onIssues, onReady } = props
  const frame = useRef<HTMLIFrameElement>(null), stage = useRef<HTMLDivElement>(null)
  const seq = useRef(0), lastSeq = useRef(-1)
  const [settled, setSettled] = useState(draft)
  const [state, setState] = useState<{ channel: string; ready: boolean; error?: string; geometry?: FyGeometry }>({ channel: '', ready: false })
  const [drag, setDrag] = useState<{ id: string; x: number; y: number; dx: number; dy: number } | null>(null)
  const dragRef = useRef(drag)
  const { channel, html, error } = useMemo(() => {
    const channel = crypto.randomUUID()
    try { return { channel, html: buildFyPreview(code, snapshot, settled, channel), error: '' } }
    catch (e) { return { channel, html: '', error: e instanceof Error ? e.message : '无法建立预览' } }
  }, [code, snapshot, settled])
  const ready = state.channel === channel && state.ready && !!state.geometry && !state.error && settled === draft

  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(draft), 180)
    return () => window.clearTimeout(timer)
  }, [draft])
  useEffect(() => { onReady(ready); if (!ready) onIssues([]) }, [ready, onReady, onIssues])
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.data?.channel !== channel) return
      const data = event.data
      if (data.type === 'ready') setState({ channel, ready: true })
      if (data.type === 'error') setState({ channel, ready: false, error: String(data.message).slice(0, 500) })
      if (data.type === 'frame' && Number.isInteger(data.seq) && data.seq >= lastSeq.current) {
        const geometry = readFyGeometry(data.geometry, snapshot)
        if (!geometry) return
        lastSeq.current = data.seq
        setState({ channel, ready: true, geometry })
        onIssues(Array.isArray(data.issues) ? data.issues.slice(0, 30).map((s: unknown) => String(s).slice(0, 300)) : [])
      }
    }
    window.addEventListener('message', receive)
    const timer = window.setTimeout(() => setState(s => s.channel === channel ? s : { channel, ready: false, error: '预览未响应，请重新打开对象编辑' }), 12000)
    return () => { window.removeEventListener('message', receive); window.clearTimeout(timer) }
  }, [channel, snapshot, onIssues])
  useEffect(() => {
    if (state.channel !== channel || !state.ready) return
    frame.current?.contentWindow?.postMessage({ channel, type: 'seek', time, seq: ++seq.current }, '*')
  }, [time, channel, state.channel, state.ready])

  const message = error || (state.channel === channel ? state.error : '')
  return <div className="fy-preview-wrap">
    {message && <Alert type="error" showIcon message={message} />}
    <div className="fy-preview-stage" ref={stage}>
      <iframe ref={frame} title="fy 对象草稿画面" sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={html} />
      {!ready && !message && <div className="fy-preview-loading"><Spin /></div>}
      {ready && state.geometry?.objects.filter(o => o.opacity > .15).sort((a, b) =>
        settled.objects.find(o => o.id === a.id)!.layer - settled.objects.find(o => o.id === b.id)!.layer).map(o => {
        const offset = drag?.id === o.id ? drag : { dx: 0, dy: 0 }
        const r = o.rect, item = settled.objects.find(item => item.id === o.id)!
        return <button type="button" key={o.id} className={`fy-object-bound ${selected === o.id ? 'is-selected' : ''}`}
          aria-label={`选择对象 ${item.name || o.id}`} aria-pressed={selected === o.id}
          style={{ left: `${100 * (r.left / snapshot.width + offset.dx)}%`, top: `${100 * (r.top / snapshot.height + offset.dy)}%`,
            width: `${100 * (r.right - r.left) / snapshot.width}%`, height: `${100 * (r.bottom - r.top) / snapshot.height}%` }}
          onClick={() => onSelect(o.id)}
          onKeyDown={event => {
            const moves: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }
            if (!editable || !moves[event.key]) return
            event.preventDefault(); const [x, y] = moves[event.key], step = event.shiftKey ? .02 : .005
            onSelect(o.id); onMove(o.id, x * step, y * step)
          }}
          onPointerDown={event => {
            onSelect(o.id)
            if (!editable || event.button !== 0) return
            event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId)
            const next = { id: o.id, x: event.clientX, y: event.clientY, dx: 0, dy: 0 }
            dragRef.current = next; setDrag(next)
          }}
          onPointerMove={event => {
            const current = dragRef.current, rect = stage.current?.getBoundingClientRect()
            if (!current || current.id !== o.id || !rect) return
            const next = { ...current, dx: Math.min(1 - item.box.width - item.box.x, Math.max(-item.box.x, (event.clientX - current.x) / rect.width)),
              dy: Math.min(1 - item.box.height - item.box.y, Math.max(-item.box.y, (event.clientY - current.y) / rect.height)) }
            dragRef.current = next; setDrag(next)
          }}
          onPointerUp={event => {
            const current = dragRef.current
            dragRef.current = null; setDrag(null)
            if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
            if (current && (current.dx || current.dy)) onMove(current.id, current.dx, current.dy)
          }}
          onPointerCancel={() => { dragRef.current = null; setDrag(null) }}>
          <span>{item.name || o.id}</span>
        </button>
      })}
    </div>
  </div>
}
