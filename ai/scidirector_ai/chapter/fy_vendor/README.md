# fy-code-animation 组件快照

来源：https://github.com/Akillll-DuoHe/fy-code-animation

固定提交：`d9a545afa393b2ffab4f36e87371691daf8e502c`。
`source.json` 保存精确源路径与 SHA-256。两个 MJS 文件按原始字节保存，
在编译 HTML 时仅将命名 export 转成隔离的内联命名空间；不会加载远程模块。

仅采用上游独立编写的 `implementations/motion-core.mjs`、
`implementations/canvas-contract.mjs` 和 `scripts/style_library.py`。Python 读取器
按原始字节保存，通过独立子进程读取外部库。上游的来源说明见
https://github.com/Akillll-DuoHe/fy-code-animation/blob/d9a545afa393b2ffab4f36e87371691daf8e502c/LICENSES/README.md 。

原作者为《俯仰观照》，使用说明完整保留在 LICENSE.md。该部分不适用本项目的 MIT 许可；
原许可允许免费使用与分享，禁止重新包装为付费课程、付费 skills 或收费资料出售。
未引入上游第三方组件、风格索引、341 画风正文、字体或音视频。

SciDirector 的 Canvas 适配器、提示词与试验样例为本项目新增。
本试验不代表安装或完整实现上游 SKILL.md 的创作流程。
