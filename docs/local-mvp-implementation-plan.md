# 人生故事 Agent：本地 MVP 实施方案

状态：已确定采用本地功能验证优先  
整理日期：2026-08-23

## 1. 实施目标

先在电脑本地完成并验证以下闭环：

```text
上传一张照片
→ 分段语音讲述
→ 语音转写与照片理解
→ Agent 进行 2～5 轮自然追问
→ 生成第一或第三人称章节
→ 事实、风格和隐私审校
→ 朗读、修改与确认
→ 保存本地章节版本
```

核心接口、数据模型和 Agent 输入输出从第一天就保持可迁移。等本地闭环稳定后，再将本地 Web 界面替换为微信小程序，并将本地媒体存储替换为云对象存储。

## 2. “本地测试”的含义

本地测试指：

- 前端、后端、数据库和媒体文件运行或保存在开发电脑；
- 用户通过本地浏览器访问测试界面；
- 照片、录音、事实和章节默认不上传到公开业务环境；
- 模型服务可以由本地后端调用云端 API；
- 所有 API 密钥只放在本地 `.env` 中，不进入前端、日志或 Git；
- 模型供应商通过统一适配层接入，后续可替换为国内云服务或本地模型。

本地 MVP 不等于必须让所有模型完全离线。是否增加本地离线 ASR 或本地大模型，可在端到端闭环跑通后根据隐私、成本和效果再决定。

## 3. MVP 功能范围

### 必须完成

- 创建一个本地自传项目；
- 设置第一人称或第三人称；
- 上传一张照片；
- 录制或上传一段语音；
- 保存原始照片和原始录音；
- ASR 转写；
- 照片线索提取；
- 多 Agent 访谈与记忆整理；
- 每次只提出一个问题；
- 2～5 轮后生成一章；
- 章节事实和风格审校；
- TTS 分段朗读；
- 文字或语音修改；
- 用户确认；
- 保存不可变章节版本；
- 上传新照片后建议加入已有章节、合并或另起一章。

### 本地 MVP 暂不完成

- 微信登录；
- 微信分享卡片；
- 公开互联网访问；
- 正式家庭协作者权限；
- 多人同时编辑；
- 完整自传排版与印刷；
- 封面生成；
- 公开社区；
- 人脸身份识别；
- 声音克隆；
- Kubernetes 和复杂微服务。

## 4. 本地技术架构

```text
本地 React Web 测试台
        ↓ HTTP / SSE
FastAPI 业务后端
        ↓
确定性 Workflow Orchestrator
        ├─ ASR Provider
        ├─ Vision Provider
        ├─ LLM Provider
        ├─ TTS Provider
        ├─ 多 Agent 逻辑模块
        └─ RAG 检索
        ↓
PostgreSQL + pgvector
        ↓
本地 data/media 文件目录
```

### 推荐技术栈

- 本地前端：React + Vite + TypeScript；
- 后端：Python + FastAPI；
- 数据校验：Pydantic；
- ORM 与迁移：SQLAlchemy + Alembic；
- 数据库：Docker 中运行 PostgreSQL + pgvector；
- 本地媒体：受控的 `data/media/` 目录；
- 初期并行：Python `asyncio`；
- 任务量增加后：Redis + RQ 或 Celery；
- 测试：pytest、前端组件测试及端到端测试；
- 本地启动：Docker Compose + 开发脚本。

选择 React 本地测试台，是为了快速观察上传、录音、转写、Agent 中间结果和章节版本；后续迁移到小程序时不要求复用页面代码，重点复用后端 API、状态机、Agent、数据库和测试集。

## 5. 推荐项目结构

```text
life-story-agent/
├── apps/
│   ├── local-web/               # 本地 React 测试台
│   └── miniprogram/             # 后续微信小程序
├── services/
│   ├── api/                     # FastAPI API
│   └── worker/                  # 后续异步任务 Worker
├── packages/
│   ├── agent-contracts/         # Agent JSON/Pydantic 契约
│   ├── model-gateway/           # ASR/Vision/LLM/TTS 适配
│   └── prompts/                 # 版本化 Prompt
├── knowledge/
│   └── writing-methods/         # 写作方法卡
├── evals/
│   ├── fixtures/                # 经授权、匿名化测试材料
│   ├── audio/
│   ├── interviews/
│   └── chapters/
├── data/
│   └── media/                   # 本地照片、录音、TTS
├── infra/
│   └── docker-compose.yml
├── docs/
└── .env.example
```

真实 `.env`、`data/media/`、原始测试照片和录音必须进入 `.gitignore`。

## 6. 多 Agent 的 MVP 划分

本地首版实现四个核心 Agent，加一个非 LLM 协调器。

### 6.1 Workflow Orchestrator

确定性程序，不使用自由决策的大模型。负责：

- 工作流状态；
- Agent 调用顺序；
- 并行任务汇合；
- 超时、重试和幂等；
- 模型调用预算；
- 用户确认门禁；
- 唯一数据提交入口。

### 6.2 Memory Agent

负责：

- 汇总照片观察和语音转写；
- 抽取人物、时间、地点、事件、感受和原话；
- 标记已确认、不确定、冲突和敏感信息；
- 更新个人语言档案；
- 每个事实关联证据来源。

### 6.3 Interview Agent

负责：

- 维持“知心小妹妹”角色；
- 每次只问一个问题；
- 选择最有价值且不冒犯的问题；
- 接受“记不清”“不想说”“以后再说”；
- 判断继续提问还是建议成稿。

只有这个 Agent 可以产生面向老人的访谈话语。

### 6.4 Chapter Agent

负责：

- 选择第一或第三人称；
- 判断章节类型和叙事结构；
- 检索个人事实和写作方法卡；
- 生成提纲、标题和正文；
- 上传新照片时提出补充、合并或另起一章的建议；
- 输出正文段落与事实 ID 的映射。

### 6.5 Review Agent

负责：

- 事实来源检查；
- 人称一致性；
- 是否像老人本人；
- 是否过度煽情；
- 隐私与第三人风险；
- 与写作范例的异常相似性。

审校未通过时允许保存草稿，但禁止标记为已确认。

## 7. Agent 数据契约

Agent 不能直接修改数据库。它们读取同一个快照并返回结构化 Proposal。

### 核心快照

```json
{
  "snapshot_id": "snapshot_001",
  "project_id": "project_001",
  "session_id": "session_001",
  "chapter_id": null,
  "base_revision": 1,
  "narrative_person": "first",
  "recent_turns": [],
  "confirmed_facts": [],
  "uncertain_facts": [],
  "voice_profile": {}
}
```

### Agent Proposal

```json
{
  "agent_role": "memory_agent",
  "input_snapshot_id": "snapshot_001",
  "proposed_changes": [],
  "evidence_refs": [],
  "warnings": [],
  "requires_user_confirmation": true
}
```

所有结构化输出都必须经过 Pydantic/JSON Schema 校验。OpenAI Responses API 当前支持文本、图片、文件输入，结构化输出、函数调用和并行工具调用；如果使用 OpenAI 路线，可利用这些能力，但业务契约不得依赖单一模型供应商。

参考：https://developers.openai.com/api/reference/cli/resources/responses/methods/create

## 8. 工作流状态

```text
project_created
→ media_uploaded
→ understanding_media
→ interviewing
→ ready_to_draft
→ drafting
→ reviewing
→ pending_confirmation
→ confirmed
```

每个状态允许的操作必须明确。只有 Orchestrator 可以改变状态。

## 9. 并行执行设计

### 照片和首次语音上传后

并行执行：

- ASR 转写；
- 照片观察；
- 隐私初筛；
- 个人历史事实检索。

完成后统一进入 Memory Agent，再由 Interview Agent 生成一个问题。

### 章节生成前

并行执行：

- 个人事实检索；
- 写作方法检索；
- 个人语言档案读取；
- 相关旧章节检索。

完成后由 Chapter Agent 生成提纲和正文。

### 章节生成后

本地首版可先使用一个综合 Review Agent。测试证明有必要后，再将事实、风格、隐私和相似性拆成并行 Reviewer。

## 10. 核心数据表

第一版至少实现：

```text
users_local
autobiography_projects
media_assets
interview_sessions
interview_turns
transcript_versions
memory_facts
chapters
chapter_versions
model_runs
writing_methods
```

### memory_facts 关键字段

- `fact_id`；
- `project_id`；
- `fact_type`；
- `value`；
- `status`；
- `source_refs`；
- `speaker`；
- `sensitivity`；
- `visibility`；
- `supersedes`；
- `created_at`。

事实状态至少区分：

```text
proposed
asserted_by_user
confirmed_by_user
disputed
retracted
```

模型生成的章节不能反向成为事实来源。

## 11. 分阶段实施顺序

### 阶段 A：准备测试材料与验收标准

任务：

1. 获取 5～10 位目标用户的知情授权；
2. 准备 20～30 张照片和对应录音；
3. 标注事实、不确定信息、敏感内容和理想问题；
4. 准备第一/第三人称参考成稿；
5. 建立模型、Prompt 和版本记录格式。

完成标准：至少有一组可以贯穿全部功能的匿名化测试样本。

### 阶段 B：模型 Gateway 与命令行技术验证

任务：

1. 定义 `transcribe_audio()`；
2. 定义 `analyze_photo()`；
3. 定义 `generate_structured()`；
4. 定义 `synthesize_speech()`；
5. 为每个 Provider 提供统一返回格式；
6. 用相同录音和照片比较候选模型。

完成标准：一条命令可以对一张照片和一段录音生成转写、视觉线索和初始事实卡。

### 阶段 C：后端骨架与持久化

任务：

1. 创建 FastAPI 工程；
2. 创建 PostgreSQL、Alembic 迁移；
3. 创建本地媒体存储适配器；
4. 实现项目、媒体、访谈、事实和章节 API；
5. 实现模型调用日志；
6. 实现不可变原始材料和章节版本。

完成标准：上传材料和全部派生结果可以保存、查询和恢复。

### 阶段 D：单照片纵向切片

任务：

1. 上传一张照片；
2. 上传或录制一段语音；
3. 并行执行 ASR 和照片分析；
4. 提出一个问题；
5. 接收一次补充回答；
6. 生成一篇章节；
7. 在本地页面展示。

此阶段暂不做 RAG、合并和 TTS。

完成标准：端到端流程可重复运行，失败不丢失用户材料。

### 阶段 E：多 Agent 访谈闭环

任务：

1. 实现 Agent 契约；
2. 实现 Memory Agent；
3. 实现 Interview Agent；
4. 实现确定性 Orchestrator；
5. 支持 2～5 轮访谈；
6. 支持暂停、继续和成稿判断；
7. 对人名、地名提供纠错入口。

完成标准：每轮只问一个问题；拒绝和记不清得到尊重；不会因刷新或中断丢失进度。

### 阶段 F：章节、审校与朗读

任务：

1. 实现第一/第三人称；
2. 实现 Chapter Agent；
3. 实现 Review Agent；
4. 保存段落与事实映射；
5. 实现文字修改；
6. 实现 TTS 分段朗读；
7. 实现语音修改；
8. 实现用户确认和章节版本。

完成标准：确认版本不包含无来源具体事实，人称一致，并可以完整朗读和修改。

### 阶段 G：最小 RAG

任务：

1. 整理 30～50 张写作方法卡；
2. 标记人称、章节类型、语气和技法；
3. 通过 pgvector 建立索引；
4. 每章只检索 2～4 张方法卡；
5. 记录检索快照；
6. 增加范例相似性检查。

完成标准：RAG 提升可读性或自传感，同时不降低事实忠实度和“像本人”的程度。

### 阶段 H：新照片和章节关系

任务：

1. 上传第二张照片；
2. 检索相关章节候选；
3. 生成加入、合并或另起一章的建议；
4. 用户作最终决定；
5. 合并创建新章节版本；
6. 原确认版本保持不变。

完成标准：Agent 能说明建议理由，不擅自合并。

### 阶段 I：真实本地试用

任务：

1. 让 5～10 位老人完成完整流程；
2. 记录操作困难、反感点和疲劳点；
3. 比较 ASR、Prompt 和模型版本；
4. 检查失败恢复；
5. 统计延迟、调用量和成本；
6. 修复最高频问题。

完成标准：真实用户能够完成“一张照片到一章”的闭环，且愿意继续上传下一张照片。

## 12. MVP 验收清单

- 一张照片能够生成一章；
- 支持第一和第三人称；
- 每轮只问一个问题；
- Agent 使用知心小妹妹人格但不幼稚、不冒犯；
- 老人可以说记不清、跳过或暂停；
- 人名、地名和年代可以纠正；
- 具体事实都能追溯到证据；
- 模型推断不会自动进入正文；
- 章节可以朗读、修改和确认；
- 确认版本不可被静默覆盖；
- 新照片可建议合并或另起一章；
- RAG 不会向正文注入他人的人生细节；
- 任意模型调用失败不会丢失原始照片、录音或回答；
- 模型、Prompt、输入快照和输出可以追溯；
- 本地删除可以清除照片、录音、转写、向量和 TTS 文件。

## 13. 从本地迁移到微信小程序

本地 MVP 稳定后，迁移内容如下：

| 本地实现 | 小程序实现 |
|---|---|
| 本地测试用户 | 微信登录与 `openid` 映射 |
| React 本地页面 | 微信小程序页面 |
| 浏览器录音 | 小程序录音 API |
| 本地文件目录 | COS/OSS 对象存储 |
| 本地章节预览 | 小程序章节页 |
| 本地分享预览 | 微信分享卡片及权限链接 |
| 本地单用户 | 老人和家庭协作者权限 |
| 本地 API 地址 | HTTPS 正式域名 |

以下部分应保持不变：

- FastAPI 业务 API；
- Agent 契约；
- Workflow Orchestrator；
- 模型 Gateway；
- 事实和章节数据模型；
- RAG；
- Prompt 与评测集；
- 版本、审校和隐私规则。

## 14. 立即执行的第一批任务

建议严格按顺序开始：

1. 创建项目代码目录和基础 Git 配置；
2. 建立 `.env.example` 和敏感文件忽略规则；
3. 创建 Docker Compose PostgreSQL + pgvector；
4. 创建 FastAPI 健康检查和数据库迁移；
5. 定义 `PhotoObservation`、`MemoryFact`、`InterviewDecision`、`ChapterDraft` 和 `ReviewReport`；
6. 接入一个 ASR Provider；
7. 接入一个支持图片的 LLM Provider；
8. 编写单张照片和单段录音的命令行流程；
9. 建立第一批真实或匿名化测试夹具；
10. 通过测试后再开始 React 本地界面。

第一里程碑不是“页面看起来完整”，而是：

> 在本地用一条命令输入照片和录音，稳定得到可追溯的照片观察、语音转写、事实卡、一个自然问题和一篇章节草稿。
