# SkillPulse Demo

一个可直接运行的前后端演示，展示 **Agent Capability Trust Platform** 的本地原型。默认演示页面是 `frontend/lego/index.html`。

项目原创源码由团队按 [Apache-2.0](LICENSE) 许可开源；第三方模型、上游 Skill
和单独下载的外部脚本遵循各自许可，详见 `PROVENANCE.md`。本源码包不含密钥、
账号数据库、运行审计或外部脚本原件。

## 从 GitHub 下载并使用（专家 / 评委快速上手）

获取代码，任选其一：

- `git clone <本仓库 URL> && cd <仓库目录>`
- GitHub 网页右上角 **Download ZIP** → 解压到本地目录

**系统要求**：仅需 Python 3.10+（Windows / Linux / DGX Spark 均可），无需数据库、无需 Docker、无其它外部服务。

**启动（一条命令）**：

- Windows：双击或运行 `run.cmd`
- Linux / DGX：`bash start.sh`（首次先 `chmod +x start.sh`）

启动器会自动创建 `.venv`、安装 `backend/requirements.txt`（官方源失败自动回退清华 / 阿里镜像）并选择空闲端口，然后自动打开编排页面。

**使用**：打开 `http://127.0.0.1:8000/frontend/lego/index.html`，**无需登录**直接进入专家工作台，即可检索 / 安检 / 下载 Skill、在画布拖拽拼搭、运行流水线并查看审计（单机单用户、无鉴权，固定以专家身份运行）。

> 运行产生的账号、审计与报告写入 `workspace/outbox/`（已在 `.gitignore` 中，不会污染仓库），演示 Skill 与固定证据随仓库提供。更多演示路径见下文「演示要点」。

## 运行方式

1. 安装依赖
2. 启动后端
3. 打开前端
4. 演示本地 Skill 检索、GitHub 在线候选发现、五项基础安检、画布连线、受限子进程运行与审计报告

## 快速开始

```bash
cd backend
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

**新设备一键启动（推荐，无需手动配置环境）**

Windows 双击或运行：

```cmd
run.cmd
```

Linux / DGX Spark：

```bash
bash start.sh
```

两个启动器会自动完成：检测 Python 3.10+ → 创建 `.venv` → 安装 `backend/requirements.txt`（官方源失败自动回退清华 / 阿里镜像）→ 启动后端 → 打开编排页面。端口默认 8000，被占用时自动选择 8000–8010 间的空闲端口，也可用环境变量 `SKILLPULSE_PORT` 覆盖；Linux 下首次运行需 `chmod +x start.sh`。

打开 `http://127.0.0.1:8000/frontend/lego/index.html`。Windows 用户优先使用 `setup.cmd`、`start.cmd`；详见 `README-LOCAL.md`。
赛事 Spark 云节点（Linux/aarch64）请使用 `bash setup-dgx.sh`、`bash start-dgx.sh`，通过 SSH 本地端口转发访问；完整步骤见 `README-DGX.md`。
可选 StepFun 3.7 Flash 需求解析和 DGX 本地 CUDA 语义检索的安全配置、实机验收见 `README-STEPFUN-DGX.md`；没有配置时，两者不会伪报成功。
队友共享同一实例的安全接入方式见 `TEAM-DEMO.md`；赛事材料的逐项核对见 `CONTEST-READINESS.md`。

## 在线 Skill 来源发现

默认搜索会同时返回本地可检查资产和公开 GitHub 仓库中可识别的 `SKILL.md` 文件。在线结果列出仓库、发布账号、固定提交、文件路径与原始链接；当前覆盖 NVIDIA、Anthropic、OpenAI、Vercel 的公开 Skill 目录及 GitHub 仓库搜索候选，**不是全互联网爬取、也不是语义检索**。GitHub API 不可用或限流时，界面显示原因，本地闭环继续可用。[GitHub 未登录 API 额度较低](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)，多人连续查询建议控制频率。

线上候选统一标记 `DISCOVERED/UNKNOWN`，不可直接下载或运行。专家可按固定 Git 提交逐文件核对 Git blob 哈希后导入隔离区，进行基础静态初审；不自动安装到 `workspace/skills`，不执行外来代码。Git 提交和哈希只证明取得了与该仓库版本一致的内容，**不证明发布者身份或数字签名**。旧隔离记录若没有导入时的内容摘要会显示 `UNVERIFIED`，不能事后补成“当时已验证”，须重新导入固定版本。第三方标准 Agent Skill 若只有有效 `SKILL.md` 而无平台 `skill.schema.json`，隔离报告会标为 `PARTIAL` 并指出缺少执行契约，不会伪造可运行端口或自动准入。

## 项目结构
- `backend/app.py`：FastAPI 后端
- `frontend/lego/index.html`：主页面；`frontend/index.html` 是健康检查辅助页面
- `workspace/skills/`：演示用 skill 目录
- `workspace/outbox/reports/`：检查报告输出目录
- `scripts/http_smoke.py`：隔离的 HTTP 端到端验收，不使用真实账号或历史运行数据
- `evidence/dgx-spark-cloud-run-20260926.json`：赛事云节点的实测记录；含 NVIDIA GB10 与 CUDA 运算结果

## 架构、Skill 设计与本地算力

前端保留原有三栏布局：左侧输入需求并查看本地资产和线上候选，中间画布连接节点，右侧查看节点配置、运行结果及审计。FastAPI 后端负责检索、准入检查、账号权限、工作流校验与执行。每个可运行的本地 Skill 有明确的名称、版本、输入输出类型、依赖与资源声明；画布连线和后端运行器共用该 Schema，而不是以页面颜色或静态徽章判断能否执行。运行前重新校验节点、必填输入和类型，按拓扑顺序把上一节点输出映射到下一节点输入；本地运行器使用独立临时工作目录和带超时的子进程。该机制能减少演示任务相互污染，但不能取代操作系统级沙箱，未知第三方代码不会进入运行链。

治理层分别给出 Catalog 元数据、Scanned 基础静态规则、Evaluated 样例运行、Signed 本地完整性以及 Documented 文档检查的结果，并保留失败原因。健康报告采用摘要加按需展开详情的呈现方式；版本快照以内容哈希复核，审计记录只保存必要的脱敏摘要。这里的“Signed”不是公钥或上游 OMS 验签，因此界面同时明确显示发布者身份和数字签名未验证。GitHub 候选按仓库和固定提交展示来源，默认只能进入隔离初审，或由专家通过“下载并安检”跑完整五项流水线准入，不会因为搜到 `SKILL.md` 就自动信任。默认搜索是本地词项匹配加公开仓库范围内的发现，不是全网索引；无匹配时返回空结果，不再填充无关 Skill。可选 CUDA 模型仅在真实可用时提供语义排序。

平台适配方面，项目已在赛事云节点验证 GB10 环境与 CUDA 小型向量运算；主要 Skill 治理流水线仍由 CPU 执行。现提供可选的 StepFun 3.7 Flash 主动需求解析接口，以及仅在本地模型与 CUDA 可用时启用的 DGX 语义排序接口。2026-09-28 的一次获授权真实调用成功返回结构化需求、两项本地 Skill 推荐与匹配依据；此前一次调用因结构化校验失败未采信。单条成功案例不代表推荐质量评测完成。StepFun 成功后自动触发本地匹配，GitHub 在线查询仍需用户明确点击。DGX 已有一次固定模型的真实 CUDA 前向推理与 HTTP 检索证据，但没有性能优化对比或多样本质量评测；不能把示例视觉或 RAG mock 输出称为实时模型生成。自动化测试使用临时目录和临时账号，不改写用户已有报告或账号。

## 演示身份
- 本项目为**单机单用户演示**，无登录、无账号数据库、无鉴权，本地固定以专家身份运行；不需要任何凭据或默认密码。
- 专家可上传 ZIP 到私有待审区（隔离区），上传者可在个人列表查看记录；未通过完整安检的 GitHub 候选也会自动转入隔离区兜底。上传包始终为 `QUARANTINED`，不得下载、加入画布或运行。
- 工作流拓扑与脱敏运行摘要按本地默认身份保存。
- 不具备企业 SSO、MFA、细粒度租户隔离或生产级安全保证。

## 演示要点
- 在画布连接 `skill-doctor → skill-report`：前者实际检查本地文件，把 JSON 结果传给后者生成 Markdown 报告。
- 可用 `sample-skill-broken` 展示不健康目标的诊断结果；该目标本身不能下载或运行。
- 执行健康检查；损坏 Skill 会被阻止下载与运行，不提供伪造式自动修复
- 分支控制节点仅选择显式 true/false 路由；循环最多重复调用健康本地 Skill 5 次
- 为健康 Skill 创建可复核哈希的本地源码快照；查看新记录的审计哈希链、导出拓扑及本地审计报告
- 导出复合 Skill：把画布编排保存为可复用复合 Skill 资产包，仅引用原始子 Skill 的名称、版本与完整性状态，不拷贝、不修改原始 Skill 本体，保留原始签名与完整性证据。
- 导出 Gateway Skill：生成 function-call 类型的调用契约 manifest（`skillpulse-gateway.skill`），外部 Agent 加载后可通过 HTTP 调用整套 SkillPulse 中台（检索 / 安检 / 编排 / 审计）；中台本身不执行 Skill 业务，最终执行由外部 Agent/Harness 完成。
- 渐进披露：Skill 的 `skill.schema.json` 提供 `use_when` / `not_for` 触发条件，健康报告详情会展示，Agent 命中适用场景才加载完整正文。
- 从 GitHub 下载并安检：线上候选可直接“下载并安检”，按固定提交下载 Skill → 安全解压到可运行区 → 跑完整五项安检 → 仅 `HEALTHY` 才载入工具箱；未通过则自动回滚，隔离区仍作为兜底。

`skill-doctor` 和 `skill-report` 是真实、确定性的本地检查与格式化，不调用大模型；其他示例 Skill 的任务输出仍可能是 mock。`Signed` 阶段目前只做仓库本地 SHA-256 完整性比对，`signature_verified=false`、`origin_verified=false`；因此本地检查通过的 Skill 仍标为 `LOCAL/MED`，不能当作公钥验签或 NVIDIA 官方身份认证。本项目不是生产级安全沙箱。详见 `README-LOCAL.md`、`PROVENANCE.md` 和 `SUBMISSION-EVIDENCE.md`。

赛事提交文案见 `SUBMISSION.md`；技术架构与部署见 `TECHNICAL-ARCHITECTURE.md`、`README-DGX.md`；发布门槛见 `RELEASE-CHECKLIST.md`。本项目不会把小型 CUDA 冒烟写成真实大模型推理。运行 `.venv/Scripts/python.exe scripts/evaluate_local_search.py`（Windows）或 `.venv/bin/python scripts/evaluate_local_search.py`（Linux）可打印 10 条只读词项检索评测；该结果不代表 StepFun 或 DGX 模型效果。

## 固定外部动作样板（仅已准备的 DGX 环境）

`nvidia-skill-card-validator` 是 SkillPulse 为 NVIDIA/skills 固定提交中
`skill-card-generator/scripts/validate_submission.py` **单一动作**编写的运行适配，
不是完整 NVIDIA Skill，也不代表 NVIDIA 对本项目背书。来源提交、路径、脚本 SHA-256
和许可证在目录卡片及 `PROVENANCE.md` 中公开。准备好的 DGX 节点上，后端每次运行前
核对脚本 SHA-256 和容器镜像 ID，在无网络、无凭据、只读挂载、非 root、超时及资源限制
条件下执行；五项检查中的样例运行使用真实脚本。它可以接在
`skill-doctor → skill-report` 后，令 `report` 文本进入 `card` 端口，形成三节点数据流。
脚本只判断是否遗留 VERIFY/SELECT 人工审核标记，`PASS` 不是安全或合规认证。
Windows 或未准备镜像的 Linux 环境会显示 `UNHEALTHY` 并阻止载入运行。
普通 GitHub 在线候选默认仍保持隔离；专家也可选择“下载并安检”，让固定提交的 Skill 跑完整五项安检，仅 `HEALTHY` 才进入可运行区，未通过自动回滚。任何路径都不能绕过治理自动获得发布权限。


## 技术栈说明（NVIDIA SDK / 模型）

- **NVIDIA / DGX**：NVIDIA DGX Spark（GB10）云节点实测；CUDA Toolkit 13.0 环境完成真实 CUDA 前向推理与 HTTP 检索（证据见 `evidence/` 与 `SUBMISSION-EVIDENCE.md`）。
- **NVIDIA 相关资产**：上游目录 NVIDIA/skills（固定提交）中的单一脚本 `validate_submission.py` 适配为受控第三节点（`nvidia-skill-card-validator`），按固定 SHA-256 与容器镜像 ID 校验后执行；不是完整 NVIDIA Skill，也不代表 NVIDIA 背书。
- **StepFun 阶跃星辰**：`step-3.7-flash` 用于需求解析与 Skill 推荐（可选，经 `stepfun.env` 配置，中国区 `api.stepfun.com` / 全球区 `api.stepfun.ai`）。
- **模型（语义排序，可选）**：`intfloat/multilingual-e5-small`（ModelScope 固定 revision），DGX 本地 CUDA 推理，仅对已登记本地 Skill 简介排序。
- **明确边界**：本项目未集成 NVIDIA NIM / NeMo / TensorRT，未做量化、微调或性能基准；模型建议不改变安检与运行权限。
- **后端框架**：Python 3.10+ / FastAPI / Uvicorn；前端为原生 HTML/CSS/JS 单页工作台；外部数据源为 GitHub REST API（api.github.com）与 StepFun API。

## 部署说明（本地算力 · Agent Skills）

**智能体部署**：项目以「平台 + 网关」方式部署 Agent：后端提供检索 / 安检 / 编排 / 审计 HTTP 接口，导出 Gateway Skill（`skillpulse-gateway.skill`，function-call 契约），外部 Agent / Harness 加载后即可调用整套中台；**Skill 业务由外部 Agent 执行，平台不代跑**，前端画布与 Agent 调用共用同一本地运行器，效果一致。

**模型优化（如实说明）**：语义排序采用固定模型版本 + 本地加载 + 进程内模型缓存 + 归一化向量；检索分数不决定安全状态。未做量化、微调或增益基准（诚实边界）。

**Agent Skills 设计**：每个 Skill 以 `SKILL.md`（触发条件与指令）+ `skill-card.md`（能力卡）+ `skill.schema.json`（`use_when` / `not_for` 触发契约与输入输出类型）组织；画布连线与后端运行器共用同一 Schema，渐进披露——Agent 命中适用场景才加载完整正文；`workspace/skills/` 内置 5 个健康 Skill 与 1 个故意不健康示例，直接可被支持 skills 协议的 CLI Agent（Claude Code / Codex 等）挂载使用。
