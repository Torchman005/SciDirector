import { useEffect, useState } from 'react'
import { Alert, Button, Checkbox, Form, Input, Radio, Space, Spin, Tag, Typography, Upload } from 'antd'
import { InboxOutlined, ReloadOutlined } from '@ant-design/icons'
import { api, errorText } from '../api'
import type { VisualAsset } from '../types'

const MAX_SELECTED = 6
const MAX_TOTAL = 8 * 1024 * 1024
const roleName: Record<VisualAsset['role'], string> = {
  character: '人物', prop: '道具', background: '背景',
}

export function SceneAssetSettings({ selected, onChange, onBusyChange }: {
  selected: string[]
  onChange: (ids: string[]) => void
  onBusyChange: (busy: boolean) => void
}) {
  const [items, setItems] = useState<VisualAsset[]>([])
  const [role, setRole] = useState<VisualAsset['role']>('character')
  const [description, setDescription] = useState('')
  const [subject, setSubject] = useState('')
  const [pose, setPose] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  async function refresh() {
    setLoading(true)
    try { setItems(await api.listVisuals()); setError('') }
    catch (err) { setError(errorText(err)) }
    finally { setLoading(false) }
  }
  useEffect(() => { void refresh() }, [])

  async function upload(file: File) {
    if (busy) return
    if (!['image/png', 'image/jpeg'].includes(file.type) || file.size < 1 || file.size > 2 * 1024 * 1024) {
      setError('请选择不超过 2 MB 的 PNG 或 JPEG 图片。')
      return
    }
    if (pose.trim() && !subject.trim()) {
      setError('填写姿态或状态时，请先填写同一对象名称。')
      return
    }
    setBusy(true); onBusyChange(true); setError('')
    try {
      const asset = await api.uploadVisual(file, role, description.trim(), subject.trim(), pose.trim())
      setItems(current => [...current, asset])
      setDescription('')
      setPose('')
      const current = items.filter(item => selected.includes(item.asset_id))
      if (current.length < MAX_SELECTED && current.reduce((sum, item) => sum + item.size_bytes, 0) + asset.size_bytes <= MAX_TOTAL)
        onChange([...selected, asset.asset_id])
    } catch (err) { setError(errorText(err)) }
    finally { setBusy(false); onBusyChange(false) }
  }

  function toggle(asset: VisualAsset, checked: boolean) {
    if (!checked) { onChange(selected.filter(id => id !== asset.asset_id)); return }
    const chosen = items.filter(item => selected.includes(item.asset_id))
    if (chosen.length >= MAX_SELECTED || chosen.reduce((sum, item) => sum + item.size_bytes, 0) + asset.size_bytes > MAX_TOTAL) {
      setError('一次生成最多选择 6 张图片，合计不超过 8 MB。')
      return
    }
    setError('')
    onChange([...selected, asset.asset_id])
  }

  const chosen = items.filter(item => selected.includes(item.asset_id))
  return <Form.Item label="分镜图片素材（可选）">
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Typography.Text type="secondary">
        导入人物立绘、道具或背景图，写清用途后由导演在结构化镜头中安排。透明 PNG 适合人物和道具；每次最多选 6 张、合计 8 MB。
      </Typography.Text>
      <Radio.Group value={role} disabled={busy} onChange={event => setRole(event.target.value)} aria-label="图片素材用途">
        <Radio.Button value="character">人物</Radio.Button>
        <Radio.Button value="prop">道具</Radio.Button>
        <Radio.Button value="background">背景</Radio.Button>
      </Radio.Group>
      <Input value={description} disabled={busy} onChange={event => setDescription(event.target.value)}
        maxLength={240} showCount placeholder="描述画面内容与使用场景，例如：讲解员侧身指向左侧图表" />
      <Space.Compact style={{ width: '100%' }}>
        <Input value={subject} disabled={busy} onChange={event => setSubject(event.target.value)} maxLength={48}
          aria-label="同一对象名称" placeholder="同一人物或道具名称，例如：小葵" />
        <Input value={pose} disabled={busy} onChange={event => setPose(event.target.value)} maxLength={32}
          aria-label="姿态或状态" placeholder="姿态/状态，例如：思考" />
      </Space.Compact>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>同一角色的多张立绘填相同对象名称，各自填写待机、讲解、思考等状态；图片需使用相同画布尺寸，才能在镜头中无跳位切换。</Typography.Text>
      <Upload.Dragger accept=".png,.jpg,.jpeg,image/png,image/jpeg" multiple={false} showUploadList={false} disabled={busy}
        beforeUpload={file => { void upload(file); return false }}>
        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
        <p className="ant-upload-text">{busy ? '正在校验并导入图片…' : '点击或拖拽 PNG / JPEG 图片'}</p>
        <p className="ant-upload-hint">单张不超过 2 MB，最长边不超过 4096 像素</p>
      </Upload.Dragger>
      {error && <Alert type="error" showIcon role="alert" message={error} action={<Button size="small" icon={<ReloadOutlined />} onClick={() => void refresh()}>刷新素材库</Button>} />}
      <Space><Typography.Text strong>本次选用 {chosen.length}/{MAX_SELECTED} 张</Typography.Text>
        <Typography.Text type="secondary">{(chosen.reduce((sum, item) => sum + item.size_bytes, 0) / 1024 / 1024).toFixed(2)} / 8 MB</Typography.Text>
        <Button size="small" loading={loading} onClick={() => void refresh()}>刷新</Button></Space>
      {loading ? <Spin /> : items.length === 0 ? <Typography.Text type="secondary">素材库暂无图片。</Typography.Text> :
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(190px,1fr))', gap: 12 }}>
          {items.map(item => <label key={item.asset_id} style={{ display: 'block', padding: 10, border: selected.includes(item.asset_id) ? '1px solid #6f9ff8' : '1px solid #394865', borderRadius: 8, cursor: 'pointer' }}>
            <img src={api.visualUrl(item.asset_id)} alt={item.filename} loading="lazy"
              style={{ width: '100%', height: 110, objectFit: 'contain', background: 'repeating-conic-gradient(#263149 0% 25%,#1d283d 0% 50%) 50% / 16px 16px', borderRadius: 4 }} />
            <div style={{ marginTop: 7 }}><Checkbox checked={selected.includes(item.asset_id)} disabled={busy}
              onChange={event => toggle(item, event.target.checked)}>{item.filename}</Checkbox></div>
            <Space size={5} wrap><Tag>{roleName[item.role]}</Tag>{item.subject && <Tag color="blue">{item.subject} · {item.pose || '默认'}</Tag>}<Typography.Text type="secondary" style={{ fontSize: 12 }}>{item.width} × {item.height}</Typography.Text></Space>
            {item.description && <Typography.Paragraph ellipsis={{ rows: 2 }} style={{ marginBottom: 0, fontSize: 12 }}>{item.description}</Typography.Paragraph>}
          </label>)}
        </div>}
    </Space>
  </Form.Item>
}
