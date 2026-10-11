import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Form, Image, Input, Radio, Slider, Space, Typography, Upload } from 'antd'
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
  const [format, setFormat] = useState<'live2d'|'portrait'>('live2d')
  const [importedKind, setImportedKind] = useState<'live2d'|'portrait'>('live2d')
  const [lipSync, setLipSync] = useState(false)
  const pending = useRef<AbortController | null>(null)
  useEffect(() => () => { pending.current?.abort() }, [])
  async function upload(file: File) {
    if (pending.current) return
    if (!file.name.toLowerCase().endsWith('.zip') || !file.size || file.size > 100 * 1024 * 1024) {
      setError('请选择不超过 100 MB 的模型 ZIP。'); return
    }
    setBusy(true); onBusyChange(true); setError('')
    const controller = new AbortController(); pending.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 120_000)
    try {
      const asset = await (format === 'portrait' ? api.uploadPortrait : api.uploadLive2D)(file,controller.signal)
      setName(asset.filename)
      setImportedKind(format); setLipSync(Boolean(asset.lip_sync))
      onChange({asset_id: asset.asset_id, mouth_gain: 1})
    } catch (err) { setError(controller.signal.aborted ? '导入已取消或超时，原素材已保留，可重新上传。' : errorText(err)) }
    finally { window.clearTimeout(timeout); pending.current = null; setBusy(false); onBusyChange(false) }
  }
  return <Form.Item label="画面讲解员（可选）">
    <Space direction="vertical" style={{width:'100%'}} size="middle">
      <Radio.Group aria-label="讲解员素材格式" value={format} disabled={busy} onChange={event => setFormat(event.target.value)}>
        <Radio.Button value="live2d">Live2D 模型</Radio.Button><Radio.Button value="portrait">多状态立绘</Radio.Button>
      </Radio.Group>
      <Typography.Text type="secondary">{format === 'portrait'
        ? '导入含 portrait.json 和待机、讲解、思考、惊讶四种状态 PNG 的 ZIP。图片须为同尺寸透明立绘；可附加四张张嘴图片随 TTS 切换口型。无需 Cubism SDK。'
        : '导入 Cubism 3/4 模型 ZIP，包含一个 .model3.json、.moc3 与纹理。成片右下角显示角色，口型跟随 TTS；主图保留完整，字幕置于最上层。'}</Typography.Text>
      {value && <Alert type="success" showIcon message={`已导入：${name || 'Live2D 模型'}`} />}
      <Upload.Dragger accept=".zip" multiple={false} disabled={busy} showUploadList={false}
        beforeUpload={file => { void upload(file); return false }}>
        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
        <p className="ant-upload-text">{busy ? '正在校验模型…' : format === 'portrait' ? '点击或拖拽立绘 ZIP' : value ? '替换 Live2D 模型' : '点击或拖拽模型 ZIP'}</p>
        <p className="ant-upload-hint">最大 100 MB · 不包含 SDK 或脚本 · {format === 'portrait' ? '需启用 TTS；状态时间表可写入素材包' : '需服务端启用 Cubism Core 与 TTS'}</p>
      </Upload.Dragger>
      {busy && <Button onClick={() => pending.current?.abort()}>取消导入</Button>}
      {error && <Alert type="error" showIcon message={error} role="alert" />}
      {value && importedKind === 'portrait' && <>
        <Alert type={lipSync ? 'info' : 'warning'} showIcon message={lipSync ? '已启用语音口型；思考和惊讶按素材包时间表切换。' : '此包未提供张嘴图片：可切换状态，暂不产生口型动画。'} />
        <div style={{display:'grid',gridTemplateColumns:'repeat(2,minmax(0,1fr))',gap:16}}>
          {([['neutral','待机'],['explain','讲解'],['think','思考'],['surprised','惊讶']] as const).map(([state,label]) =>
            <div key={state}><Image preview={false} src={`/api/v1/assets/portraits/${value.asset_id}/${state}`} alt={`${label}立绘预览`}
              height={144} style={{objectFit:'contain',maxWidth:'100%'}} onError={() => setError('立绘预览加载失败，请检查素材服务后重试。')} /><div>{label}</div></div>)}
        </div>
      </>}
      {value && <>
        {importedKind === 'live2d' && <>
        <label htmlFor="mouth-parameter">口型参数（留空读取模型 LipSync 分组）</label>
        <Input id="mouth-parameter" placeholder="例如 ParamMouthOpenY" value={value.mouth_parameter || ''}
          maxLength={64} disabled={busy} onChange={event => onChange({...value,mouth_parameter:event.target.value})} />
        </>}
        <label id="mouth-gain-label">口型强度：{value.mouth_gain ?? 1}</label>
        <Slider aria-labelledby="mouth-gain-label" disabled={busy || (importedKind === 'portrait' && !lipSync)} min={0.2} max={3} step={0.1} value={value.mouth_gain || 1}
          onChange={mouth_gain => onChange({...value,mouth_gain})} />
        <Button onClick={() => {onChange(undefined); setError(''); setName('')}} disabled={busy}>不使用讲解员</Button>
      </>}
    </Space>
  </Form.Item>
}
