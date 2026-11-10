import { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import {
  Alert, App, Button, Empty, Input, InputNumber, Modal, Popconfirm, Segmented,
  Select, Slider, Space, Switch, Table, Tag, Tooltip, Typography,
} from 'antd'
import {
  DeleteOutlined, EditOutlined, PauseCircleOutlined, PlayCircleOutlined, PlusOutlined,
  RedoOutlined, ReloadOutlined, UndoOutlined,
} from '@ant-design/icons'
import { api, errorText } from '../api'
import {
  buildFyRevision, cloneDocument, FY_COLORS, FY_COMMENT_LIMIT, FY_PORTS, fyBaseIdentity,
  fyChanges, fyHistory, moveFyBox, type FyBox, type FyDocument, type FyObject, type FyRelation,
} from '../fyEditor'
import { readFySnapshot } from '../fyPreview'
import type { Shot } from '../types'
import { FyPreview } from './FyPreview'

const { Text } = Typography
const PORT_LABELS = { auto: '自动', left: '左', right: '右', top: '上', bottom: '下' }
const COLOR_LABELS = { ink: '主色', mute: '次色', line: '线色', q: 'Q', k: 'K', v: 'V', gold: '金色', blue: '蓝色' }
const canEdit = (s: Shot) => ['APPROVED', 'AWAITING_HUMAN', 'FAILED', 'REJECTED'].includes(s.status)

interface Props {
  jobId: string; shot: Shot; busy: boolean; onChanged: () => void
  onSubmit: (comment: string) => Promise<boolean>
}

export function FyObjectEditor(props: Props) {
  const [base, setBase] = useState<Shot | null>(null)
  const unsupported = useMemo(() => {
    try { readFySnapshot(props.shot.code || ''); return '' }
    catch (e) { return errorText(e) }
  }, [props.shot.code])
  return <>
    <Tooltip title={unsupported || undefined}>
      <span><Button aria-label="对象编辑" icon={<EditOutlined />} disabled={!!unsupported || props.busy || !canEdit(props.shot)}
        onClick={() => setBase(JSON.parse(JSON.stringify(props.shot)))}>对象编辑</Button></span>
    </Tooltip>
    {base && <FyEditorSession {...props} base={base} onClose={() => setBase(null)} />}
  </>
}

function FyEditorSession({ base, shot, jobId, busy, onSubmit, onChanged, onClose }: Props & { base: Shot; onClose: () => void }) {
  const { modal } = App.useApp()
  const [snapshot] = useState(() => readFySnapshot(base.code!))
  const [history, dispatch] = useReducer(fyHistory, { past: [], present: cloneDocument(snapshot.document), future: [] })
  const [kind, setKind] = useState('objects'), [selectedObject, setSelectedObject] = useState(snapshot.document.objects[0].id)
  const [selectedRelation, setSelectedRelation] = useState(snapshot.document.relations[0]?.id || '')
  const [comparison, setComparison] = useState('draft'), [time, setTime] = useState(0), [playing, setPlaying] = useState(false)
  const [issues, setIssues] = useState<string[]>([]), [previewReady, setPreviewReady] = useState(false)
  const [saving, setSaving] = useState(false), [submitError, setSubmitError] = useState(''), [serverStale, setServerStale] = useState(false)
  const pending = useRef(false)
  const draft = history.present, revision = buildFyRevision(snapshot, draft), changes = fyChanges(snapshot.document, draft)
  const stale = serverStale || fyBaseIdentity(shot) !== fyBaseIdentity(base)
  const locked = busy || saving || stale || !canEdit(shot)
  const editable = !locked && comparison === 'draft'
  const selected = kind === 'objects' ? selectedObject : ''
  const object = draft.objects.find(o => o.id === selectedObject)!
  const relation = draft.relations.find(r => r.id === selectedRelation)
  const tooLarge = revision.length > FY_COMMENT_LIMIT
  const submitDisabled = locked || !revision.changed || tooLarge || comparison !== 'draft' || !previewReady || issues.length > 0

  useEffect(() => {
    if (!playing) return
    const start = performance.now(), position = time >= snapshot.duration ? 0 : time
    let frame = 0
    const tick = () => {
      const next = Math.min(snapshot.duration, position + (performance.now() - start) / 1000)
      setTime(next)
      if (next >= snapshot.duration) setPlaying(false)
      else frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
    // Playback owns the clock until paused; seeking pauses it before changing time.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, snapshot.duration])

  function commit(document: FyDocument) {
    if (!editable) return
    setPlaying(false); setSubmitError(''); dispatch({ type: 'edit', document })
  }
  function updateObject(update: Partial<FyObject>) {
    if (update.layer !== undefined) update.layer = Math.min(100, Math.max(-100, Math.round(update.layer)))
    commit({ ...draft, objects: draft.objects.map(o => o.id === object.id ? { ...o, ...update } : o) })
  }
  function updateBox(key: keyof FyBox, value: number | null) {
    if (value === null) return
    const b = object.box
    const max = key === 'x' ? 1 - b.width : key === 'y' ? 1 - b.height : key === 'width' ? 1 - b.x : 1 - b.y
    const normalized = Math.min(max, Math.max(key === 'x' || key === 'y' ? 0 : .00001, +(value / 100).toFixed(5)))
    updateObject({ box: { ...b, [key]: normalized } })
  }
  function updateRelation(update: Partial<FyRelation>) {
    if (update.stroke_width !== undefined) update.stroke_width = Math.min(12, Math.max(1, update.stroke_width))
    if (relation) commit({ ...draft, relations: draft.relations.map(r => r.id === relation.id ? { ...r, ...update } : r) })
  }
  function close() {
    if (saving || busy) return
    setPlaying(false)
    if (!revision.changed) { onClose(); return }
    modal.confirm({ title: '放弃本地调整？', content: '尚未提交的对象和连线修改将被丢弃，服务器上的镜头不变。',
      okText: '放弃调整', cancelText: '继续编辑', onOk: onClose })
  }
  function locateIssue(issue: string) {
    const match = issue.match(/\[(对象|关系) ([A-Za-z][A-Za-z0-9_-]*)\]/)
    if (!match) return
    setPlaying(false)
    if (match[1] === '对象' && draft.objects.some(o => o.id === match[2])) {
      setKind('objects'); setSelectedObject(match[2])
    } else if (match[1] === '关系' && draft.relations.some(r => r.id === match[2])) {
      setKind('relations'); setSelectedRelation(match[2])
    }
  }
  async function submit() {
    if (pending.current || submitDisabled) return
    pending.current = true; setSaving(true); setPlaying(false); setSubmitError('')
    try {
      const response = await api.getJob(jobId), latest = response.job.shots?.find(s => s.shot_id === base.shot_id)
      if (!latest || fyBaseIdentity(latest) !== fyBaseIdentity(base)) {
        setServerStale(true); onChanged(); throw new Error('镜头版本已更新，本地调整未提交。请关闭并刷新后重新编辑。')
      }
      if (!canEdit(latest)) throw new Error('镜头正在处理，请等待完成后再提交。')
      if (await onSubmit(revision.comment)) onClose()
    } catch (e) { setSubmitError(errorText(e)) }
    finally { pending.current = false; setSaving(false) }
  }

  const objectOptions = draft.objects.map(o => ({ value: o.id, label: o.name || o.id }))
  const portOptions = FY_PORTS.map(p => ({ value: p, label: PORT_LABELS[p] }))
  return <Modal open title={<Space wrap><span>镜头对象工作台</span><Tag>本地草稿</Tag><Text type="secondary" className="fy-version">{snapshot.styleId || 'fy'} · {snapshot.revision.slice(0, 10)}</Text></Space>}
    width={1360} className="fy-editor-modal" onCancel={close} maskClosable={false} closable={!saving && !busy}
    footer={<div className="fy-editor-footer">
      <Text type={tooLarge ? 'danger' : 'secondary'}>{revision.changed ? `${revision.changed} 项修改 · ${revision.length}/${FY_COMMENT_LIMIT}` : '尚无修改'}</Text>
      <Space wrap><Button onClick={close} disabled={saving || busy}>取消</Button>
        <Tooltip title={tooLarge ? '本次修改超过接口上限，请撤销部分修改后分次提交' : comparison === 'original' ? '当前正在查看修改前版本' : undefined}>
          <Button type="primary" loading={saving || busy} disabled={submitDisabled} onClick={() => void submit()}>提交修改并复审</Button>
        </Tooltip>
      </Space>
    </div>}>
    <div className="fy-editor">
      {stale && <Alert type="warning" showIcon message="镜头版本已更新，本地草稿不会覆盖新版本。"
        action={<Button size="small" icon={<ReloadOutlined />} onClick={() => { onChanged(); close() }}>关闭并刷新</Button>} />}
      {!canEdit(shot) && <Alert type="info" showIcon message="镜头正在处理，暂不能提交修改。" />}
      {submitError && <Alert type="error" showIcon message={submitError} />}
      {tooLarge && <Alert type="warning" showIcon message="修改内容超过单次上限，请减少本次修改；不会截断或自动拆分提交。" />}
      <div className="fy-editor-layout">
        <aside className="fy-editor-list">
          <Segmented block value={kind} onChange={value => setKind(String(value))} options={[{ value: 'objects', label: `对象 ${draft.objects.length}` }, { value: 'relations', label: `连线 ${draft.relations.length}` }]} />
          <div className="fy-item-list">
            {kind === 'objects' ? draft.objects.map(o => <button key={o.id} type="button" className={`fy-item ${selectedObject === o.id ? 'is-selected' : ''}`}
              aria-pressed={selectedObject === o.id} onClick={() => { setSelectedObject(o.id); setPlaying(false) }}>
              <span>{o.name || o.id}</span><small>{o.id} · 层 {o.layer}</small>
            </button>) : draft.relations.map(r => <button key={r.id} type="button" className={`fy-item ${selectedRelation === r.id ? 'is-selected' : ''}`}
              aria-pressed={selectedRelation === r.id} onClick={() => { setSelectedRelation(r.id); setPlaying(false) }}>
              <span>{r.id}</span><small>{r.source} → {r.target}</small>
            </button>)}
            {kind === 'relations' && !draft.relations.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无连线" />}
          </div>
          {kind === 'relations' && <Button block icon={<PlusOutlined />} disabled={!editable || draft.objects.length < 2 || draft.relations.length >= 48}
            onClick={() => {
              let i = 1
              const ids = new Set([...draft.objects, ...draft.relations].map(o => o.id))
              while (ids.has(`link-${i}`)) i++
              const r: FyRelation = { id: `link-${i}`, source: selectedObject, target: draft.objects.find(o => o.id !== selectedObject)!.id,
                source_port: 'auto', target_port: 'auto', color: 'ink', stroke_width: 3, arrow: true, flow: false }
              commit({ ...draft, relations: [...draft.relations, r] }); setSelectedRelation(r.id)
            }}>添加连线</Button>}
        </aside>
        <section className="fy-editor-canvas" aria-label="镜头预览与修改对照">
          <div className="fy-preview-toolbar">
            <Segmented value={comparison} options={[{ value: 'original', label: '修改前' }, { value: 'draft', label: '调整后' }]}
              onChange={value => { setPlaying(false); setComparison(String(value)) }} />
            <Space size={4}>
              <Tooltip title="撤销"><Button aria-label="撤销" icon={<UndoOutlined />} disabled={!editable || !history.past.length}
                onClick={() => { setPlaying(false); dispatch({ type: 'undo' }) }} /></Tooltip>
              <Tooltip title="重做"><Button aria-label="重做" icon={<RedoOutlined />} disabled={!editable || !history.future.length}
                onClick={() => { setPlaying(false); dispatch({ type: 'redo' }) }} /></Tooltip>
            </Space>
          </div>
          <FyPreview code={base.code!} snapshot={snapshot} document={comparison === 'original' ? snapshot.document : draft}
            time={time} selected={selected} editable={editable && !playing}
            onSelect={id => { setKind('objects'); setSelectedObject(id); setPlaying(false) }}
            onMove={(id, dx, dy) => commit({ ...draft, objects: draft.objects.map(o => o.id === id ? { ...o, box: moveFyBox(o.box, dx, dy) } : o) })}
            onIssues={setIssues} onReady={setPreviewReady} />
          <div className="fy-playback">
            <Tooltip title={playing ? '暂停' : '播放'}><Button aria-label={playing ? '暂停' : '播放'}
              icon={playing ? <PauseCircleOutlined /> : <PlayCircleOutlined />} disabled={!previewReady} onClick={() => setPlaying(!playing)} /></Tooltip>
            <Slider aria-label="预览时间" min={0} max={snapshot.duration} step={1 / snapshot.fps} value={time}
              onChange={value => { setPlaying(false); setTime(value) }} tooltip={{ formatter: value => `${value?.toFixed(2)} 秒` }} />
            <output>{time.toFixed(2)} / {snapshot.duration.toFixed(2)}s</output>
          </div>
          {!!issues.length && <Alert type="warning" showIcon message={`当前时刻${comparison === 'original' ? '原版' : '草稿'}：${issues.length} 项布局问题`}
            description={<ul className="fy-issues">{issues.slice(0, 4).map((issue, i) => <li key={i}>
              {/\[(对象|关系) /.test(issue) ? <button type="button" className="fy-issue-link" onClick={() => locateIssue(issue)}>{issue}</button> : issue}
            </li>)}</ul>} />}
          <div className="fy-diff">
            <Text strong>修改对照</Text>
            <Table size="small" pagination={false} rowKey={r => `${r.id}:${r.field}`} dataSource={changes}
              locale={{ emptyText: '暂无修改' }} scroll={{ x: 430, y: 190 }}
              columns={[{ title: '对象 / 连线', dataIndex: 'id', width: 120 }, { title: '项目', dataIndex: 'field', width: 90 },
                { title: '修改前', dataIndex: 'before', width: 110 }, { title: '调整后', dataIndex: 'after', width: 110 }]} />
          </div>
        </section>
        <aside className="fy-editor-properties">
          <Text strong>{kind === 'objects' ? object.name || object.id : relation?.id || '连线属性'}</Text>
          {kind === 'objects' ? <>
            <Text type="secondary" className="fy-id">{object.id}</Text>
            <div className="fy-numeric-grid">
              {(['x', 'y', 'width', 'height'] as const).map(key => <label key={key}>
                <span>{{ x: '横向位置 %', y: '纵向位置 %', width: '宽度 %', height: '高度 %' }[key]}</span>
                <InputNumber aria-label={{ x: '横向位置', y: '纵向位置', width: '宽度', height: '高度' }[key]} size="small" disabled={!editable}
                  value={+(object.box[key] * 100).toFixed(3)} min={key === 'x' || key === 'y' ? 0 : .1}
                  max={+(100 * (key === 'x' ? 1 - object.box.width : key === 'y' ? 1 - object.box.height : key === 'width' ? 1 - object.box.x : 1 - object.box.y)).toFixed(3)}
                  step={.5} precision={3} onChange={value => updateBox(key, value)} />
              </label>)}
            </div>
            <label className="fy-field"><span>层级</span><InputNumber aria-label="层级" disabled={!editable} min={-100} max={100} precision={0}
              value={object.layer} onChange={value => value !== null && updateObject({ layer: value })} /></label>
            <label className="fy-field"><span>有意叠放组</span><Input aria-label="有意叠放组" disabled={!editable} maxLength={64}
              value={object.overlap_group} onChange={event => updateObject({ overlap_group: event.target.value })} /></label>
            <Text type="secondary">运动关键帧：{object.keyframes.length}</Text>
          </> : relation ? <>
            <label className="fy-field"><span>起点对象</span><Select aria-label="起点对象" value={relation.source} disabled={!editable}
              options={objectOptions.map(o => ({ ...o, disabled: o.value === relation.target }))} onChange={source => updateRelation({ source })} /></label>
            <label className="fy-field"><span>起点端口</span><Select aria-label="起点端口" value={relation.source_port} disabled={!editable}
              options={portOptions} onChange={source_port => updateRelation({ source_port })} /></label>
            <label className="fy-field"><span>终点对象</span><Select aria-label="终点对象" value={relation.target} disabled={!editable}
              options={objectOptions.map(o => ({ ...o, disabled: o.value === relation.source }))} onChange={target => updateRelation({ target })} /></label>
            <label className="fy-field"><span>终点端口</span><Select aria-label="终点端口" value={relation.target_port} disabled={!editable}
              options={portOptions} onChange={target_port => updateRelation({ target_port })} /></label>
            <label className="fy-field"><span>颜色</span><Select aria-label="连线颜色" value={relation.color} disabled={!editable}
              options={FY_COLORS.map(color => ({ value: color, label: <Space><span className="fy-swatch" style={{ backgroundColor: snapshot.palette[color] }} />{COLOR_LABELS[color]}</Space> }))}
              onChange={color => updateRelation({ color })} /></label>
            <label className="fy-field"><span>线宽</span><InputNumber aria-label="线宽" min={1} max={12} value={relation.stroke_width} disabled={!editable}
              onChange={stroke_width => stroke_width !== null && updateRelation({ stroke_width })} /></label>
            <div className="fy-toggle"><span>箭头</span><Switch aria-label="箭头" checked={relation.arrow} disabled={!editable} onChange={arrow => updateRelation({ arrow })} /></div>
            <div className="fy-toggle"><span>流动</span><Switch aria-label="流动" checked={relation.flow} disabled={!editable} onChange={flow => updateRelation({ flow })} /></div>
            <Popconfirm title="删除这条连线？" okText="删除" cancelText="取消" disabled={!editable}
              onConfirm={() => { commit({ ...draft, relations: draft.relations.filter(r => r.id !== relation.id) }); setSelectedRelation(draft.relations.find(r => r.id !== relation.id)?.id || '') }}>
              <Button danger icon={<DeleteOutlined />} disabled={!editable}>删除连线</Button>
            </Popconfirm>
          </> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未选择连线" />}
        </aside>
      </div>
    </div>
  </Modal>
}
