import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Input, Modal, Select, Space, Typography } from 'antd'
import { PictureOutlined, ReloadOutlined } from '@ant-design/icons'
import { api, errorText } from '../api'
import type { ImageProvider, VisualAsset } from '../types'

export function IllustrationSettings({ fyStyleId, role, description, subject, pose, disabled, onBusyChange, onCreated }: {
  fyStyleId?: string; role: VisualAsset['role']; description: string; subject: string; pose: string; disabled: boolean
  onBusyChange: (busy: boolean) => void; onCreated: (asset: VisualAsset) => void
}) {
  const [providers, setProviders] = useState<ImageProvider[]>([])
  const [providerId, setProviderId] = useState('')
  const [size, setSize] = useState('')
  const [prompt, setPrompt] = useState('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState(false)
  const inFlight = useRef(false)
  const provider = providers.find(p => p.id === providerId)
  const canGenerate = provider?.configured && provider.sizes.includes(size) && prompt.trim().length >= 4 &&
    (!pose.trim() || !!subject.trim()) && !disabled && !busy
  async function refresh() {
    setLoading(true); setError('')
    try {
      const result = await api.imageProviders()
      setProviders(result.providers)
      setProviderId(current => result.providers.some(p => p.id === current) ? current :
        (result.providers.find(p => p.configured)?.id ?? result.providers[0]?.id ?? ''))
    } catch (err) { setError(errorText(err)) }
    finally { setLoading(false) }
  }
  useEffect(() => { void refresh() }, [])
  useEffect(() => { setSize(current => provider?.sizes.includes(current) ? current : provider?.sizes[0] ?? '') }, [provider])
  async function generate() {
    if (!canGenerate || inFlight.current) return
    inFlight.current = true
    setConfirming(false); setBusy(true); onBusyChange(true); setError('')
    try {
      const asset = await api.generateIllustration({ provider_id: providerId, prompt: prompt.trim(), size,
        fy_style_id: fyStyleId, role, description: description.trim(), subject: subject.trim(), pose: pose.trim(), confirm_cost: true })
      onCreated(asset)
    } catch (err) { setError(errorText(err)) }
    finally { setBusy(false); onBusyChange(false); inFlight.current = false }
  }
  return <Space direction="vertical" style={{ width: '100%' }} size="middle">
    <Space wrap>
      <Select aria-label="生图服务商" value={providerId || undefined} loading={loading} disabled={disabled || busy}
        placeholder="生图服务商" style={{ width: 220, maxWidth: '100%' }} onChange={setProviderId}
        options={providers.map(p => ({ value: p.id, label: `${p.label}${p.configured ? '' : '（待配置）'}` }))} />
      <Select aria-label="配图尺寸" value={size || undefined} disabled={disabled || busy} onChange={setSize}
        style={{ minWidth: 140 }} options={provider?.sizes.map(value => ({ value, label: value }))} />
      <Button icon={<ReloadOutlined />} aria-label="刷新生图服务" title="刷新生图服务" loading={loading}
        disabled={busy} onClick={() => void refresh()} />
    </Space>
    {provider && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>{provider.model || '尚未配置模型'}{fyStyleId ? ` · 画风 ${fyStyleId}` : ''}</Typography.Text>}
    {!loading && !providers.some(p => p.configured) && <Alert type="info" showIcon
      message="尚未配置生图服务" description="请在服务端配置服务地址、准确模型 ID 和新密钥。" />}
    <Input.TextArea aria-label="配图描述" value={prompt} onChange={e => setPrompt(e.target.value)}
      rows={3} maxLength={4000} showCount disabled={disabled || busy}
      placeholder="例如：一台展开外壳的蒸汽机，正侧视角，白色背景，无文字" />
    {error && <Alert type="error" showIcon message={error} />}
    <Button icon={<PictureOutlined />} aria-label="生成配图" loading={busy} disabled={!canGenerate} onClick={() => setConfirming(true)}>生成配图</Button>
    <Modal title="确认生成配图" open={confirming} onCancel={() => setConfirming(false)}
      okText="确认生成 1 张" cancelText="取消" cancelButtonProps={{ 'aria-label': '取消生成配图' }}
      okButtonProps={{ disabled: !canGenerate }} onOk={() => void generate()}>
      <p style={{ overflowWrap: 'anywhere' }}>{provider?.label} · {provider?.model} · {size}</p>
      <p>本次调用可能产生服务商费用。失败或超时不会自动重试；生成后请先查看图片，再选择用于视频的素材。</p>
    </Modal>
  </Space>
}
