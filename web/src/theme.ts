import { theme } from 'antd'
import type { ThemeConfig } from 'antd'

/**
 * antd 主题。
 *
 * 两个决定值得说明：
 *
 * 1. **用内置的 `darkAlgorithm`，而不是自己写一套暗色变量。**
 *    审核台用深色是为了长时间盯屏幕，而暗色最难做的恰恰是那些"只差一两个像素"的
 *    层级（分隔线、悬浮态、禁用态、表头底色）—— 手写必然漏掉几处，漏掉的地方在
 *    深色下会糊成一片。算法版会把这些一次性算对。
 *
 * 2. **主色沿用项目原有的 `#4F8CFF`**，与成片里的 `primary_color` 是同一个值。
 *    界面和产出物用同一套颜色，看图时不会有"屏幕上的蓝"和"片子里的蓝"两个概念。
 */
export const BRAND = '#4F8CFF'

/** 与 styles.css 里的 --bg / --panel 保持一致，避免两套深色打架。 */
export const SURFACE = {
  bg: '#0B1020',
  panel: '#0F1730',
  panelQuiet: '#0D1428',
  border: '#1E2740',
  text: '#E6ECFF',
  muted: '#8A94A6',
}

export const antdTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: BRAND,
    colorBgLayout: SURFACE.bg,
    colorBgContainer: SURFACE.panel,
    colorBgElevated: SURFACE.panel,
    colorBorder: SURFACE.border,
    colorBorderSecondary: SURFACE.border,
    colorText: SURFACE.text,
    colorTextSecondary: SURFACE.muted,
    borderRadius: 8,
    fontSize: 14,
    // 字体与 styles.css 一致：中文优先用系统 UI 字体，避免 antd 默认字体
    // 在 Windows 上把中文回退成宋体（那是审核台里最刺眼的一处不一致）。
    fontFamily:
      "system-ui, 'Noto Sans CJK SC', 'Microsoft YaHei', -apple-system, sans-serif",
  },
  components: {
    Layout: { headerBg: SURFACE.bg, bodyBg: SURFACE.bg, headerHeight: 64 },
    Card: { colorBgContainer: SURFACE.panel },
    Table: {
      headerBg: SURFACE.panelQuiet,
      // 表格是这一页的主角，行高留松一点更好扫读。
      cellPaddingBlock: 10,
    },
    Timeline: { itemPaddingBottom: 12 },
  },
}
