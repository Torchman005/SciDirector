import { useState } from 'react'
import { Alert, Button, Form, Input, Slider, Space, Typography, Upload } from 'antd'
import { InboxOutlined } from '@ant-design/icons'
import { api, errorText } from '../api'
import type { Effects } from '../types'

export function PresenterSettings({value, onChange, onBusyChange}: {
  value: Effects['presenter']
  onChange: (value: Effects['presenter']) => void
  onBusyChange: (value: boolean) => void
}) {
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  async function upload(file: File) {
    if (busy) return
    if (!file.name.toLowerCase().endsWith('.zip') || file.size > 100 * 1024 * 1024) {
      setError('请选择不超过 100 MB 的模型 ZIP。'); return
    }
    setBusy(true); onBusyChange(true); setError('')
    try {
      const asset = await api.uploadLive2D(file)
      setName(asset.filename)
      onChange({asset_id: asset.asset_id, mouth_gain: 1})
    } catch (err) { setError(errorText(err)) }
    finally { setBusy(false); onBusyChange(false) }
  }
  return <Form.Item label="Live2D 讲解员（可选）">
    <Space direction="vertical" style={{width:'100%'}} size="middle">
      <Typography.Text type="secondary">导入 Cubism 3/4 模型 ZIP，包含一个 .model3.json、.moc3 与纹理。成片右下角显示角色，口型跟随 TTS；主图保留完整，字幕置于最上层。</Typography.Text>
      {value && <Alert type="success" showIcon message={`已导入：${name || 'Live2D 模型'}`} />}
      <Upload.Dragger accept=".zip" multiple={false} disabled={busy} showUploadList={false}
        beforeUpload={file => { void upload(file); return false }}>
        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
        <p className="ant-upload-text">{busy ? '正在校验模型…' : value ? '替换 Live2D 模型' : '点击或拖拽模型 ZIP'}</p>
        <p className="ant-upload-hint">最大 100 MB · 不包含 SDK 或脚本 · 需服务端启用 Cubism Core 与 TTS</p>
      </Upload.Dragger>
      {error && <Alert type="error" showIcon message={error} role="alert" />}
      {value && <>
        <label htmlFor="mouth-parameter">口型参数（留空读取模型 LipSync 分组）</label>
        <Input id="mouth-parameter" placeholder="例如 ParamMouthOpenY" value={value.mouth_parameter || ''}
          maxLength={64} onChange={event => onChange({...value,mouth_parameter:event.target.value})} />
        <label id="mouth-gain-label">口型强度：{value.mouth_gain ?? 1}</label>
        <Slider aria-labelledby="mouth-gain-label" min={0.2} max={3} step={0.1} value={value.mouth_gain || 1}
          onChange={mouth_gain => onChange({...value,mouth_gain})} />
        <Button onClick={() => {onChange(undefined); setError(''); setName('')}} disabled={busy}>不使用讲解员</Button>
      </>}
    </Space>
  </Form.Item>
}
