# CodeReader · 本地离线代码解读工具

在浏览器里打开你的项目，选择一个代码文件：**左边看代码，右边看逐段中文解读**。
全程本地运行（llama.cpp + 本地 GGUF 大模型），不需要任何网络，代码不出本机，适合公司内网环境。

[下载最新 Windows 版本](https://github.com/EnzePeng/CodeReader/releases/latest)

## 功能

- **逐段中文解读**：Python 文件按 AST 精准切分（模块说明 / 导入 / 全局定义 / 类 / 函数 / 入口），逐段流式生成中文讲解；其他语言按通用规则分块解读
- **简单 / 逐行双模式**：右侧面板可切换「简单」（每段 2~4 句通俗概括）和「逐行」（按"第 X 行：…"逐行细讲）两种解读深度；同一段的两种结果分开缓存，随时切换秒回
- **自动全部 / 手动选块**：可以一键解读整个文件，也可以切到"手动选块"，只点想看的段落，每段还能各自选简单或逐行模式
- **项目视角的文件总览**：不只概括当前文件，还会结合项目地图说明它在整个项目中的职责、上游入口、下游依赖与主要数据流
- **跨文件关系打通**：打开项目后自动建立关系索引，解析函数、类、方法、导入别名、对象构造类型、直接/递归调用与反向调用方；解读每个分段时自动携带相关真实源码，而不再只看当前 `py` 文件的一小段
- **双栏联动**：点击右侧解读卡片，左侧代码高亮滚动到对应行；点击代码行，右侧定位对应卡片
- **结构大纲**：类 / 函数大纲导航，点击跳转
- **项目级追问**：先由 Harness 确定性装入当前范围、import 精确定义和调用证据；证据不足时再启动最多 3 步、8 次调用的只读研究循环，适合询问「这个函数在哪」「谁调用它」「数据如何流动」「这个文件在项目中负责什么」
- **跨文件连续会话**：追问按项目保存，点击证据切换到其他文件不会清空聊天；会话只在本地进程内保留 locator/hash，不长期复制源码正文
- **可验证代码证据**：总览、分段和追问在正文前返回项目内证据，引用可跳转到文件与行号；定义和引用问题优先返回索引事实
- **增量 Python 索引**：SQLite 持久化符号、调用、反向引用与 FTS5 文本块，只重建发生变化的文件，并展示索引状态
- **专业导航**：支持 `Ctrl+P` 文件搜索、`Ctrl+Shift+O` 项目符号、`F12` 定义、`Shift+F12` 引用以及 `Alt+Left/Right` 导航历史
- **模型切换**：顶栏下拉框可在 `models/` 目录内的多个 GGUF 之间切换（约 10~30 秒重载），选择会写回 config.json，下次启动仍然生效
- **思考模式开关**：使用 Qwen3.5 等思考型模型时，顶栏显示「思考开/关」按钮；关闭时秒级响应（默认），开启后模型先推理再作答、解读更深入但更慢；两种结果分别缓存
- **项目感知缓存**：解读结果按当前代码、模型、思考状态和项目索引指纹缓存；其他文件的相关实现变化后不会继续误用旧的跨文件解读
- **导出报告**：一键导出当前文件的完整中文解读为 Markdown
- **服务自愈**：模型服务意外退出时自动重启

## 目录结构（开发）

```
code-reader/
├── backend/            # FastAPI 后端（分段、解读编排、缓存、llama-server 托管）
├── frontend/           # React + Monaco 前端（构建产物 dist/ 由后端托管）
├── llama/              # llama.cpp Windows 运行时（需单独下载）
├── models/             # GGUF 模型文件（需单独下载）
├── data/               # 运行数据：解读缓存 cache.db、llama-server.log、最近打开记录
├── scripts/            # 打包脚本
└── config.json         # 运行配置
```

## 开发模式运行

要求：Python 3.13、Node 24 LTS（仅开发时需要）、NVIDIA GPU（8GB 显存即可）

```powershell
# 1. 后端依赖
cd backend
uv sync --frozen --all-groups

# 2. 前端构建（或 npm run dev 起开发服务器，已配置 /api 代理）
cd ../frontend
npm install
npm run build

# 3. 启动（会自动拉起 llama-server 并打开浏览器）
cd ../backend
python run.py
```

## 下载与安装

GitHub Release 提供轻量 Windows x64 压缩包，**不包含 llama.cpp 和 GGUF 模型**。这样安装包更小，也便于按目标机器的显卡选择推理后端和模型。

1. 从 [Releases](https://github.com/EnzePeng/CodeReader/releases/latest) 下载 `CodeReader-windows-x64-v*.zip` 并解压。
2. 从 [llama.cpp 官方 Releases](https://github.com/ggml-org/llama.cpp/releases/latest) 下载运行时：
   - NVIDIA 显卡：下载 Windows x64 CUDA 12.4 主压缩包和对应的 `cudart` 压缩包，将两者解压到同一个 `llama/` 目录。
   - 仅使用 CPU：下载 Windows x64 CPU 压缩包并解压到 `llama/`。
   - AMD/Intel 显卡：下载 Windows x64 Vulkan 压缩包并解压到 `llama/`。
   - 解压完成后应直接存在 `llama/llama-server.exe`，不要再多套一层目录。
3. 下载至少一个 GGUF 模型并放入 `models/`：
   - 默认模型：[Qwen3.5-9B Q4_K_M](https://huggingface.co/lmstudio-community/Qwen3.5-9B-GGUF/blob/main/Qwen3.5-9B-Q4_K_M.gguf)（社区 GGUF 转换）。
   - 更轻量：[Qwen2.5-Coder-7B-Instruct GGUF](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/tree/main)（Qwen 官方仓库，选择 Q4_K_M 文件）。
4. 将 `config.json` 中的 `llama.model` 改成实际下载的文件名。默认值是 `Qwen3.5-9B.Q4_K_M.gguf`；若下载文件名为 `Qwen3.5-9B-Q4_K_M.gguf`，也可以直接将文件重命名为默认值。
5. 双击 `CodeReader.exe`，浏览器会打开 http://127.0.0.1:8710；关闭控制台窗口即退出。

目标机器支持 Windows 10/11 x64。CUDA 包要求 NVIDIA 驱动支持对应 CUDA 版本，但无需安装 CUDA Toolkit；CPU 包不要求独立显卡。

## 打包与内网离线部署

在有网的开发机上执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

默认本地构建会包含已有的 llama.cpp 运行时和模型。构建与 GitHub Release 相同的轻量包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -SkipRuntime -SkipModel
```

产物为 `release\CodeReader\` 文件夹：

```
CodeReader/
├── CodeReader.exe      # 主程序（内嵌 Python 运行时与前端页面）
├── config.json         # 配置（模型路径、端口、显存参数等）
├── llama/              # llama.cpp 推理引擎，轻量包内只有下载说明
├── models/             # GGUF 模型，轻量包内只有下载说明
└── 使用说明.md
```

离线部署时，先在有网机器上按“下载与安装”补齐 `llama/` 和 `models/`，再把整个 `CodeReader` 文件夹通过 U 盘或内网共享复制到目标机器。

## 发布 GitHub Release

推送 `v` 开头的 Git 标签后，GitHub Actions 会执行后端、前端和打包冒烟测试，并自动创建 Release：

```powershell
git tag v2.1.0
git push origin v2.1.0
```

Release 附件包含轻量 ZIP 和对应的 SHA-256 校验文件，不会上传 `llama-server.exe`、DLL 或任何 `.gguf` 模型。

## 配置说明（config.json）

| 键 | 说明 |
| --- | --- |
| `app_port` | Web 界面端口（默认 8710） |
| `llama.model` | `models/` 下的 GGUF 文件名；不接受绝对路径或 `..` |
| `llama.think_prefill` | 思考块预填策略：`auto`（按模型名判断，qwen3/qwq 等思考型自动启用）/ `on` / `off` |
| `llama.ctx_size` | 上下文长度（默认 16384，显存紧张可降到 8192 或 4096） |
| `llama.n_gpu_layers` | GPU 加载层数（默认 99 全量；显存不足可调小，如 24） |
| `llama.cache_type_k/v` | KV 缓存量化（默认 q8_0 省显存） |
| `llama.thinking` | 思考模式开关（默认 false。思考型模型开启后解读更深但每段慢几十秒；可在顶栏「思考开/关」按钮切换，写回 config.json） |
| `llama.autostart` | 是否自动拉起 llama-server（连接已有服务时设为 false 并改 base_url） |
| `explain.segment_max_tokens` | 每段解读（简单模式）的生成上限 |
| `explain.segment_max_tokens_detailed` | 逐行模式每段的生成上限（默认 1600） |
| `explain.project_overview_context_tokens` | 文件总览的项目证据 token 预算（默认 1250） |
| `explain.project_segment_context_tokens` | 单个分段的项目证据 token 预算（默认 2000） |
| `explain.project_chat_context_tokens` | 旧客户端配置兼容字段；新 `/chat` 使用统一上下文总账本 |
| `explain.chat_current_file_tokens` | 旧客户端配置兼容字段；当前文件由 ContextBroker 按需装入 |
| `explain.project_dependency_depth` | 关联源码递归展开层数（默认 2） |
| `agent.protocol` | 研究协议：`auto` 探针后选择 native tool call，否则使用 JSON Schema |
| `agent.max_research_steps` / `max_tool_calls` | 单次追问最多 3 个研究步骤 / 8 次只读工具调用 |
| `agent.repo_map_tokens` / `tool_result_tokens` | 导航地图与单个模型可见工具结果上限 |
| `agent.context_soft_ratio` / `context_hard_ratio` | 统一上下文总账本的压缩阈值与硬上限（默认 0.75 / 0.90） |
| `agent.session_ttl_minutes` / `max_sessions` | 项目级内存会话 TTL 与 LRU 数量（默认 120 / 64） |

## 常见问题

- **启动提示端口被占用**：已有一个 CodeReader 在运行，直接访问 http://127.0.0.1:8710 即可
- **状态一直是"模型加载中"**：查看 `data/llama-server.log`；显存不足时把 `ctx_size` 降为 4096 或调小 `n_gpu_layers`
- **想换模型**：把新的 GGUF 放进 `models/`，在界面顶栏下拉框直接切换（无需重启）；缓存按模型区分，不会串。切换后已打开文件的解读仍显示旧模型的结果，点"全部重新生成"即可用新模型重算
- **切换到 qwen2.5-coder 后解读风格变了**：正常现象，不同模型的表述风格不同；qwen2.5-coder-7b 更快，Qwen3.5-9B 分析更细
- **解读结果想重算**：右侧面板"全部重新生成"，或单张卡片上的 ↺ 按钮
- **缓存太大**：删除 `data/cache.db` 即可（下次打开会重新生成）

## 技术栈

- 推理：llama.cpp（原生 `/v1/chat/completions` 与模型聊天模板；默认单 slot；随机 API key；仅监听 `127.0.0.1`）
- 模型：Qwen3.5-9B Q4_K_M / qwen2.5-coder-7B Q4_K_M（8GB 显存友好；快速模式使用 non-thinking profile，深度模式使用 thinking profile）
- 项目级上下文：Python 3.13 AST + SQLite/FTS5 增量索引，以精确定义/引用、调用图和 BM25 混合召回，再按模型 tokenizer 的 token 预算打包证据
- 追问 Harness：ContextBroker 确定性预取 + 有界只读 ResearchAgent + 项目级内存 ConversationStore；设计取舍见 [ADR-001](docs/decisions/ADR-001-evidence-first-cross-file-harness.md)
- 后端：FastAPI + httpx（SSE 流式），Python `ast` 分段，SQLite 缓存
- 前端：React 18 + Vite + Monaco Editor + react-markdown（全部本地打包，零 CDN）
- 打包：PyInstaller onefile

## 本地安全边界

CodeReader 的 HTTP API 是内部本地接口：主服务只允许 `127.0.0.1`，浏览器先建立随机 HttpOnly 会话，后续请求校验会话、Host 与 Origin。打开项目后，文件、结构、搜索、解读、追问和导出都只接受 `project_id + relative_path`；llama-server 使用独立随机 API key，并关闭 Web UI 与 slots 管理端点。
