# CodeReader UI 重设计规范（Graphite / 石墨·长春花）

> 版本：v1.0 · 2026-08　适用范围：`frontend/`（React + Vite + TS，纯 CSS，无 UI 框架）
> 本文档是**可无脑执行**的实施规范：所有色值、尺寸、动效参数均为最终值，直接照抄即可。
> 硬约束：**完全离线**（禁止任何 CDN / Google Fonts / 外部图标库）；**不重命名任何现有 class**（并行任务正在改 TSX）；新增 class 统一写在 styles.css 末尾的 redesign 区段。

---

## 目录

1. [风格定位与参考对象](#1-风格定位与参考对象)
2. [设计 Token 总表（:root 完整代码）](#2-设计-token-总表)
3. [字体方案（完全离线）](#3-字体方案完全离线)
4. [界面层级模型](#4-界面层级模型)
5. [逐组件优化清单](#5-逐组件优化清单)
6. [动效规范](#6-动效规范)
7. [无障碍与可读性](#7-无障碍与可读性)
8. [新旧变量 / class 映射与兼容策略](#8-新旧变量--class-映射与兼容策略)
9. [实施顺序清单（按文件）](#9-实施顺序清单按文件)
10. [视觉验收清单](#10-视觉验收清单)

---

## 1. 风格定位与参考对象

**一句话定位：Linear 式近单色石墨暗色 + 单一长春花蓝（Periwinkle）强调，按中文长时阅读调校的低眩光、hairline 分层界面。**

| 参考对象 | 借鉴什么 | 落到本产品 |
|---|---|---|
| **Linear** | 近单色底 + 唯一 lavender-blue 强调（Magic Blue #5E6AD2 系）；文字四阶灰；hairline 边框当"纹理"而非分割线；激活项 = 微染背景 + 细色条；13px 基准 + 4px 网格 | 强调色只出现在：主按钮、激活卡片/树行、状态灯、进度条、流式光标、焦点环。其余一律灰阶 |
| **Vercel Geist** | 功能定义的灰阶（背景/悬停/边框/文字各司其职）；半透明 alpha 灰专用于边框与悬停叠加，可压在任意底色上 | 边框用 `rgba(255,255,255,.06)` hairline；行悬停用 `rgba(255,255,255,.055)` 叠加层，不再用实色 `--bg-3` |
| **Zed / JetBrains Fleet** | 编辑器与 chrome 同色系融合、克制的语法配色（6~7 色） | 自定义 Monaco 主题 `codereader-dark`，编辑器背景与应用底色同值，语法色全部从强调色同族派生 |
| **暗色长阅读共识（2026）** | 底色避纯黑（#0A0A0A~#1A1A1A）；正文 off-white ≈87% 白；舒适对比 12~14:1 而非 17:1；暗色行高 1.65~1.75+；强调色降饱和防"发光" | 主底 #131318；正文 #E3E5EC（卡片上 13.3:1，恰在舒适区）；解读区中文行高 1.8；强调色取 #7A9BFF（比原 #4F8CFF 亮度更高、饱和更低，暗底上不刺眼） |

**为什么适合本产品**：这是一个"读"为主的工具——用户 80% 时间在看代码 + 看中文解读卡片，界面本身必须退后。近单色 + 单强调色把注意力留给内容；hairline + 柔和阴影在不增加亮度差的前提下建立层级（暗色里阴影几乎不可见，主要靠"逐级变亮的表面"）；内网离线部署意味着零外部资源，系统字体栈 + 纯 CSS 完全满足。

---

## 2. 设计 Token 总表

直接替换 `frontend/src/styles.css` 顶部的 `:root` 块。**旧变量以别名保留**（见 §8），并行任务写的旧变量名代码不会坏。

```css
:root {
  color-scheme: dark;

  /* ===== 背景（5 级，由深到浅 = 由低到高的海拔） ===== */
  --bg-inset:   #101015;  /* -1 级：输入框、代码块、segmented 轨道等"凹陷"面 */
  --bg-app:     #131318;  /*  0 级：应用底/编辑器底/卡片区底/欢迎页 */
  --bg-surface: #17181E;  /*  1 级：顶栏、侧栏、面板头等 chrome 表面 */
  --bg-raised:  #1C1D24;  /*  2 级：卡片、抽屉、气泡 */
  --bg-overlay: #22232C;  /*  3 级：弹窗、菜单、segmented 选中块 */

  /* 派生背景（叠加型，可压任意底色） */
  --bg-wash:   rgba(255, 255, 255, 0.03);   /* 次级按钮底 */
  --bg-hover:  rgba(255, 255, 255, 0.055);  /* 行/项悬停 */
  --bg-active: rgba(255, 255, 255, 0.08);   /* 行/项按下 */
  --bg-raised-active: #21242F;              /* 激活卡片底（raised + 6% 蓝染） */

  /* ===== 文字（3 级 + 装饰级） ===== */
  --text-primary:   #E3E5EC;  /* 正文/标题（卡片上 13.3:1） */
  --text-secondary: #A9ADBC;  /* 次级说明、图标（7.5:1） */
  --text-muted:     #7B8194;  /* 元信息：行号、字数、时间（4.3:1，仅限 ≥11px） */
  --text-faint:     #565B6E;  /* 装饰性占位符/禁用（≈2.5:1，不承载信息） */

  /* ===== 边框 ===== */
  --border-hairline: rgba(255, 255, 255, 0.06);  /* 默认发丝线：卡片、分栏、表头 */
  --border-default:  #262733;                    /* 实色边框：输入框、代码块 */
  --border-strong:   #333546;                    /* 悬停边框/分隔强调 */

  /* ===== 强调色（唯一彩色，长春花蓝） ===== */
  --accent:        #7A9BFF;                      /* 文字/图标/光标/进度 6.4:1 */
  --accent-hover:  #93AFFF;
  --accent-active: #6889F2;
  --accent-ink:    #0E1015;                      /* 主按钮上的深色文字 7.2:1 */
  --accent-soft:   rgba(122, 155, 255, 0.10);    /* 微染背景：激活行/激活卡 */
  --accent-border: rgba(122, 155, 255, 0.45);    /* 强调描边：激活卡边框 */
  --focus-ring:    rgba(122, 155, 255, 0.55);    /* 键盘焦点环 */

  /* ===== 语义色（各配 soft 底） ===== */
  --success:      #4CC38A;
  --success-soft: rgba(76, 195, 138, 0.10);
  --warning:      #E8B45A;
  --warning-soft: rgba(232, 180, 90, 0.10);
  --danger:       #F0717C;
  --danger-soft:  rgba(240, 113, 124, 0.10);

  /* ===== 阴影（3 级；暗色下只做"贴地感"，层级主要靠表面变亮） ===== */
  --shadow-1: 0 1px 2px rgba(0, 0, 0, 0.30);
  --shadow-2: 0 4px 12px rgba(0, 0, 0, 0.35);
  --shadow-3: 0 16px 40px rgba(0, 0, 0, 0.50);

  /* ===== 圆角 ===== */
  --radius-xs:   4px;   /* chip、行内代码 */
  --radius-sm:   6px;   /* 按钮、输入框、树行、segmented 内块 */
  --radius-md:   10px;  /* 卡片 */
  --radius-lg:   14px;  /* 弹窗、抽屉 */
  --radius-full: 999px; /* 状态灯、FAB */

  /* ===== 间距（4/8 基准） ===== */
  --sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px;
  --sp-4: 16px; --sp-5: 24px; --sp-6: 32px;

  /* ===== 字号 ===== */
  --fs-micro:   10px;    /* chip、徽标 */
  --fs-meta:    11px;    /* 行号、元信息 */
  --fs-ui-sm:   12px;    /* 次级按钮、tab、下拉 */
  --fs-ui:      13px;    /* UI 基准 */
  --fs-title:   14px;    /* 面板标题 */
  --fs-reading: 13.5px;  /* 解读卡正文（中文长阅读，行高 1.8） */

  /* ===== 字体 ===== */
  --font-ui: system-ui, "Segoe UI Variable Text", "Segoe UI",
             "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
             "Noto Sans CJK SC", sans-serif;
  --font-mono: "Cascadia Code", "Cascadia Mono", Consolas,
               "Sarasa Mono SC", "Courier New", monospace;

  /* ===== 动效 ===== */
  --dur-1: 100ms;   /* 微交互：hover 变色 */
  --dur-2: 160ms;   /* 状态切换：激活、边框、tab */
  --dur-3: 240ms;   /* 弹层进入：弹窗、抽屉 */
  --ease-standard: cubic-bezier(0.2, 0, 0, 1);
  --ease-enter:    cubic-bezier(0.16, 1, 0.3, 1);

  /* ===== 兼容别名（迁移期保留，并行任务的旧变量名代码继续有效；全部迁完后删） ===== */
  --bg: var(--bg-app);
  --bg-2: var(--bg-surface);
  --bg-3: var(--bg-overlay);
  --border: var(--border-hairline);
  --line: var(--border-default);
  --text: var(--text-primary);
  --muted: var(--text-secondary);
  --accent-dim: var(--accent-border);
  --card: var(--bg-raised);
  --card-active: var(--bg-raised-active);
  --ok: var(--success);
  --warn: var(--warning);
  --err: var(--danger);
}
```

> 注意：旧 `--border: #33364200` 是全透明死变量，从未产生视觉效果，别名指到 hairline 后若有旧代码引用它反而会"多出一条淡线"，属预期改进；确认无异常即可。
> `--accent-dim` 旧用途混杂（边框/气泡底/spinner 环/分隔条悬停），别名统一指向 `--accent-border`；styles.css 内部的这些用法会在 §5 中逐个改为语义正确的新 token，别名只兜底并行任务的新增代码。

---

## 3. 字体方案（完全离线）

全部依赖 Windows 本机字体，**零网络请求**。不引入任何 `@font-face` 远程源、不使用 Google Fonts / CDN。

| 用途 | font-family（完整） | 说明 |
|---|---|---|
| UI 与解读正文 | `system-ui, "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif` | Win11 `system-ui` 即 Segoe UI Variable；中文落到"微软雅黑 UI"（比"微软雅黑"字面更窄、行内混排更整齐）；PingFang/Noto 兜底 macOS/Linux 内网机 |
| 代码（Monaco + md 内代码） | `"Cascadia Code", "Cascadia Mono", Consolas, "Sarasa Mono SC", "Courier New", monospace` | Cascadia 随 Win10/11 终端分发，装了就用、没装静默回退 Consolas；本地栈查找无任何下载行为 |
| 数字对齐 | `font-variant-numeric: tabular-nums`（仅 `.card-lines` `.panel-meta` `.outline-line` `.progress-text`） | 行号/进度数字宽度稳定，流式更新时不抖动 |

全局基础样式（替换现有 `body` 规则）：

```css
body {
  font-family: var(--font-ui);
  font-size: var(--fs-ui);
  line-height: 1.5;
  background: var(--bg-app);
  color: var(--text-primary);
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

::selection { background: rgba(122, 155, 255, 0.28); color: #F0F2F7; }

:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
button:focus:not(:focus-visible) { outline: none; }
```

`frontend/index.html` 增加两行（防启动白闪 + 声明暗色）：

```html
<meta name="color-scheme" content="dark" />
<style>html { background: #131318; }</style>
```

---

## 4. 界面层级模型

「内容井最深、chrome 略亮、浮层最亮」。所有区域按下表取背景，**禁止就地发明新灰**：

| 海拔 | Token | 值 | 用在哪 |
|---|---|---|---|
| -1 凹陷 | `--bg-inset` | `#101015` | 项目路径输入框、追问 textarea、md 代码块/行内代码、segmented 轨道、状态灯底 |
| 0 内容井 | `--bg-app` | `#131318` | `body`、Monaco `editor.background`、`.explain-area`、`.welcome` |
| 1 chrome | `--bg-surface` | `#17181E` | `.topbar`、`.sidebar`、`.panel-head` |
| 2 悬浮 | `--bg-raised` | `#1C1D24` | `.card`、`.chat-drawer`、`.chat-bubble`（助手） |
| 3 浮层 | `--bg-overlay` | `#22232C` | `.modal`、segmented 选中块、下拉展开项 |

规则：
- 同海拔之间只用 hairline 分隔，不叠阴影；跨海拔（抽屉、弹窗）才用 `--shadow-2/3`。
- 行悬停一律用叠加型 `--bg-hover`（rgba），保证在任何海拔上表现一致。
- 中间代码栏与右侧解读区同为 0 级——两个"阅读井"视觉等权，侧栏/顶栏作为 1 级 chrome 略亮、自然退后。

---

## 5. 逐组件优化清单

以下按现有 class 逐条给出改法。**所有 class 名保持不变**。

### 5.1 顶栏（TopBar：`.topbar` `.brand` `.proj-open` `.recents` `.model-select` `.status-pill`）

| class | 现状 | 改法 |
|---|---|---|
| `.topbar` | 46px，`--bg-2` 底 | 高度 48px；`background: var(--bg-surface)`；`border-bottom: 1px solid var(--border-hairline)`；`padding: 0 var(--sp-4)`；`gap: var(--sp-3)` |
| `.brand-mark` | 蓝底白字 5px 圆角 | 保持强调色底但换深色墨字：`background: var(--accent); color: var(--accent-ink); border-radius: var(--radius-sm); font-weight: 700; letter-spacing: 0.02em` |
| `.brand-sub` | 灰字 | `color: var(--text-muted); font-size: var(--fs-ui-sm)` |
| `.proj-open input` | `--bg` 底 + `--line` 边 | 凹陷面：`background: var(--bg-inset); border: 1px solid var(--border-default); border-radius: var(--radius-sm); height: 28px; padding: 0 10px; color: var(--text-primary)`；`::placeholder { color: var(--text-faint); }`；聚焦态见下方代码块 |
| `.recents` / `.model-select` | 原生 select 样式 | 自绘下拉（见下方代码块）：去原生外观、内联 SVG 箭头（data URI，离线）、悬停边框变亮 |
| `.topbar-err` | 红字 | `color: var(--danger); font-size: var(--fs-ui-sm)` 不变，仅换 token |
| `.status-pill` | 白描边药丸 + 彩点 | 重写为"凹陷药丸 + 发光点"（见下方代码块）；色彩纪律：仅圆点带色，err 态才染文字与边框 |

```css
/* 输入框聚焦：边框亮 + 内环，不用 outline 抢焦点环 */
.proj-open input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(122, 155, 255, 0.15);
}

/* 自绘下拉框（.recents 与 .model-select 共用） */
.recents {
  appearance: none;
  -webkit-appearance: none;
  height: 28px;
  padding: 0 26px 0 10px;
  max-width: 200px;
  background-color: var(--bg-inset);
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%237B8194' stroke-width='1.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 9px center;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: var(--fs-ui-sm);
  cursor: pointer;
  outline: none;
  transition: border-color var(--dur-2) var(--ease-standard),
              color var(--dur-2) var(--ease-standard);
}
.recents:hover:not(:disabled) { border-color: var(--border-strong); color: var(--text-primary); }
.recents:focus-visible { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(122, 155, 255, 0.15); }
.recents:disabled { opacity: 0.45; cursor: default; }
/* 展开项底色（Chromium 支持有限，但离线 Edge/Chrome 均生效） */
.recents option { background: var(--bg-overlay); color: var(--text-primary); }

/* 状态灯：凹陷药丸 + 发光点 */
.status-pill {
  display: flex; align-items: center; gap: 7px;
  height: 26px; padding: 0 12px; white-space: nowrap;
  border-radius: var(--radius-full);
  font-size: var(--fs-ui-sm);
  background: var(--bg-inset);
  border: 1px solid var(--border-hairline);
  color: var(--text-secondary);
  transition: border-color var(--dur-2) var(--ease-standard);
}
.status-pill .dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.status-pill.ok .dot {
  background: var(--success);
  box-shadow: 0 0 0 3px rgba(76, 195, 138, 0.15);
}
.status-pill.busy .dot {
  background: var(--warning);
  box-shadow: 0 0 0 3px rgba(232, 180, 90, 0.15);
  animation: pulse 1.2s ease-in-out infinite;
}
.status-pill.err {
  border-color: rgba(240, 113, 124, 0.35);
  color: #F5A3AB;
}
.status-pill.err .dot {
  background: var(--danger);
  box-shadow: 0 0 0 3px rgba(240, 113, 124, 0.15);
}
@keyframes pulse { 50% { opacity: 0.35; } }
```

### 5.2 侧栏（`.sidebar` `.side-tabs` `.filetree` `.tree-row` `.arrow` `.file-chip` / Outline：`.outline-row`）

| class | 现状 | 改法 |
|---|---|---|
| `.sidebar` | `--bg-2` + 右边框 | `background: var(--bg-surface); border-right: 1px solid var(--border-hairline)` |
| `.side-tabs button` | 底部 2px 直角下划线 | 高度 34px；`font-size: var(--fs-ui-sm)`；悬停 `color: var(--text-secondary)`；激活下划线改为**圆角短线**（见下方代码块） |
| `.tree-root-label` | 虚线分隔 | `font-size: var(--fs-meta); color: var(--text-muted); border-bottom: 1px solid var(--border-hairline)`（虚线改实 hairline，减少噪点） |
| `.tree-row` | 全宽行 + 左侧 2px 色条 | 改"圆角悬浮行"（Linear 式）：行两侧留 6px 边距、`border-radius: var(--radius-sm)`、去掉左色条；激活 = `--accent-soft` 微染 + 文字提亮（见下方代码块） |
| `.arrow` | CSS 三角 | 改细 chevron（旋转的 L 形边框），线宽 1.5px，展开旋转 90°（见下方代码块） |
| `.file-chip` | 灰底扩展名徽标 | `font-family: var(--font-mono); font-size: var(--fs-micro); border-radius: var(--radius-xs); padding: 0 4px; background: rgba(255,255,255,0.06); color: var(--text-muted)`；`.ext-py { background: rgba(122,155,255,0.13); color: #A9C0FF; }` |
| `.outline-row` | 同 tree-row 旧样式 | 与 `.tree-row` 同步为圆角悬浮行；`.outline-line` 加 `font-variant-numeric: tabular-nums` |

```css
/* 侧栏页签：圆角短下划线 */
.side-tabs { display: flex; border-bottom: 1px solid var(--border-hairline); }
.side-tabs button {
  flex: 1; height: 34px; position: relative;
  background: none; border: none; cursor: pointer;
  color: var(--text-muted); font-size: var(--fs-ui-sm);
  transition: color var(--dur-2) var(--ease-standard);
}
.side-tabs button:hover { color: var(--text-secondary); }
.side-tabs button.active { color: var(--text-primary); }
.side-tabs button.active::after {
  content: ''; position: absolute; left: 50%; bottom: -1px;
  width: 32px; height: 2px; transform: translateX(-50%);
  border-radius: 1px; background: var(--accent);
}

/* 文件树行：圆角悬浮行 */
.tree-row {
  display: flex; align-items: center; gap: 6px;
  margin: 0 6px; padding: 3px 6px; height: 26px;
  border-radius: var(--radius-sm);
  cursor: pointer; white-space: nowrap;
  color: var(--text-secondary);
  transition: background var(--dur-1) var(--ease-standard),
              color var(--dur-1) var(--ease-standard);
}
.tree-row:hover { background: var(--bg-hover); color: var(--text-primary); }
.tree-row.active { background: var(--accent-soft); color: #DEE6FF; }
.tree-row.dim .tree-name { color: var(--text-muted); }
.tree-row.loading { color: var(--text-faint); cursor: default; }

/* chevron 箭头 */
.arrow {
  width: 10px; height: 10px; position: relative; flex-shrink: 0;
  transition: transform var(--dur-2) var(--ease-standard);
}
.arrow::before {
  content: ''; position: absolute; top: 2.5px; left: 2px;
  width: 4px; height: 4px;
  border-right: 1.5px solid var(--text-muted);
  border-bottom: 1.5px solid var(--text-muted);
  transform: rotate(-45deg);
}
.arrow.open { transform: rotate(90deg); }
```

### 5.3 分隔条（`.divider`）

现状 5px 实色块，悬停整块变蓝，视觉重。改为**默认隐形、悬停浮现 2px 强调线**：

```css
.divider {
  width: 5px; cursor: col-resize; flex-shrink: 0;
  background: transparent;
  border-left: 1px solid var(--border-hairline);
  position: relative;
}
.divider::after {
  content: ''; position: absolute; inset: 0 1px;
  border-radius: 1px; background: transparent;
  transition: background var(--dur-2) var(--ease-standard);
}
.divider:hover::after, .divider:active::after { background: var(--accent-border); }
```

### 5.4 Monaco 区（`.code-area` + 自定义主题）

**新建 `frontend/src/monacoTheme.ts`**，替换 `vs-dark`，让编辑器与应用底色融为一体：

```ts
import * as monaco from 'monaco-editor'

// 与 styles.css 的 token 对齐：编辑器背景 = --bg-app，语法色从强调色同族派生
export function registerCodeReaderTheme() {
  monaco.editor.defineTheme('codereader-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [
      { token: '', foreground: 'D5D9E8', background: '131318' },
      { token: 'comment', foreground: '6B7390', fontStyle: 'italic' },
      { token: 'keyword', foreground: '82A0FF' },
      { token: 'string', foreground: '83C892' },
      { token: 'number', foreground: 'E3B15F' },
      { token: 'constant', foreground: 'E3B15F' },
      { token: 'type', foreground: '56C1D6' },
      { token: 'class', foreground: '56C1D6' },
      { token: 'function', foreground: 'B9A3F2' },
      { token: 'variable', foreground: 'D5D9E8' },
      { token: 'operator', foreground: '8E95AD' },
      { token: 'delimiter', foreground: '8E95AD' },
      { token: 'tag', foreground: 'F2949C' },
      { token: 'attribute.name', foreground: 'E3B15F' },
      { token: 'regexp', foreground: '83C892' },
    ],
    colors: {
      'editor.background': '#131318',
      'editor.foreground': '#D5D9E8',
      'editor.lineHighlightBackground': '#FFFFFF06',
      'editor.selectionBackground': '#7A9BFF2E',
      'editor.inactiveSelectionBackground': '#7A9BFF17',
      'editorCursor.foreground': '#7A9BFF',
      'editorLineNumber.foreground': '#4E5366',
      'editorLineNumber.activeForeground': '#9AA3BF',
      /* 新老版本 monaco 的缩进参考线 key 都写上，多余的会被忽略 */
      'editorIndentGuide.background': '#22232C',
      'editorIndentGuide.activeBackground': '#333546',
      'editorIndentGuide.background1': '#22232C',
      'editorIndentGuide.activeBackground1': '#333546',
      'editorGutter.background': '#131318',
      'editorWidget.background': '#1C1D24',
      'editorWidget.border': '#262733',
      'minimap.background': '#131318',
      'minimapSlider.background': '#FFFFFF0F',
      'minimapSlider.hoverBackground': '#FFFFFF1A',
      'minimapSlider.activeBackground': '#FFFFFF24',
      'scrollbarSlider.background': '#FFFFFF14',
      'scrollbarSlider.hoverBackground': '#FFFFFF22',
      'scrollbarSlider.activeBackground': '#FFFFFF2E',
      'editor.findMatchBackground': '#E8B45A40',
      'editor.findMatchHighlightBackground': '#E8B45A22',
      'editorBracketMatch.background': '#7A9BFF22',
      'editorBracketMatch.border': '#7A9BFF55',
      'editorOverviewRuler.border': '#00000000',
      'scrollbar.shadow': '#00000000',
    },
  })
}
```

`CodePane.tsx` 改动（实施阶段执行）：

- 文件顶部 `import { registerCodeReaderTheme } from '../monacoTheme'`，模块级调用一次 `registerCodeReaderTheme()`；
- `theme="vs-dark"` → `theme="codereader-dark"`；
- options 增改：`fontFamily: "'Cascadia Code', 'Cascadia Mono', Consolas, 'Courier New', monospace"`、`fontLigatures: true`、`lineHeight: 20`、`padding: { top: 8 }`。

联动高亮装饰（styles.css）：

```css
.seg-active-line { background: rgba(122, 155, 255, 0.07); }
.seg-active-gutter {
  background: var(--accent); width: 3px !important;
  margin-left: 3px; border-radius: 2px;
}
```

### 5.5 解读面板头（`.panel-head` `.panel-file` `.panel-meta` `.progress-bar` `.banner`）

| class | 改法 |
|---|---|
| `.panel-head` | `background: var(--bg-surface); border-bottom: 1px solid var(--border-hairline); padding: 10px var(--sp-4) 8px` |
| `.panel-file` | `font-size: var(--fs-title); font-weight: 600` |
| `.panel-meta` | `color: var(--text-muted); font-size: var(--fs-meta); font-variant-numeric: tabular-nums` |
| `.progress-text` | 同上加 `tabular-nums` |
| `.explain-panel` 空态 `.panel-placeholder` | `color: var(--text-muted)`，配合 §5.15 空状态图形 |

```css
/* 进度条：2px 圆角 + 渐变填充 */
.progress-bar {
  height: 2px; background: rgba(255, 255, 255, 0.06);
  border-radius: 1px; margin-top: 8px; overflow: hidden;
}
.progress-bar div {
  height: 100%; border-radius: 1px;
  background: linear-gradient(90deg, var(--accent), #A9C4FF);
  transition: width 0.3s var(--ease-standard);
}

/* 横幅：soft 底 + 左侧色点，不再用整块脏黄/脏红 */
.banner {
  display: flex; align-items: center; gap: 8px;
  padding: 7px var(--sp-4); font-size: var(--fs-ui-sm); flex-shrink: 0;
  border-bottom: 1px solid var(--border-hairline);
}
.banner::before {
  content: ''; width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
}
.banner.busy { background: rgba(232, 180, 90, 0.08); color: #E5C283; }
.banner.busy::before { background: var(--warning); animation: pulse 1.2s ease-in-out infinite; }
.banner.err { background: rgba(240, 113, 124, 0.09); color: #F5A3AB; }
.banner.err::before { background: var(--danger); }
```

### 5.6 解读卡片（`.card` `.seg-card` `.card-head` `.card-body` `.spinner` `.caret`）——核心组件，全量代码

状态矩阵：普通（done）/ 生成中（streaming）/ 排队（idle + explaining）/ 错误（文本内嵌）/ 缓存（chip-cache）/ 激活（active）/ 悬停。
实施阶段在 `SegmentCard.tsx` 根节点补状态 class：`` className={`card seg-card ${active ? 'active' : ''} ${status === 'streaming' ? 'streaming' : ''} ${status === 'idle' && explaining ? 'queued' : ''}`} ``（若暂时不能改 TSX，可用 `:has()` 兜底：`.seg-card:has(.spinner)` ≈ streaming）。

```css
.cards {
  flex: 1; overflow-y: auto; padding: var(--sp-3);
  display: flex; flex-direction: column; gap: 10px;
  background: var(--bg-app);
}

.card {
  position: relative;                 /* 供流式左轨 ::before 定位 */
  background: var(--bg-raised);
  border: 1px solid var(--border-hairline);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-1);
  overflow: hidden;
  flex-shrink: 0;                     /* 保留：列式 flex 防压扁 */
}

/* 可点卡片：悬停仅提边框与阴影，禁止 transform（滚动中会抖） */
.seg-card {
  cursor: pointer;
  transition: border-color var(--dur-2) var(--ease-standard),
              background var(--dur-2) var(--ease-standard),
              box-shadow var(--dur-2) var(--ease-standard);
}
.seg-card:hover { border-color: var(--border-strong); box-shadow: var(--shadow-2); }
.seg-card.active {
  border-color: var(--accent-border);
  background: var(--bg-raised-active);
  box-shadow: 0 0 0 1px var(--accent-border), var(--shadow-2);
}

/* 生成中：呼吸左轨 + 微亮边框 */
.seg-card.streaming { border-color: rgba(122, 155, 255, 0.30); }
.seg-card.streaming::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 2px;
  background: var(--accent);
  animation: rail-breathe 1.8s ease-in-out infinite;
}
@keyframes rail-breathe { 50% { opacity: 0.35; } }

/* 排队中：整卡降不透明度，配合骨架屏（§5.16） */
.seg-card.queued { opacity: 0.75; }

.card-head {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-hairline);
}
.card-title {
  font-weight: 600; font-size: var(--fs-ui);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.card-lines {
  color: var(--text-muted); font-size: var(--fs-meta);
  white-space: nowrap; font-variant-numeric: tabular-nums;
}
.card-tools { margin-left: auto; display: flex; align-items: center; gap: 4px; }

/* 解读正文：中文长阅读参数 */
.card-body {
  padding: 10px 14px 12px;
  font-size: var(--fs-reading);
  line-height: 1.8;
  color: var(--text-primary);
}

/* 总览卡：强调左沿，标识"全文入口" */
.overview-card { border-left: 2px solid var(--accent); }

/* spinner：细环 */
.spinner {
  width: 12px; height: 12px; border-radius: 50%;
  border: 1.5px solid var(--border-strong);
  border-top-color: var(--accent);
  animation: spin 0.8s linear infinite; display: inline-block;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* 流式打字光标：2px 细条（原 7px 色块太重） */
.caret {
  display: inline-block; width: 2px; height: 1.1em;
  margin-left: 2px; border-radius: 1px;
  background: var(--accent); vertical-align: -0.15em;
  animation: blink 1s steps(2, start) infinite;
}
@keyframes blink { 50% { opacity: 0; } }
```

### 5.7 chip / 徽标体系（`.chip` `.chip-*` + 新增模式徽标）

统一公式：**背景 = 色相 13% 透明染，文字 = 同色相高明度**，不描边、不发光：

```css
.chip {
  flex-shrink: 0; font-size: var(--fs-micro); font-weight: 500;
  padding: 1px 6px; border-radius: var(--radius-xs);
  background: rgba(255, 255, 255, 0.06); color: var(--text-muted);
}
.chip-class, .chip-class_header { background: rgba(167, 139, 250, 0.13); color: #C7B3F7; }
.chip-function, .chip-method    { background: rgba(122, 155, 255, 0.13); color: #A9C0FF; }
.chip-imports                   { background: rgba(94, 214, 180, 0.12);  color: #85D9C4; }
.chip-globals                   { background: rgba(232, 180, 90, 0.13);  color: #E5CB8B; }
.chip-docstring, .chip-overview { background: rgba(96, 165, 250, 0.13);  color: #97CBF2; }
.chip-main                      { background: rgba(240, 113, 124, 0.13); color: #F2ABA4; }
.chip-cache                     { background: rgba(76, 195, 138, 0.13);  color: #93D9A8; }
.chip-dir                       { background: rgba(255, 255, 255, 0.06); color: var(--text-secondary); }
.chip-file                      { background: rgba(122, 155, 255, 0.13); color: #A9C0FF;
                                  max-width: 160px; overflow: hidden; text-overflow: ellipsis; }

/* 新增：解读模式徽标（并行任务的「简单解读/逐行解读」标识） */
.mode-badge {
  font-size: var(--fs-micro); font-weight: 500;
  padding: 1px 6px; border-radius: var(--radius-xs); white-space: nowrap;
}
.mode-badge.simple { background: rgba(122, 155, 255, 0.13); color: #A9C0FF; }  /* 简单解读 */
.mode-badge.line   { background: rgba(167, 139, 250, 0.13); color: #C7B3F7; }  /* 逐行解读 */
```

### 5.8 按钮体系（`.btn-primary` `.btn-sm` `.btn-ghost` `.icon-btn`）

主按钮改「强调底 + 深色墨字」（对比 7.2:1，暗界面上比白字更高级也更可读）：

```css
.btn-primary {
  height: 28px; padding: 0 14px;
  border: none; border-radius: var(--radius-sm);
  background: var(--accent); color: var(--accent-ink);
  font-size: var(--fs-ui-sm); font-weight: 600;
  cursor: pointer; white-space: nowrap;
  transition: background var(--dur-1) var(--ease-standard);
}
.btn-primary:hover:not(:disabled)  { background: var(--accent-hover); }
.btn-primary:active:not(:disabled) { background: var(--accent-active); }
.btn-primary:disabled { opacity: 0.4; cursor: default; }

.btn-sm {
  height: 26px; padding: 0 10px;
  display: inline-flex; align-items: center; gap: 5px;
  background: var(--bg-wash);
  border: 1px solid var(--border-default);
  color: var(--text-secondary);
  border-radius: var(--radius-sm);
  font-size: var(--fs-ui-sm);
  cursor: pointer; white-space: nowrap; text-decoration: none;
  transition: background var(--dur-1) var(--ease-standard),
              border-color var(--dur-1) var(--ease-standard),
              color var(--dur-1) var(--ease-standard);
}
.btn-sm:hover:not(:disabled) {
  background: var(--bg-hover);
  border-color: var(--border-strong);
  color: var(--text-primary);
}
.btn-sm:disabled { opacity: 0.45; cursor: default; }
.btn-sm.warn { border-color: rgba(232, 180, 90, 0.4); color: #E5C283; }
.btn-sm.warn:hover:not(:disabled) { background: var(--warning-soft); border-color: var(--warning); }

.btn-ghost {
  background: none; border: none; cursor: pointer;
  color: var(--text-muted); font-size: 16px;
  border-radius: var(--radius-sm); padding: 2px 6px;
  transition: color var(--dur-1) var(--ease-standard),
              background var(--dur-1) var(--ease-standard);
}
.btn-ghost:hover { color: var(--text-primary); background: var(--bg-hover); }

.icon-btn {
  height: 22px; min-width: 22px; padding: 0 5px;
  display: inline-flex; align-items: center; justify-content: center;
  background: none; border: 1px solid transparent;
  color: var(--text-muted); font-size: var(--fs-ui-sm);
  border-radius: var(--radius-sm); cursor: pointer;
  transition: color var(--dur-1) var(--ease-standard),
              background var(--dur-1) var(--ease-standard);
}
.icon-btn:hover:not(:disabled) { color: var(--text-primary); background: var(--bg-hover); }
.icon-btn:disabled { opacity: 0.4; cursor: default; }
```

### 5.9 segmented 模式切换（新增，供并行任务的「简单/逐行」全局切换）

```css
.segmented {
  display: inline-flex; gap: 2px; padding: 2px;
  background: var(--bg-inset);
  border: 1px solid var(--border-hairline);
  border-radius: 8px;
}
.segmented > button {
  height: 22px; padding: 0 10px;
  border: none; border-radius: var(--radius-sm);
  background: transparent; color: var(--text-muted);
  font-size: var(--fs-ui-sm); cursor: pointer; white-space: nowrap;
  transition: color var(--dur-2) var(--ease-standard),
              background var(--dur-2) var(--ease-standard);
}
.segmented > button:hover { color: var(--text-secondary); }
.segmented > button.active {
  background: var(--bg-overlay); color: var(--text-primary);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.35), inset 0 0 0 1px var(--border-hairline);
}
/* 卡片头内的小号变体（「简单解读/逐行解读」按钮组可直接用） */
.segmented--sm > button { height: 20px; padding: 0 8px; font-size: var(--fs-micro); }
```

> 若并行任务已用其他 class 名实现，**保留其命名、套用以上数值**即可（轨道 `--bg-inset`、选中块 `--bg-overlay` + shadow、文字三态 muted→secondary→primary）。

### 5.10 追问抽屉（`.chat-fab` `.chat-drawer` `.chat-bubble` `.chat-input`）

| class | 改法 |
|---|---|
| `.chat-fab` | `background: var(--accent); color: var(--accent-ink); font-weight: 600; border-radius: var(--radius-full); box-shadow: var(--shadow-2)`；悬停 `background: var(--accent-hover); box-shadow: var(--shadow-3)` |
| `.chat-drawer` | `background: var(--bg-raised); border: 1px solid var(--border-default); border-radius: var(--radius-lg); box-shadow: var(--shadow-3)`；进场动画 `pop-in`（§6） |
| `.chat-head` | 加 `border-bottom: 1px solid var(--border-hairline); font-size: var(--fs-title)` |
| `.chat-ctx` | `border-bottom: 1px solid var(--border-hairline); color: var(--text-secondary)` |
| `.chat-bubble` | 助手：`background: var(--bg-overlay); border: 1px solid var(--border-hairline); border-radius: 10px`；正文继承 §5.6 阅读参数（`font-size: var(--fs-reading); line-height: 1.8`） |
| `.chat-msg.user .chat-bubble` | 改微染：`background: rgba(122, 155, 255, 0.16); border-color: rgba(122, 155, 255, 0.25); color: #DEE6FF`（原实心深蓝块太重） |
| `.chat-input textarea` | `background: var(--bg-inset); border: 1px solid var(--border-default); border-radius: var(--radius-sm)`；聚焦同 §5.1 输入框（accent 边框 + 内环） |
| `.chat-empty` | `color: var(--text-muted); line-height: 1.8` |

### 5.11 弹窗（`.modal-mask` `.modal` `.picker-*`）

```css
.modal-mask {
  position: fixed; inset: 0; z-index: 100;
  display: flex; align-items: center; justify-content: center;
  background: rgba(9, 10, 14, 0.62);
  backdrop-filter: blur(4px);           /* 仅弹窗遮罩允许 blur */
  animation: overlay-in var(--dur-2) var(--ease-standard);
}
.modal {
  width: 480px; max-height: 70vh;
  background: var(--bg-overlay);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-3);
  display: flex; flex-direction: column;
  animation: pop-in var(--dur-3) var(--ease-enter);
}
.modal-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 16px; font-weight: 600; font-size: var(--fs-title);
  border-bottom: 1px solid var(--border-hairline);
}
.modal-foot { padding: 12px 16px; border-top: 1px solid var(--border-hairline); text-align: right; }
.picker-path { padding: 10px 16px; border-bottom: 1px solid var(--border-hairline); }
.picker-current { color: var(--text-muted); font-size: var(--fs-ui-sm); }
.picker-err { padding: 8px 16px; color: var(--danger); font-size: var(--fs-ui-sm); }
.picker-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-radius: var(--radius-sm); cursor: pointer;
  transition: background var(--dur-1) var(--ease-standard);
}
.picker-item:hover { background: var(--bg-hover); }
```

### 5.12 滚动条（全局）

```css
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.08);
  border-radius: 5px;
  border: 3px solid transparent;      /* 内缩成 4px 视觉宽度的"悬浮胶囊" */
  background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.16);
  background-clip: padding-box;
}
::-webkit-scrollbar-corner { background: transparent; }
```

### 5.13 欢迎页与空状态（`.welcome` `.side-empty` `.panel-placeholder`）

纯 CSS"插画"：三层错位卡片 + 强调条，零图片资源。实施时在 `App.tsx` 欢迎区顶部插入 `<div className="welcome-art"><i/><i/><i/></div>`（TSX 一行改动，可与并行任务协调后做，不做也不影响其余样式）：

```css
.welcome { gap: 10px; color: var(--text-secondary); }
.welcome h2 { color: var(--text-primary); font-size: 22px; font-weight: 650; letter-spacing: 0.01em; }
.welcome ol { line-height: 2.1; padding-left: 20px; color: var(--text-secondary); }
.welcome .dim { font-size: var(--fs-ui-sm); color: var(--text-muted); max-width: 380px; text-align: center; }

.welcome-art { position: relative; width: 104px; height: 76px; margin-bottom: 6px; }
.welcome-art::before {
  content: ''; position: absolute; inset: 0;
  border-radius: 12px; border: 1px solid var(--border-default);
  background: var(--bg-surface);
  transform: translate(10px, -10px) rotate(4deg);
  opacity: 0.5;
}
.welcome-art::after {
  content: ''; position: absolute; inset: 0;
  border-radius: 12px; border: 1px solid var(--border-strong);
  background: linear-gradient(160deg, var(--bg-raised), var(--bg-surface));
  box-shadow: var(--shadow-2);
}
.welcome-art i { position: absolute; z-index: 1; border-radius: 3px; height: 6px; }
.welcome-art i:nth-child(1) { left: 16px; top: 18px; width: 42px; background: var(--accent); opacity: 0.9; }
.welcome-art i:nth-child(2) { left: 16px; top: 32px; width: 64px; background: var(--bg-overlay); }
.welcome-art i:nth-child(3) { left: 16px; top: 46px; width: 52px; background: var(--bg-overlay); }

.side-empty {
  margin: 14px 10px; padding: 16px 14px;
  border: 1px dashed var(--border-default); border-radius: var(--radius-md);
  color: var(--text-muted); line-height: 1.8; font-size: var(--fs-ui-sm);
  text-align: center;
}
```

### 5.14 骨架屏（新增 `.skl`，用于排队卡片）

实施阶段把 `SegmentCard.tsx` 中 `排队等待解读…` 的占位文本替换为两条骨架线（保留文案作 `title` 或辅助文本亦可）：

```css
.skl {
  height: 10px; border-radius: 5px;
  background: linear-gradient(90deg,
    rgba(255, 255, 255, 0.05) 25%,
    rgba(255, 255, 255, 0.09) 50%,
    rgba(255, 255, 255, 0.05) 75%);
  background-size: 200% 100%;
  animation: skl-wave 1.6s ease-in-out infinite;
}
.skl + .skl { margin-top: 8px; width: 72%; }
@keyframes skl-wave {
  from { background-position: 200% 0; }
  to   { background-position: -200% 0; }
}
```

TSX 结构建议：`<div className="skl" /><div className="skl" />`。若本阶段不动 TSX，排队态沿用 `.dim` 文本 + `.seg-card.queued` 降透明度，不阻塞其余实施。

### 5.15 Markdown 排版（`.md`，解读卡与聊天气泡共用）

```css
.md p { margin: 6px 0; }
.md ul, .md ol { margin: 6px 0; padding-left: 18px; }
.md li { margin: 4px 0; }
.md li::marker { color: var(--text-muted); }
.md code {
  font-family: var(--font-mono); font-size: 12px;
  background: var(--bg-inset);
  border: 1px solid var(--border-hairline);
  border-radius: var(--radius-xs);
  padding: 1px 5px; color: #A9C0FF;
}
.md pre {
  background: var(--bg-inset);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 10px 12px; overflow-x: auto; margin: 8px 0;
}
.md pre code {
  border: none; padding: 0; background: none;
  color: #D5D9E8; font-size: 12.5px; line-height: 1.6;
}
.md h1, .md h2 {
  font-size: var(--fs-ui); font-weight: 600; color: var(--text-primary);
  margin: 12px 0 4px; padding-left: 8px;
  border-left: 2px solid var(--accent-border);
}
.md h3, .md h4 { font-size: var(--fs-ui); font-weight: 600; margin: 10px 0 4px; }
.md strong { color: #F0F2F7; font-weight: 600; }
.md blockquote {
  border-left: 2px solid var(--border-strong);
  padding: 2px 0 2px 10px; margin: 8px 0;
  color: var(--text-secondary);
}
.md table { border-collapse: collapse; margin: 8px 0; font-size: 12.5px; }
.md th { background: rgba(255, 255, 255, 0.04); font-weight: 600; }
.md th, .md td { border: 1px solid var(--border-default); padding: 5px 10px; }
.md a { color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent-border); }
.md hr { border: none; border-top: 1px solid var(--border-default); margin: 10px 0; }
```

---

## 6. 动效规范

**时长与曲线**（已入 token）：

| Token | 值 | 用途 |
|---|---|---|
| `--dur-1` 100ms | 悬停变色（按钮、行、icon-btn） |
| `--dur-2` 160ms | 状态切换（tab、卡片激活、边框、chevron 旋转） |
| `--dur-3` 240ms | 弹层进入（modal、drawer） |
| `--ease-standard` `cubic-bezier(0.2, 0, 0, 1)` | 一切常规过渡 |
| `--ease-enter` `cubic-bezier(0.16, 1, 0.3, 1)` | 进场（带轻微回弹感的减速） |

**进场动画**：

```css
@keyframes overlay-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes pop-in {
  from { opacity: 0; transform: translateY(6px) scale(0.985); }
  to   { opacity: 1; transform: none; }
}
/* .modal-mask → overlay-in；.modal / .chat-drawer → pop-in（drawer 加 transform-origin: bottom right） */
```

**加过渡的元素**（白名单属性：`color / background / border-color / box-shadow / opacity / transform`）：
按钮三件套、`.tree-row` / `.outline-row` / `.picker-item`、`.seg-card`（border/background/shadow）、`.side-tabs button`、`.arrow`（transform）、`.divider::after`、`.recents`、状态灯边框。

**禁止过渡/动画的场景**：
- **流式文本区**（`.card-body`、`.chat-bubble` 内容）：不得对高度、字色、透明度做过渡——token 逐字追加时任何过渡都会造成频闪与重排抖动；打字光标只允许 `opacity` 阶跃闪烁（`steps(2)`）。
- `.seg-card:hover` 禁用 `transform`（卡片流滚动 + hover 组合会抖）。
- `backdrop-filter` 仅允许在 `.modal-mask`；侧栏/顶栏禁用（Monaco 同屏时 GPU 合成成本高）。
- 无限循环动画仅限 5 个：状态灯 `pulse`、`spinner`、`caret`、`.skl` 波纹、streaming 左轨 `rail-breathe`。

**系统减动效**：

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
  .caret { animation: none; opacity: 1; }   /* 光标保持常亮而非消失 */
}
```

---

## 7. 无障碍与可读性

### 7.1 对比度实测表（WCAG 2.x 相对亮度公式核算）

| 前景 / 背景 | 用途 | 对比度 | 结论 |
|---|---|---|---|
| `#E3E5EC` / `#131318` | 正文 · 内容井 | **14.7:1** | AAA |
| `#E3E5EC` / `#1C1D24` | 解读卡正文 | **13.3:1** | AAA，落在 12~14:1 长阅读舒适区 |
| `#A9ADBC` / `#1C1D24` | 次级文字 · 卡片 | **7.5:1** | AAA |
| `#A9ADBC` / `#17181E` | 次级文字 · chrome | **7.9:1** | AAA |
| `#7B8194` / `#1C1D24` | 元信息（行号等） | **4.3:1** | ≈AA，仅限 ≥11px 非关键信息 |
| `#7A9BFF` / `#1C1D24` | 强调文字/链接 | **6.4:1** | AA+（>4.5） |
| `#0E1015` / `#7A9BFF` | 主按钮文字 | **7.2:1** | AAA |
| `#E5C283` / `#282523`(合成) | busy 横幅文字 | **9.0:1** | AAA |
| `#F5A3AB` / `#2B2027`(合成) | 错误横幅文字 | **8.0:1** | AAA |
| `#4CC38A` / `#17181E` | 状态灯圆点 | **8.0:1** | 图形 ≥3:1 ✓ |

约束：`--text-faint` 只做占位符/装饰（≈2.5:1，不承载必读信息）；Monaco 行号 `#4E5366` 为装饰级，当前行号自动提亮到 `#9AA3BF`。

### 7.2 中文排版

- 解读正文 `13.5px / 1.8`（暗色 + 中文在 1.65~1.75 通用建议上再加一档）；UI 文本 `13px / 1.5`；欢迎页步骤 `2.1`。
- 中文不加 `letter-spacing`（方块字自带节奏，加了反而松散）；仅 `.brand-mark` 拉丁缩写允许 `0.02em`。
- 中英/中数混排交给字体栈处理，不手工加空格；数字场景统一 `tabular-nums`。
- 段落间距用 `margin`（6px）而非空行；卡片正文左右内边距 14px，保证行长在面板 42% 宽度下约 30~45 字/行。

### 7.3 键盘与焦点

- 全局 `:focus-visible` 焦点环（§3），鼠标点击不显示（`:focus:not(:focus-visible)` 清除）。
- 输入框聚焦 = accent 边框 + 3px 内环（双保险，色弱用户仍能靠亮度差识别）。
- 弹窗遮罩点击关闭保留；`.modal` 出现时焦点应移入（实施阶段在 FolderPicker 首个按钮加 `autoFocus`，一行改动，可选）。

---

## 8. 新旧变量 / class 映射与兼容策略

### 8.1 CSS 变量映射（旧 → 新）

| 旧变量 | 新变量 | 新值 | 备注 |
|---|---|---|---|
| `--bg` | `--bg-app` | `#131318` | 旧 `#17181d` |
| `--bg-2` | `--bg-surface` | `#17181E` | 旧 `#1e1f26` |
| `--bg-3` | `--bg-overlay`（弹层）<br>`--bg-hover`（行悬停，rgba） | `#22232C`<br>`rgba(255,255,255,.055)` | 旧值一物多用，按用途拆分 |
| `--border` | `--border-hairline` | `rgba(255,255,255,.06)` | 旧值全透明（死变量） |
| `--line` | `--border-default` | `#262733` | 旧 `#2e3039` |
| （新增） | `--border-strong` | `#333546` | 悬停边框 |
| `--text` | `--text-primary` | `#E3E5EC` | 旧 `#d7dae2` |
| `--muted` | `--text-secondary`（说明文字）<br>`--text-muted`（元信息） | `#A9ADBC`<br>`#7B8194` | 旧值一物多用，按用途拆分 |
| `--accent` | `--accent` | `#7A9BFF` | 旧 `#4f8cff`，降饱和提亮度 |
| `--accent-dim` | `--accent-soft` / `--accent-border` / `--accent-hover` | 见 §2 | 旧用途混杂，按用途拆分；别名指向 `--accent-border` |
| `--ok` | `--success` | `#4CC38A` | |
| `--warn` | `--warning` | `#E8B45A` | |
| `--err` | `--danger` | `#F0717C` | |
| `--card` | `--bg-raised` | `#1C1D24` | |
| `--card-active` | `--bg-raised-active` | `#21242F` | |

**兼容策略**：`:root` 末尾保留旧名 → 新名的别名块（§2 已给出）。并行任务新增的任何 `var(--bg-3)`、`var(--muted)` 等写法继续生效且自动获得新配色。两个任务都合入后，再做一次全局查替删除别名（可选清理项，不阻塞）。

### 8.2 class 增改清单

**零重命名**。新增 class（全部写在 styles.css 末尾 `/* ===== redesign additions ===== */` 区段，避免与并行任务的 diff 冲突）：

| 新 class | 用途 | 需要的 TSX 配合 |
|---|---|---|
| `.seg-card.streaming` | 生成中卡片左轨呼吸 | SegmentCard 根节点按 `status` 加 class（或临时用 `.seg-card:has(.spinner)`） |
| `.seg-card.queued` | 排队卡片降透明 | 同上（`status==='idle' && explaining`） |
| `.skl` | 骨架线 | SegmentCard 排队态渲染两条 `<div className="skl"/>`（可选） |
| `.segmented` / `.segmented--sm` | 全局与卡片级模式切换 | 并行任务控件直接套用；如其已有命名则按 §5.9 数值对齐 |
| `.mode-badge`（`.simple` / `.line`） | 「简单解读/逐行解读」徽标 | 同上 |
| `.welcome-art` | 欢迎页纯 CSS 插画 | App.tsx 插一行三个 `<i/>`（可选） |

### 8.3 与并行任务的协调注意

- `styles.css` 是共享热点：本次重设计**整文件重写**，实施前先确认并行任务对 styles.css 的追加已合入，或将其追加段落原样搬进新文件末尾再统一 token 化。
- 并行任务新增控件（segmented 模式切换、卡片上的模式按钮、模式徽标）如已带内联样式或临时 class，迁移到 §5.9 / §5.7 规范即可，**视觉参数以本文为准**。
- TopBar / ChatDrawer / FileTree / Outline 无需改 TSX 即可获得全部新样式。

---

## 9. 实施顺序清单（按文件）

按依赖顺序执行，每步可独立验证、可独立回滚：

1. **`frontend/index.html`**：`<head>` 加 `<meta name="color-scheme" content="dark" />` 与 `<style>html{background:#131318}</style>`。（2 行，零风险）
2. **`frontend/src/styles.css`**——整文件按本规范重写，顺序：
   a. `:root` 全量 token + 兼容别名（§2）；
   b. 全局基础：`body` 字体栈/字号、`::selection`、`:focus-visible`、滚动条（§3、§5.12）；
   c. 顶栏 + 状态灯 + 下拉框（§5.1）；
   d. 侧栏：tabs、树行、chevron、大纲、root-label（§5.2）；
   e. 分隔条（§5.3）、布局区背景对齐层级模型（§4：`.explain-area`/`.cards` 用 `--bg-app`）；
   f. 面板头、进度条、横幅（§5.5）；
   g. 卡片全状态 + spinner + caret（§5.6）、总览卡；
   h. chip 体系 + mode-badge（§5.7）；
   i. 按钮体系（§5.8）；
   j. 追问抽屉（§5.10）、弹窗（§5.11）；
   k. Markdown 排版（§5.15）；
   l. 文件末尾 redesign additions 区段：segmented、skl、welcome-art、进场动画 keyframes、`prefers-reduced-motion`（§5.9/5.13/5.14/§6）。
3. **`frontend/src/monacoTheme.ts`**（新建）：§5.4 全量代码。
4. **`frontend/src/components/CodePane.tsx`**：import + 注册主题；`theme="codereader-dark"`；options 加 `fontLigatures/lineHeight/padding`、fontFamily 换 Cascadia 栈。
5. **`frontend/src/components/SegmentCard.tsx`**（小改）：根节点补 `streaming/queued` 状态 class；排队态换骨架线；卡片头预留 `mode-badge` 插槽（与并行任务对齐）。
6. **`frontend/src/components/ExplainPanel.tsx`**（小改，可选）：总览卡在 `overview.status==='streaming'` 时加 `streaming` class，与分段卡行为一致。
7. **`frontend/src/App.tsx`**（一行，可选）：欢迎区插入 `.welcome-art`。
8. **回归验证**：`npm run dev` 后按 §10 走查；重点确认与并行任务新控件同屏无样式冲突。

预计改动文件：`index.html`、`styles.css`（主体）、`monacoTheme.ts`（新建）、`CodePane.tsx`、`SegmentCard.tsx`、`ExplainPanel.tsx`（可选）、`App.tsx`（可选）。后端零改动。

---

## 10. 视觉验收清单

- [ ] 顶栏/侧栏（1 级）明显比编辑器与卡片区底（0 级）亮一档，卡片（2 级）再亮一档；无突兀纯黑或纯白。
- [ ] 全界面只有一种彩色（长春花蓝）+ 三个语义色点缀；截图灰度化后层级依然清晰。
- [ ] Monaco 背景与 `.cards` 区底色一致（#131318），编辑器不再是"另一块黑"。
- [ ] 打开大文件流式生成：卡片文字无闪烁、无抖动；streaming 卡左轨呼吸、caret 细条闪烁；排队卡呈骨架/半透明。
- [ ] 缓存徽标、模式徽标、分段 chip 均为"微染底 + 同族亮字"，无高饱和实心块。
- [ ] 状态灯三态（就绪/加载/失败）可辨且不刺眼；模型切换期间 busy 点脉冲。
- [ ] 键盘 Tab 走查：所有可交互元素有可见焦点环；鼠标点击无焦点环残留。
- [ ] 滚动条静默（4px 视觉胶囊），hover 变亮；弹窗/抽屉进出场 240ms 无跳帧。
- [ ] 中文解读正文行高 1.8、行内代码与代码块使用等宽栈；断网环境（内网机）下无任何字体/资源请求失败。
