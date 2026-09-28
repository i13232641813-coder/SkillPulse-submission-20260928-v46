# SkillPulse 本地运行（Windows）

## 首次配置

1. 安装 Python 3.10 或更高版本，并确保 `python` 命令可用。
2. 双击 `setup.cmd`。它会在项目目录创建 `.venv`，并安装 `backend/requirements.txt` 中固定的直接与间接依赖版本。
3. 本地 Skill 检索、安检和演示执行不需要 `backend/.env`、API Key 或付费模型。页面还可从公开 GitHub 仓库发现带 `SKILL.md` 的候选，并显示仓库、提交和来源链接；此功能需要后端能访问 GitHub，属于有范围的在线发现，**不是全网或语义检索**。外部候选只能进入隔离初审，不能直接运行。`skill-doctor → skill-report` 实际检查本地 Skill 文件并生成报告；其他示例 Skill 的任务输出仍可能是 mock，均不代表真实模型或 NVIDIA 硬件推理。
4. （可选）启用 AI 需求解析：把 `stepfun.env.example` 复制为 `stepfun.env`，填入 `STEPFUN_API_KEY`（中国区默认 `china`；全球区用 `global`，两区密钥不可混用）。`run.cmd` 启动时会自动读取该文件；未配置时该功能停用，本地检索与安检不受影响。`stepfun.env` 已被 git 忽略，请勿提交密钥。
5. 双击 `start.cmd`。浏览器会打开 SkillPulse 主页面并**直接进入专家工作台**，无需登录。API 文档地址为 `http://127.0.0.1:8000/docs`，若 8000 端口被占用则使用 8001。

## 手动启动

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

随后打开 `http://127.0.0.1:8000/frontend/lego/index.html`。旧的 `frontend/index.html` 只是健康检查辅助页。

`.env` 和 `.venv` 是本机文件，不应提交到公开仓库。不要使用或分享未经密钥持有人许可的 API Key。

## 本地身份与上传隔离区（Phase 3）

- 本项目为**单机单用户演示**：无登录、无账号数据库、无鉴权，后端固定以本地专家身份运行，不需要任何凭据或默认密码。请勿把此服务暴露到公网或共享网络；它不是生产级安全边界。
- 界面为单一专家工作台（无业务视图切换）。本地固定以专家身份运行，审计事件关联本地 actor_id；客户端上报的操作类型不能单独证明真实用户行为。
- 专家上传 `.zip` 后，文件被保存在 `workspace/outbox/private_skills/<用户ID>/<上传ID>/`；上传者可在个人列表查看记录。ZIP 限 5 MiB / 100 文件 / 解压后 20 MiB；拒绝路径穿越、符号链接和常见密钥文件。静态预检只解析元数据、文档与少量代码模式；**不执行上传代码**，也不把上传包加入可运行 Skill 目录。
- 上传包的 Signed 永远标记 `UNVERIFIED`、Evaluated 标记 `NOT_RUN`，整体 `QUARANTINED/HIGH`。本阶段没有自动审批、晋级或发布流程；即使静态检查通过也不能下载、加入画布或运行。

## 本地审计与数据归档（Phase 4）

- 专家可在隔离区查看待审上传的文件名/大小/SHA-256 清单与文本预览。上传内容不可信；不要把文件里的指令当作系统操作要求。
- 未通过完整安检的 GitHub 候选会自动转入隔离区兜底。任何上传包均为 `QUARANTINED/HIGH`，未完成受信任数字签名和安全执行验证，也没有自动晋级/发布入口。
- 流程拓扑保存在 `workspace/outbox/user_workflows/<用户ID>/`，输入值不保存；载入时会重新核对 Skill 版本与当前安检状态，必填输入需重新填写。新运行摘要保存在 `workspace/outbox/user_runs/<用户ID>/`，只包含节点状态、字段名和内容哈希等脱敏信息。旧的 `workspace/outbox/catalogs/` 历史文件原样保留，不自动迁移或改写。
- `/api/me/workflows` 和 `/api/me/runs` 按本地默认身份读取；全局审计对当前专家可见。该隔离只在本地应用 API 层生效：同一操作系统账号若能直接读取 `outbox` 文件，仍可看到其他账号的本地文件；它不等于租户级或生产级隔离。审计链和初审文件不是跨文件原子事务，也没有外部锚定。
- 回归命令：在 `backend` 目录运行 `..\.venv\Scripts\python.exe -m unittest discover -s tests`；返回项目根目录运行 `.venv\Scripts\python.exe check_frontend.py` 和 `.venv\Scripts\python.exe scripts/http_smoke.py`。HTTP 冒烟测试使用隔离的临时目录，不会写入真实 `workspace/outbox`。

## Phase 2 本地控制节点

- **分支**：先从 `rag-blueprint` 的输出端连接分支输入端；在分支的输入表单填 `condition=true` 或 `false`，再分别拉两条出线并指定路由。运行时只执行被选中的路径，另一条显示 `SKIPPED`。条件是显式布尔值，不执行表达式或用户代码。
- **循环**：在循环节点填写 `value`、`count`（1–5）和 `target_skill`（例如 `skill-doctor`）。目标 Skill 必须通过当前安检且恰有一个输入、一个输出；每轮把上轮输出传给下轮。循环是有界的重复调用，不允许图上形成循环依赖。
- **版本与依赖**：版本显示以磁盘 `skill.schema.json` 为准。只有健康 Skill 的当前版本可创建内容寻址的源码 ZIP 快照；下载前会复核 ZIP 的 SHA-256。旧版本记录保留并标为未验证，不证明对应源码仍存在。快照会拒绝 `.env*`、私钥等明显凭证文件，但不能保证识别所有内嵌秘密；创建前仍应人工检查源码。依赖检查只比较 Schema 声明中的精确 `package==version`，未探测本机安装包、GPU 驱动或模型兼容性。
- **导出与审计**：YAML 是拓扑与版本快照，不含运行输入或密钥；API 描述是本地原型接口说明，不是可独立部署的服务。新运行/部分操作事件写入本地哈希链，运行摘要与链中的内容哈希关联；旧审计文件原样保留，但在 UI 和导出中隐藏旧详情并标记未验证。哈希链可发现内容修改与重排，**不能**防止有文件写权限者删除链尾或重建整链；没有外部时间戳或身份签名。页面的添加/移除/连线记录由客户端上报，没有用户身份保证，不是完整操作审计。审计报告不是第三方合规认证。

运行器使用临时目录中的独立子进程并有超时，但 Windows 下没有可靠的内存限制或 OS 级隔离。请勿把未知恶意 Skill 当作已经安全隔离。`skill-doctor` 的五项结果来自本地检查，`skill-report` 只格式化其结果；其他示例能力仍可能是 mock，且没有真实模型推理。`Signed=PASS` 仅表示本地 SHA-256 清单一致，不表示真实数字签名；API 同时返回 `signature_verified=false` 和 `origin_verified=false`。提交证据要求见 `SUBMISSION-EVIDENCE.md`。
