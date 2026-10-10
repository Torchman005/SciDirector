import { useEffect, useState } from 'react'
import {
  Alert,
  App as AntApp,
  Badge,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Divider,
  Form,
  Input,
  InputNumber,
  Layout,
  Progress,
  Row,
  Select,
  Slider,
  Space,
  Statistic,
  Switch,
  Tag,
  Typography,
  Upload,
} from 'antd'
import { InboxOutlined, ReloadOutlined } from '@ant-design/icons'

import { api, errorText } from './api'
import { EventTimeline } from './components/EventTimeline'
import { ShotTable } from './components/ShotTable'
import { PresenterSettings } from './components/PresenterSettings'
import { deriveReviewSummary, deriveStat, shotsOf } from './stream'
import { useJobStream } from './useJobStream'
import { BACKGROUND_STYLES, STYLE_PRESETS, formatTime, jobStatus } from './display'
import type { ConnectionState, Effects, GenerateRequest } from './types'

/**
 * SciDirector 分镜审核台。
 *
 * 页面组织遵循「先看结论，再看细节，最后看过程」：
 *   ① 任务头（状态 + 进度 + 计数）—— 一眼知道整体怎么样；
 *   ② 待人工处理提示 —— 需要我做什么；
 *   ③ 分镜表 —— 逐个确认与操作；
 *   ④ 事件时间线 —— 出问题时再往下看。
 *
 * 连接状态始终可见（包括重连中）。断网时如果界面没有任何提示，
 * 用户只会以为「系统卡住了」，然后刷新页面 —— 而实际上它正在自动恢复。
 */

const { Header, Content } = Layout
const { Title, Text, Paragraph } = Typography

const CONNECTION: Record<
  ConnectionState,
  { label: string; status: 'processing' | 'success' | 'warning' | 'error' }
> = {
  connecting: { label: '连接中…', status: 'processing' },
  open: { label: '实时', status: 'success' },
  reconnecting: { label: '重连中…', status: 'warning' },
  closed: { label: '已断开', status: 'error' },
}

const GRADES = [
  { value: 'none', label: '不调色' },
  { value: 'warm', label: '暖' },
  { value: 'cool', label: '冷' },
  { value: 'high_contrast', label: '高对比' },
  { value: 'film', label: '胶片感' },
]

interface SubmitForm {
  animation_style: 'precise' | 'anime'
  raw_script: string
  /** null = 留空 = 按脚本自动估算（提交时转成 0）。 */
  target_duration_sec: number | null
  preset: string
  background_style: string
  grade: string
  grade_strength: number
  fade_in_sec: number
  fade_out_sec: number
  burn_subtitles: boolean
  subtitle_font_size: number
  subtitle_color: string
  subtitle_margin_v: number
}

/** 从 URL 读任务号，让审核页可以分享 / 刷新不丢。 */
function jobFromUrl(): string | null {
  const v = new URLSearchParams(window.location.search).get('job')
  return v && v.trim() ? v.trim() : null
}

export function App() {
  const { message } = AntApp.useApp()
  const [form] = Form.useForm<SubmitForm>()
  const [jobId, setJobId] = useState<string | null>(jobFromUrl)
  const [submitting, setSubmitting] = useState(false)
  const [presenter, setPresenter] = useState<Effects['presenter']>()
  const [presenterBusy, setPresenterBusy] = useState(false)
  const [refreshTick, setRefreshTick] = useState(0)
  const [estimate, setEstimate] = useState<number | null>(null)
  const [estimateBasis, setEstimateBasis] = useState('')
  // 提交后服务端实际采用的时长与来源（留空时才知道它选了多久）。
  const [resolved, setResolved] = useState<{ sec: number; source: string } | null>(null)

  // BGM：上传后记住 asset_id。**只传 id，不传路径** ——
  // 路径由服务端从素材库解析，请求方拿不到"读服务端任意文件"的能力。
  //
  // meanDb/peakDb 是服务端**实测**的电平：音乐文件响度差异极大（常见 -3 到 -25
  // dBFS），而音量滑块是**相对基准**的偏移 —— 不知道文件本身多响，滑块该往哪边
  // 拖就只能靠猜。这正是"上传以后无法判断音量"的症结。
  const [bgm, setBgm] = useState<{
    assetId: string
    filename: string
    durationSec: number
    meanDb: number | null
    peakDb: number | null
    peakWarning: boolean
  } | null>(null)
  const [bgmBusy, setBgmBusy] = useState(false)
  // 配乐音量偏移（dB）。0 = 基准响度（服务端把配乐归一到 -20 LUFS）。
  // 缺省 0 而不是负数：基准本身已经比对白低一截，"再减一点"是多余的。
  const [bgmVolumeDb, setBgmVolumeDb] = useState(0)

  // 表单联动：色调决定强度滑块是否可用，烧录开关决定字幕样式是否可用。
  // 用 useWatch 而不是自己再存一份 state —— 两份真相迟早会不一致。
  const script = Form.useWatch('raw_script', form) ?? ''
  const grade = Form.useWatch('grade', form) ?? 'none'
  const burnSubtitles = Form.useWatch('burn_subtitles', form) ?? false
  const duration = Form.useWatch('target_duration_sec', form)
  // 子标题颜色也在这里 watch：**不能**写在 JSX 里，
  // 因为那处 JSX 属于 `{!jobId && …}` 分支 —— 有任务时它不会执行，
  // hooks 数量随渲染变化，React 会直接抛错。
  const subtitleColor = Form.useWatch('subtitle_color', form) ?? '#ffffff'

  const { state, connection, lastError, reconnect, mergeDetail } = useJobStream({
    jobId,
    onNeedJobRefresh: () => setRefreshTick((n) => n + 1),
  })

  // 任务号写回 URL：刷新页面不会丢掉正在看的那条任务，
  // 也让它变成一条可以直接发给别人的链接。
  useEffect(() => {
    const url = new URL(window.location.href)
    if (jobId) url.searchParams.set('job', jobId)
    else url.searchParams.delete('job')
    window.history.replaceState(null, '', url)
  }, [jobId])

  // 回源任务明细。
  //
  // 触发时机有三类：
  //   - 实时事件到达时（事件里不含分镜明细，不回源就看不到推进）；
  //   - 人工操作之后（同上）；
  //   - WS 断线重连之后（断线期间分镜表可能已经变了）。
  // 用 tick 而不是把 setState 暴露出去：让「什么时候该回源」集中在 App 这一层，
  // hook 只负责连接本身与状态归约。
  useEffect(() => {
    if (!jobId || refreshTick === 0) return
    let cancelled = false
    void (async () => {
      try {
        const resp = await api.getJob(jobId)
        if (cancelled) return
        // 注意 `data` 这一层 —— 查询接口在信封之内还有一层（见 types.ts 的说明）。
        // 三样一起合并：只换 job 会让统计与进度条停在旧值上。
        mergeDetail({ job: resp.job, stat: resp.stat, progress: resp.progress })
      } catch {
        // 回源失败不影响实时流：下次重连时快照会补齐。
      }
    })()
    return () => {
      cancelled = true
    }
  }, [jobId, refreshTick, mergeDetail])

  // 时长留空时，跟着脚本实时估算 —— 否则用户提交前完全不知道成片会有多长，
  // 而这正是"自动判断时长"最容易被质疑的地方。
  //
  // 加了 600ms 防抖：估算本身是纯计算，但每敲一个字就发一次请求既没必要，
  // 也会让界面在打字时闪烁。
  useEffect(() => {
    const text = script.trim()
    if (text.length < 10) {
      setEstimate(null)
      return
    }
    let cancelled = false
    const timer = setTimeout(() => {
      api
        .estimateDuration(text)
        .then((r) => {
          if (cancelled) return
          setEstimate(r.duration_sec)
          setEstimateBasis(r.basis)
        })
        .catch(() => {
          // 估算失败不该打断提交：留空时服务端仍会自己算一遍。
          if (!cancelled) setEstimate(null)
        })
    }, 600)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [script])

  // 时长提示必须**如实反映本次会用哪个值**：留空时说清"按脚本自动估算"，
  // 填了就说清"不再自动估算"。否则用户无法判断自己填的数字有没有被采纳。
  const durationHint =
    duration !== null && duration !== undefined
      ? `将使用你指定的 ${duration} 秒（不再自动估算）`
      : estimate !== null
        ? `时长留空 → 按脚本自动估算：约 ${estimate} 秒`
        : '时长留空 → 按脚本自动估算'

  async function uploadBgm(file: File) {
    setBgmBusy(true)
    try {
      const resp = await api.uploadAsset(file)
      setBgm({
        assetId: resp.asset_id,
        filename: resp.filename,
        durationSec: resp.duration_sec,
        meanDb: resp.mean_volume_dbfs ?? null,
        peakDb: resp.peak_volume_dbfs ?? null,
        peakWarning: resp.peak_warning === true,
      })
      message.success(`已上传 ${resp.filename}（${formatTime(resp.duration_sec)}）`)
    } catch (err) {
      setBgm(null)
      message.error(errorText(err))
    } finally {
      setBgmBusy(false)
    }
  }

  async function submit(values: SubmitForm) {
    if (presenterBusy || bgmBusy || submitting) return
    setSubmitting(true)
    try {
      const effects: Effects = {}
      if (presenter) effects.presenter = presenter
      if (values.grade && values.grade !== 'none') {
        effects.grade = values.grade
        effects.grade_strength = values.grade_strength
      }
      if (values.fade_in_sec > 0) effects.fade_in_sec = values.fade_in_sec
      if (values.fade_out_sec > 0) effects.fade_out_sec = values.fade_out_sec
      if (values.burn_subtitles) effects.burn_subtitles = true
      // 只在烧录时带上样式：服务端会对"设了样式却没开烧录"直接报 400，
      // 与其让用户提交后才看到错误，不如在这里就不发。
      if (values.burn_subtitles) {
        effects.subtitle_style = {
          font_size: values.subtitle_font_size,
          primary_color: values.subtitle_color,
          margin_v: values.subtitle_margin_v,
        }
      }
      // volume_db 是**相对基准**的偏移（服务端先把配乐归一到 -20 LUFS）。
      // 之前这里漏了这个字段，滑块因此形同虚设 —— 服务端只能拿到默认值 0。
      if (bgm) effects.bgm = { asset_id: bgm.assetId, loop: true, volume_db: bgmVolumeDb }

      const req: GenerateRequest = {
        raw_script: values.raw_script.trim(),
        // 留空传 0：服务端据此走"按脚本自动估算"。
        target_duration_sec: values.target_duration_sec ?? 0,
        locale: 'zh-CN',
        style_guide: {
          animation_style: values.animation_style,
          preset: values.preset,
          background_style: values.background_style,
        },
        effects,
      }
      const resp = await api.generate(req)
      setResolved({
        sec: resp.target_duration_sec ?? 0,
        source: resp.duration_source ?? 'explicit',
      })
      setJobId(resp.job_id)
      message.success('任务已受理，正在拆解脚本')
    } catch (err) {
      message.error(errorText(err))
    } finally {
      setSubmitting(false)
    }
  }

  const shots = shotsOf(state.job)
  const stat = deriveStat(state)
  const reviewSummary = deriveReviewSummary(state)
  const js = jobStatus(state.job?.status ?? 'PENDING')
  const conn = CONNECTION[connection]
  const pending = stat.awaiting_human

  // 合成完成后回报的"实际生效的效果"。写在这里而不是只写日志：
  // 用户问"为什么没有背景音乐"时，答案要能在界面上看到。
  const effectsApplied = [...state.events]
    .reverse()
    .find((ev) => ev.node === 'compose' && ev.payload && 'effects' in ev.payload)?.payload
    ?.effects as Record<string, unknown> | undefined

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header className="app-header">
        <Space size={12} align="center">
          <Title level={4} style={{ margin: 0 }}>
            SciDirector · 分镜审核台
          </Title>
          <Text type="secondary" className="app-subtitle">
            导演 → 编码 → 渲染 → 视觉审查 → 人类反馈闭环
          </Text>
        </Space>
        <Space size={12}>
          {/*
            没有任务时不显示连接状态：此时前端根本没建立 WebSocket，
            而一个红色的「已断开」会让人以为系统出了问题 ——
            它描述的是一件当时并不存在的事。
          */}
          {jobId ? (
            <>
              <Badge status={conn.status} text={conn.label} />
              {connection === 'reconnecting' && (
                <Button size="small" icon={<ReloadOutlined />} onClick={reconnect}>
                  立即重试
                </Button>
              )}
            </>
          ) : null}
        </Space>
      </Header>

      <Content className="app-content">
        {lastError && connection !== 'open' && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message={`连接提示：${lastError}`}
            description="界面仍在自动重连，无需刷新页面。"
          />
        )}

        {!jobId && (
          <Card title="提交脚本">
            <Form<SubmitForm>
              form={form}
              noValidate
              layout="vertical"
              onFinish={(values) => void submit(values)}
              initialValues={{
                target_duration_sec: null,
                preset: 'default',
                animation_style: 'anime',
                background_style: 'auto',
                grade: 'none',
                grade_strength: 1,
                fade_in_sec: 0,
                fade_out_sec: 0,
                burn_subtitles: false,
                subtitle_font_size: 0,
                subtitle_color: '#ffffff',
                subtitle_margin_v: 0,
              }}
            >
              <Form.Item
                name="raw_script"
                label="科普脚本"
                rules={[
                  { required: true, message: '请输入脚本' },
                  { min: 20, message: '脚本至少 20 字' },
                ]}
              >
                <Input.TextArea
                  rows={6}
                  placeholder="把要讲解的科学内容贴进来，例如：用三分钟解释傅里叶变换的直觉……"
                />
              </Form.Item>

              <Row gutter={16} align="top">
                <Col xs={24} sm={12} md={8}>
                  <Form.Item name="animation_style" label="动画演出" extra="二次元模式在关键镜头穿插角色，公式与数据仍按科学图示呈现。">
                    <Select options={[{value:'anime',label:'二次元科学剧场'}, {value:'precise',label:'严谨图解'}]} />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={12} md={4}>
                  <Form.Item
                    name="target_duration_sec"
                    label="目标时长（秒）"
                    // 提示写在 label 下方：它是"会发生什么"的说明，
                    // 不是校验错误，因此不用 validateStatus 染色。
                    extra={<Text type="secondary" title={estimateBasis || undefined} style={{ fontSize: 12 }}>{durationHint}</Text>}
                  >
                    <InputNumber
                      min={5}
                      max={1800}
                      style={{ width: '100%' }}
                      placeholder="留空＝按脚本自动"
                    />
                  </Form.Item>
                </Col>
                <Col xs={12} sm={6} md={4}>
                  <Form.Item
                    name="preset"
                    label="配色预设"
                    tooltip="影响生成：决定模型画面用哪套颜色"
                  >
                    <Select options={STYLE_PRESETS} />
                  </Form.Item>
                </Col>
                <Col xs={12} sm={6} md={4}>
                  <Form.Item
                    name="background_style"
                    label="背景样式"
                    tooltip="影响生成：背景长什么样（纯色/网格/扫描线…）。与配色是两件独立的事"
                  >
                    <Select options={BACKGROUND_STYLES} />
                  </Form.Item>
                </Col>
                <Col xs={12} sm={6} md={6}>
                  <Form.Item name="grade" label="后期色调" tooltip="影响成片：合成阶段整体调色">
                    <Select options={GRADES} />
                  </Form.Item>
                </Col>
                <Col xs={12} sm={6} md={6}>
                  <Form.Item name="grade_strength" label="调色强度">
                    <Slider min={0.1} max={1} step={0.1} disabled={grade === 'none'} />
                  </Form.Item>
                </Col>
              </Row>

              <Divider orientation="left" plain style={{ marginTop: 0 }}>
                后期效果
              </Divider>

              <Row gutter={16}>
                <Col xs={12} sm={6} md={4}>
                  <Form.Item name="fade_in_sec" label="片头淡入（秒）">
                    <InputNumber min={0} max={5} step={0.5} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col xs={12} sm={6} md={4}>
                  <Form.Item name="fade_out_sec" label="片尾淡出（秒）">
                    <InputNumber min={0} max={5} step={0.5} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={12} md={6}>
                  <Form.Item
                    name="burn_subtitles"
                    label="字幕"
                    valuePropName="checked"
                    tooltip="烧进画面：任何播放器都看得到，代价是必须重编码"
                  >
                    <Switch checkedChildren="烧进画面" unCheckedChildren="软字幕" />
                  </Form.Item>
                </Col>
              </Row>

              {/* 字幕样式：不开烧录就没有意义，因此整体置灰而不是藏起来 ——
                  藏起来用户会以为"没有这个功能"。 */}
              <Row gutter={16}>
                <Col xs={12} sm={6} md={4}>
                  <Form.Item name="subtitle_font_size" label="字幕字号">
                    <InputNumber
                      min={0}
                      max={200}
                      disabled={!burnSubtitles}
                      placeholder="0＝自动"
                      style={{ width: '100%' }}
                    />
                  </Form.Item>
                </Col>
                <Col xs={12} sm={6} md={4}>
                  <Form.Item name="subtitle_color" label="字幕颜色">
                    {/*
                      用原生 color 输入而不是 antd ColorPicker：后者的值是
                      一个 Color 对象，要转成 #RRGGBB 才能进 effects，
                      而这一处只需要一个色值，不值得多一层转换。
                    */}
                    <input
                      type="color"
                      className="native-color"
                      disabled={!burnSubtitles}
                      value={subtitleColor}
                      onChange={(e) => form.setFieldValue('subtitle_color', e.target.value)}
                    />
                  </Form.Item>
                </Col>
                <Col xs={12} sm={6} md={4}>
                  <Form.Item name="subtitle_margin_v" label="距底边">
                    <InputNumber
                      min={0}
                      max={400}
                      disabled={!burnSubtitles}
                      placeholder="0＝自动"
                      style={{ width: '100%' }}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={6} md={8}>
                  {!burnSubtitles && (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      字幕样式只在「字幕烧进画面」时生效（软字幕的样式由播放器决定）。
                    </Text>
                  )}
                </Col>
              </Row>

              <PresenterSettings value={presenter} onChange={setPresenter} onBusyChange={setPresenterBusy} />
              <Form.Item label="背景音乐" style={{ marginBottom: 8 }}>
                {bgm ? (
                  <Space direction="vertical" style={{ width: '100%' }} size={8}>
                    <Space wrap>
                      <Tag color="green" bordered={false}>
                        {bgm.filename}
                      </Tag>
                      <Text type="secondary">{formatTime(bgm.durationSec)}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        比成片短的部分会自动循环
                      </Text>
                      <Button size="small" danger onClick={() => setBgm(null)}>
                        移除
                      </Button>
                    </Space>

                    {/* 试听：不给听就只能靠猜音量。原生 audio 自带播放与进度条，
                        接口支持 Range 因此可以拖动。 */}
                    <audio
                      controls
                      preload="none"
                      style={{ width: '100%', height: 32 }}
                      src={api.assetUrl(bgm.assetId)}
                    />

                    {/* 实测电平：滑块是"相对基准"的偏移，不知道源文件多响就没法调。 */}
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {bgm.meanDb !== null && bgm.peakDb !== null ? (
                        <>
                          源文件实测：平均 {bgm.meanDb.toFixed(1)} dB / 峰值{' '}
                          {bgm.peakDb.toFixed(1)} dB
                          {bgm.peakWarning && (
                            <Text type="danger" style={{ fontSize: 12 }}>
                              {' '}
                              · 峰值已贴近满刻度，源文件可能已经削顶（爆音），调音量救不回来
                            </Text>
                          )}
                        </>
                      ) : (
                        '未能测出源文件电平（不影响使用）'
                      )}
                    </Text>

                    <Space>
                      <Text style={{ fontSize: 12, whiteSpace: 'nowrap' }}>配乐音量</Text>
                      <input
                        type="range"
                        min={-24}
                        max={12}
                        step={1}
                        value={bgmVolumeDb}
                        onChange={(e) => setBgmVolumeDb(Number(e.target.value))}
                        style={{ width: 200 }}
                      />
                      <Text style={{ fontSize: 12, width: 110 }}>
                        {bgmVolumeDb > 0 ? `+${bgmVolumeDb}` : bgmVolumeDb} dB
                        {bgmVolumeDb === 0 ? '（基准）' : ''}
                      </Text>
                      <Button size="small" onClick={() => setBgmVolumeDb(0)}>
                        复位
                      </Button>
                    </Space>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      0 dB 已是"垫在旁白下面"的基准响度（约 -20 LUFS），通常不需要再调小；
                      听不清旁白时再往左拖。
                    </Text>
                  </Space>
                ) : (
                  <Upload.Dragger
                    accept="audio/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus"
                    maxCount={1}
                    showUploadList={false}
                    disabled={bgmBusy}
                    // 返回 false：拦住 antd 自己的上传，改由我们走
                    // api.uploadAsset（它要处理 {ok,data} 信封与错误文案）。
                    beforeUpload={(file) => {
                      void uploadBgm(file)
                      return false
                    }}
                  >
                    <p className="ant-upload-drag-icon">
                      <InboxOutlined />
                    </p>
                    <p className="ant-upload-text">{bgmBusy ? '上传中…' : '点击或拖拽音频文件到此处'}</p>
                    <p className="ant-upload-hint">
                      支持 mp3 / wav / m4a / aac / flac / ogg / opus，单个不超过 20MB
                    </p>
                  </Upload.Dragger>
                )}
              </Form.Item>

              <Button type="primary" size="large" htmlType="submit" aria-label="开始生成" aria-busy={submitting}
                loading={submitting} disabled={submitting || bgmBusy || presenterBusy}>
                开始生成
              </Button>
            </Form>
          </Card>
        )}

        {jobId && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Card
              title={
                <Space size={8} wrap>
                  <span>任务</span>
                  <Text code copyable style={{ fontSize: 13 }}>
                    {jobId}
                  </Text>
                  <Tag color={js.tag} bordered={false}>
                    {js.label}
                  </Tag>
                </Space>
              }
              extra={
                <Button size="small" onClick={() => setJobId(null)}>
                  新建任务
                </Button>
              }
            >
              {!state.loaded && <Text type="secondary">正在获取任务快照…</Text>}

              {resolved && resolved.sec > 0 && (
                <Paragraph type="secondary" style={{ marginBottom: 8 }}>
                  目标时长 {formatTime(resolved.sec)}
                  {resolved.source === 'auto' ? '（按脚本自动估算）' : '（你指定的）'}
                </Paragraph>
              )}

              <Progress
                percent={Math.round(state.progress * 100)}
                status={js.terminal && state.job?.status === 'FAILED' ? 'exception' : 'active'}
              />

              <Row gutter={16} style={{ marginTop: 8 }}>
                <Col xs={8} sm={5} md={4}>
                  <Statistic title="共" value={stat.total} suffix="个分镜" />
                </Col>
                <Col xs={8} sm={5} md={4}>
                  <Statistic title="已通过" value={stat.approved} valueStyle={{ color: '#2ecc71' }} />
                </Col>
                <Col xs={8} sm={5} md={4}>
                  <Statistic
                    title="待人工"
                    value={stat.awaiting_human}
                    valueStyle={{ color: pending > 0 ? '#ff9f43' : undefined }}
                  />
                </Col>
                <Col xs={8} sm={5} md={4}>
                  <Statistic title="技术失败" value={stat.failed} valueStyle={{ color: '#e74c3c' }} />
                </Col>
                <Col xs={8} sm={4} md={4}>
                  <Statistic title={state.job?.status === 'FAILED' ? '未处理' : '进行中'} value={stat.in_progress} />
                </Col>
              </Row>

              {stat.total > 0 && (
                <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
                  已审查 {reviewSummary.reviewed} 镜，通过率 {reviewSummary.passRate === null ? '暂无' : `${reviewSummary.passRate}%`}
                  {' · '}未审查 {reviewSummary.unreviewed} 镜
                </Text>
              )}

              {state.job?.error && (
                <Alert type="warning" showIcon style={{ marginTop: 16 }} message={state.job.error} />
              )}

              {state.job?.final_video_path && (
                <>
                  <Divider orientation="left" plain>
                    成片
                  </Divider>
                  {/* artifact 接口支持 Range，所以进度条能拖动。 */}
                  <video
                    className="film-video"
                    controls
                    preload="metadata"
                    src={api.artifactUrl(jobId)}
                  />
                  <Descriptions
                    size="small"
                    column={1}
                    style={{ marginTop: 12 }}
                    items={[
                      {
                        key: 'path',
                        label: '文件',
                        children: (
                          <Text code copyable style={{ fontSize: 12 }}>
                            {state.job.final_video_path}
                          </Text>
                        ),
                      },
                      {
                        key: 'effects',
                        label: '实际生效的效果',
                        children: effectsApplied ? (
                          <Space size={6} wrap>
                            <Tag
                              color={effectsApplied.bgm_applied ? 'green' : 'default'}
                              bordered={false}
                            >
                              {effectsApplied.bgm_applied ? '背景音乐 ✓' : '无背景音乐'}
                            </Tag>
                            <Tag
                              color={effectsApplied.post_applied ? 'blue' : 'default'}
                              bordered={false}
                            >
                              {effectsApplied.post_applied
                                ? `后期处理 ✓（${String(effectsApplied.grade || 'none')}）`
                                : '无后期处理'}
                            </Tag>
                            {effectsApplied.presenter_applied === true && <Tag color="cyan">Live2D · TTS 口型同步 ✓</Tag>}
                            {effectsApplied.burn_subtitles ? (
                              <Tag color="purple" bordered={false}>
                                字幕已烧录
                              </Tag>
                            ) : null}
                          </Space>
                        ) : (
                          <Text type="secondary">合成事件尚未上报</Text>
                        ),
                      },
                    ]}
                  />
                </>
              )}
            </Card>

            {pending > 0 && (
              <Alert
                type="warning"
                showIcon
                message={`有 ${pending} 个镜头需要人工复核`}
                description="镜头已在下方展开。请先检查视频、抽帧和未解决的问题，再决定放行或提出具体修改意见。"
              />
            )}

            <Card title="分镜表" extra={<Text type="secondary">共 {shots.length} 个</Text>}>
              {shots.length === 0 ? (
                <Text type="secondary">
                  {state.loaded ? '导演智能体尚未拆解出分镜。' : '加载中…'}
                </Text>
              ) : (
                <ShotTable
                  jobId={jobId}
                  shots={shots}
                  // 任务已进入终态时禁止再操作，避免发出注定被拒的请求。
                  disabled={js.terminal === true}
                  onChanged={() => setRefreshTick((n) => n + 1)}
                />
              )}
            </Card>

            <Card
              title="事件时间线"
              extra={
                <Text type="secondary">
                  共 {state.events.length} 条 · 已同步至 #{state.lastSeq}
                </Text>
              }
            >
              <EventTimeline events={state.events} />
            </Card>

            {state.job && (
              <Card title="任务概览" size="small">
                <Descriptions
                  size="small"
                  column={{ xs: 1, sm: 2, md: 3 }}
                  items={[
                    {
                      key: 'duration',
                      label: '目标时长',
                      children: formatTime(state.job.target_duration_sec),
                    },
                    { key: 'locale', label: '语言', children: state.job.locale },
                    {
                      key: 'created',
                      label: '创建时间',
                      children: new Date(state.job.created_at).toLocaleString('zh-CN'),
                    },
                  ]}
                />
                <Collapse
                  ghost
                  style={{ marginTop: 8 }}
                  items={[
                    {
                      key: 'script',
                      label: '原始脚本',
                      children: <pre className="script-view">{state.job.raw_script}</pre>,
                    },
                  ]}
                />
              </Card>
            )}
          </Space>
        )}
      </Content>
    </Layout>
  )
}
