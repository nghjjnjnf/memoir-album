# 测试版核心功能与验收追踪表

状态：执行基线  
版本：MVP 0.7  
更新日期：2026-08-30

## 1. 使用规则

此文档是当前测试版的唯一验收入口。后续每完成一个功能，必须同时完成四件事：

1. 实现代码；
2. 增加或更新自动测试；
3. 对照原始产品文档进行人工核对；
4. 在本表中填写实现位置、测试证据和最终状态。

状态定义：

- `未开始`：尚无可运行实现；
- `部分完成`：已有演示实现，但未满足全部验收标准；
- `已完成`：代码与自动测试通过，并已对照来源文档；
- `延后`：本轮经过明确决策不进入测试版。

## 2. 本轮范围

当前测试版验证：照片上传、文字讲述、多 Agent 访谈、事实记忆、章节生成、写作方法检索、审校、版本、确认、新照片关系、私密分享和本地数据权利。

根据当前决定，以下功能延后，不得因为原始文档曾列为首版而误报为已完成：

| 编号 | 延后功能 | 恢复条件 |
|---|---|---|
| D-01 | 录音、ASR 与语音修改 | 文字闭环和 Prompt 质量稳定后 |
| D-02 | TTS 朗读确认 | 选定中文语音供应商后 |
| D-04 | 微信小程序页面 | 本地 REST API 和状态机稳定后 |
| D-05 | 完整成书、书名与封面 | 累积多章并验证全书组织后 |
| D-06 | 正式亲友协作与互联网公开访问 | 完成身份、权限和部署方案后 |

## 3. 核心功能追踪矩阵

| ID | 核心功能 | 验收标准 | 来源文档 | 当前状态 | 实现证据 | 测试证据 |
|---|---|---|---|---|---|---|
| F-01 | 创建自传与选择人称 | 可创建项目；可设置第一/第三人称；新版本使用当前设置 | `agent-requirements-v1.md` 3.5；`rag-writing-design.md` 7 | 已完成 | `app/main.py`、`app/orchestrator.py` | `test_mvp_flow.py`、`test_core_features.py::test_project_perspective_and_health` |
| F-02 | 照片上传与本地保存 | 只接受允许格式；限制大小；原照片本地保存；不发送给文本模型 | `agent-requirements-v1.md` 3.1；`local-mvp-implementation-plan.md` 2 | 已完成 | `app/orchestrator.py::save_photo` | `test_core_features.py::test_photo_validation_and_local_storage` |
| F-03 | 文字讲述会话 | 上传照片后创建可恢复会话；回答和问题均保存；至少回答一次才可成稿 | 本轮语音替代决策；`agent-requirements-v1.md` 3.2 | 已完成 | `start_interview`、`accept_reply` | `test_mvp_flow.py` |
| F-04 | 自然晚辈式回应与未解线索追踪 | 以两个人友好交谈为目标；Interview Agent 根据最新回答、最近对话、人物事件记忆与未解线索一次生成完整回应；篇幅按本轮信息量弹性建议，不设150字硬门槛，不用本地套话补字；拦截记录员口吻、无依据内容和多问题；优先追踪动机、因果和含糊路线 | `agent-requirements-v1.md` 4；`local-mvp-implementation-plan.md` 6.3 | 已完成 | `INTERVIEW_SYSTEM`、`_interview_reply_length_guidance`、`TASKLIKE_LISTENING_PATTERNS`、`normalize_interview_output` | `test_interview_uses_adaptive_length_without_opening_detector_or_padding_library`、`test_interview_length_guidance_does_not_add_story_content`、`test_interview_tracks_unexplained_motivation_then_ambiguous_route` |
| F-05 | 结构化事实与纠错 | 事实关联用户回答；可更正；可标记不入书；旧事实保留撤回状态；章节只读取可入书事实 | `agent-requirements-v1.md` 3.3～3.4；实施计划阶段 E | 已完成 | `update_fact`、`_usable_facts`、事实卡界面 | `test_fact_correction_and_exclusion` |
| F-06 | 事实约束的完整章节写作 | 一张照片可生成一章；支持两种人称；章节同时读取事实卡、完整用户原话和受限视觉证据；按访谈深度设置约 450～1600 字动态目标，丰富材料默认不少于 900 字；短稿自动重写一次；硬事实不变，允许合理文学推断；保存事实、方法卡、视觉证据和推断快照 | `agent-requirements-v1.md` 3.5；`model-and-chapter-design.md` 4；`literary-autobiography-writing-spec.md` | 已完成 | `Chapter Agent`、`user_transcript`、`visual_evidence`、`literary_inferences`、`target_length_chars`、`source_snapshot_json` | `test_chapter_length_target_grows_with_interview_depth`、`test_third_person_is_used_by_new_chapter`、`test_chapter_uses_retrieval_snapshot` |
| F-07 | 模型审校与确定性事实门禁 | 生成后必须审校；人物、时间、地点、关系、核心事件、结果和直接引语受硬约束；具体外部细节必须来自口述、事实或照片可见证据；允许不冲突的氛围、心理、主题和象征；审校报告随版本保存；审校失败不能确认 | `local-mvp-implementation-plan.md` 6.5、阶段 F；`literary-autobiography-writing-spec.md` | 已完成 | `Review Agent`、`CHAPTER_HARD_RISK_MARKERS`、`enforce_grounding_review`、`chapter_versions.review_json` | `test_deterministic_review_rejects_unsupported_scene_padding`、`test_deterministic_review_accepts_visible_scene_evidence`、`test_mvp_flow.py` |
| F-08 | 候选修改、不可变版本与用户仲裁 | 修改先生成独立候选稿，不移动当前版本指针；用户可继续调整、采用或放弃；只有采用后才创建不可变章节版本；确认与采用分离，已分享版本不被静默覆盖 | `agent-requirements-v1.md` 3.5；`model-and-chapter-design.md` 4G | 已完成 | `chapter_revision_candidates`、`revise_chapter`、`adopt_revision_candidate`、`discard_revision_candidate`、候选稿对比界面 | `test_complete_photo_to_confirmed_chapter_flow`、`test_revision_candidate_can_be_discarded_without_changing_chapter` |
| F-09 | 新照片章节关系 | Agent 给出独立、附图或合并建议及理由；用户最终决定；合并生成新版本并保留旧版 | `agent-requirements-v1.md` 3.5.1；实施计划阶段 H | 已完成 | `Relation Advisor`、`apply_relation_choice`、关系选择界面 | `test_second_photo_merge_preserves_versions` |
| F-10 | 最小写作 RAG | 从独立方法卡库检索 2～4 张；只提供结构方法；记录检索快照；不把他人人生事实写入 | `rag-writing-design.md` 2.2、5、6；实施计划阶段 G | 已完成 | `knowledge/writing-methods`、`writing_methods.py` | `test_chapter_uses_retrieval_snapshot` |
| F-11 | 单章私密分享与撤回 | 仅已确认版本可分享；默认不公开；链接绑定具体版本；可撤回；分享页不暴露原图下载入口 | `mvp-product-design.md` 分享与协作；`agent-requirements-v1.md` 3.8 | 已完成 | `share_links`、`/share/{token}`、分享与撤回界面 | `test_confirmed_version_share_and_revoke` |
| F-12 | 下一段故事引导 | 确认后给出可忽略的下一张照片或人物建议，不制造任务压力 | `agent-requirements-v1.md` 3.7 | 已完成 | `next_story_suggestion`、章节界面 | `test_confirmed_version_share_and_revoke` |
| F-13 | 本地导出与删除 | 可导出项目 JSON；删除项目级联数据库记录、照片、模型审计和分享令牌 | `product-discovery.md` 隐私；`agent-requirements-v1.md` 5 | 已完成 | `export_project`、`delete_project`、项目操作界面 | `test_project_export_and_delete` |
| F-14 | 多 Agent 协调与审计 | Agent 不直写核心表；协调器唯一提交；记录模型、输入、输出、错误；失败不丢用户数据 | `local-mvp-implementation-plan.md` 6～8 | 已完成 | `app/llm.py`、`app/orchestrator.py`、`model_runs` | `test_core_features.py::test_model_runs_are_audited` |
| F-15 | 本地可用界面与小程序迁移边界 | 浏览器可完成核心闭环；API 与页面分离；健康接口明确语音/视觉状态 | `local-mvp-implementation-plan.md` 4、13 | 已完成 | `app/static/`、REST API | HTTP 首页检查、`test_project_perspective_and_health` |
| F-16 | 个人记忆时间线与跨访谈回填 | 上传顺序不决定人生顺序；事实归属稳定人生事件；后续访谈可补充旧事件；旧章节只提示新材料且不静默覆盖；章节目录按事件时间排列 | 本轮时间线设计讨论 | 已完成 | `timeline_events`、`chapter_events`、`_fact_target_event`、时间线界面 | `test_timeline_orders_by_life_time_and_backfills_old_event_without_overwriting_chapter` |
| F-17 | 上传即识图与候选确认 | 后台提取并保存EXIF、人数、场景、物品、OCR及时间/地点候选；前端不展示检测报告；访谈每轮最多使用一个未确认线索自然询问；只有用户原话进入事实；视觉失败不阻断讲述；可重新识图 | 本轮视觉模型决策 | 已完成 | `photo_observations`、`app/vision.py`、后台视觉上下文 | `test_vision_opening_labels_image_information_as_unconfirmed_candidates`、真实视觉API端到端测试 |
| F-18 | 默认文学版、低门槛修订与记忆隔离 | 文风修改只作用于候选正文，不污染人物、事件和事实记忆；“细节不准确”解析为事实变更提案，界面展示原记忆与新记忆；采用时追加新事实、撤回旧事实并同步明确年份到时间线，放弃时章节和记忆均不变 | `literary-autobiography-writing-spec.md` 2、10、11 | 已完成 | `CHAPTER_SYSTEM`、`_memory_correction_plan`、append-only `memory_facts`、候选稿记忆影响说明 | `test_adopting_fact_correction_updates_memory_append_only`；完整回归50项通过；周桂兰真实 DeepSeek 候选稿生成后放弃，当前版本指针保持不变 |
| F-19 | 整书人物与暗线关联 | 整书导演统一人物和2～4条暗线；逐章关联改写；事实链接重建正文来源；至少半数章节实质变化且至少3条跨章事实；整书版本不可变、失败不覆盖确认版 | 本轮整书关联设计讨论 | 部分完成 | `book_editions`、`Book Director`、`Chapter Reweaver`、`Cross-chapter Link Editor`、`Chapter Fact Linker`、`Book Continuity Reviewer`、整书关联版界面 | 26项自动测试通过；周桂兰第6版真实 DeepSeek：6/6章变化、17条跨章事实、4条暗线、0个模型审校失败；人工终检仍发现细节漂移，故未确认并导出人工终检版 |
| F-20 | 共享人生记忆与阈值压缩 | 所有 Agent 共享版本化人物、事件和关键事实快照；上下文超过可配置阈值后压缩早期对话；保留摘要、最近对话和未解线索；原始 turns 不删除；正文不得反向成为事实 | `context-memory-and-compaction.md` | 已完成 | `app/context_memory.py`、`life_context_snapshots`、`conversation_compactions`、`LLMGateway.generate_json`、`_prepare_session_context` | `test_shared_life_snapshot_is_injected_into_agent_context`、`test_context_compression_triggers_at_token_boundary_without_deleting_turns`；完整回归 33 项通过 |
| F-21 | 事件级时间解析与归属 | 照片说明和用户回复走统一解析链路；区分照片时间、支持时间、重访、其他时期和明确更正；人生阶段也可锁定；关联事件不得覆盖主时间；相对时间绑定讲述时间；旧数据可幂等回填 | `event-time-memory-design.md` | 已完成 | `event_mentions`、`event_relations`、`time_locked`、`_parse_event_mentions`、`_persist_event_mentions`、`reconcile_temporal_evidence` | `test_photo_note_life_stage_is_locked_before_later_revisit`、`test_one_reply_can_contain_capture_time_and_later_event`、`test_explicit_time_correction_replaces_locked_capture_time`；完整回归 36 项通过 |
| F-22 | 归档标题与文学标题分层 | 事件名称继续用于事实归档和编辑；存在章节时，时间线优先展示章节文学标题；接口同时返回 `archival_title`、`display_title` 和 `title_source`，避免展示层误用数据库标签 | 本轮标题文学性讨论 | 已完成 | `timeline_detail`、`renderTimeline` | `test_timeline_uses_literary_chapter_title_without_overwriting_archive_title`；完整回归 37 项通过 |
| F-23 | 上传即生成文学标题 | 标题可选填；留空时识图完成后由 Title Agent 综合照片观察、用户说明和共享人生记忆生成标题；视觉猜测不得升级为事实；保存标题版本、来源、阶段及记忆快照；展示优先级为用户命名 > 章节标题 > 自动标题 > 归档名称；用户更正后不得被模型覆盖 | 本轮上传标题生成讨论；`literary-title-generation.md` | 已完成 | `generate_photo_title`、`generate_initial_event_title`、`event_title_versions`、上传标题输入框、`timeline_detail` | `test_blank_photo_title_is_generated_from_vision_note_and_shared_memory`、`test_user_photo_title_has_priority_over_generated_and_chapter_titles`、`test_corrected_event_name_becomes_user_display_title`；完整回归 40 项通过 |
| F-24 | 用户输入驱动首问去重 | 用户标题或说明中已给出的时间、地点进入事件上下文；首轮访谈优先承接用户信息，不再用识图模板重复确认；重新加载零轮会话不新增或覆盖首句；视觉候选与用户信息冲突时以用户为准 | 本轮“上海交大”首问问题 | 已完成 | `user_context_slots`、`opening_from_observation`、`start_interview`、`session_detail` | `test_user_title_context_prevents_reasking_known_time_and_place`；完整回归 41 项通过 |
| F-25 | 逐照片查看访谈记录 | 每个时间线故事显示“查看访谈记录”或“继续聊这张照片”；点击后恢复该照片绑定的 session、全部历史问答和事实；已成稿访谈以查看模式展示，照片资源已删除时仍可阅读文字 | 本轮历史对话查看需求 | 已完成 | `renderTimeline`、`renderSession`、`GET /api/sessions/{session_id}` | `test_each_timeline_story_can_reopen_its_saved_interview`；完整回归 43 项通过 |
| F-26 | 内容驱动的自然访谈表达 | 不检测或轮换回复开头，不向模型提供固定开场句库；LLM 根据用户最新回答、对话上下文、事实与访谈目标自行决定如何起笔和组织整段；程序只负责事实、安全、边界和单问题门禁；门禁过滤后几乎无正文时才由 LLM 重新生成 | 本轮“不要检查重复开头，由 LLM 自然生成”决策 | 已完成 | `PERSONA`、`INTERVIEW_SYSTEM`、`INTERVIEW_REPLY_EDITOR_SYSTEM`、Interview Agent 温度配置 | `test_interview_uses_adaptive_length_without_opening_detector_or_padding_library`；真实 DeepSeek 动态回复测试；完整回归 43 项通过 |
| F-27 | GPT 式语音访谈输入 | 输入框提供中文语音按钮与向上箭头发送；用户消息先乐观进入对话区，模型等待期间箭头旋转且禁止重复提交；失败时恢复原文字；Edge/Chrome 不支持时给出明确提示 | 本轮访谈输入体验需求 | 已完成 | Browser Web Speech API、`setupVoiceInput`、`setReplyThinking`、乐观消息渲染 | `test_interview_composer_supports_voice_arrow_and_optimistic_message`；浏览器检查语音、箭头、等待态与零控制台错误；完整回归 50 项通过 |

## 4. 完成门禁

功能只有同时满足以下条件才能从“部分完成”改为“已完成”：

- 对应 API 或页面可以实际操作；
- 正常路径自动测试通过；
- 至少一个关键失败路径被测试；
- 数据库中能追溯输入、输出或版本；
- 与来源文档不存在未说明的偏差；
- 不依赖已经暴露的 API 密钥；
- 不将 Mock 模式的效果误报为真实模型效果。

## 5. 每轮回归命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check app\static\app.js
python -m compileall app
```

每次交付必须记录：通过的测试数量、仍为“部分完成”的 ID、延后项，以及是否使用了真实 DeepSeek 调用。

## 6. MVP 0.7 回归记录

- 自动测试：`50 passed`；Demo 数据预检：`26/26`；
- 前端 JavaScript 语法检查：通过；
- Python 编译检查：通过；
- F-01～F-18、F-20～F-27：全部达到当前测试范围的完成门禁；
- 部分完成项：F-19（整书关联已可用，但全自动出版仍需人工终检）；
- 明确延后：D-01、D-02、D-04～D-06；
- 自动回归模型模式：Mock；本地交互服务模式：DeepSeek；
- 真实章节回归：周桂兰案例生成第 3 版《半毫米的前排》，正文 1028 字，审校通过；
- 本轮时间线自动测试未调用真实 DeepSeek，避免把供应商波动混入确定性归档测试；
- 安全检查：API 密钥仅保存在本地且被 Git 忽略的 `.env` 中，前端和仓库源码不包含密钥。
