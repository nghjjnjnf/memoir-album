const state = {
  projects: [],
  project: null,
  session: null,
  chapter: null,
  bookEdition: null,
  photoToDelete: null,
  peopleCatalog: null,
  revisionCandidate: null,
  revisionMode: "auto",
  revisionBaseCandidateId: null,
  workspacePage: "story",
};

const $ = (id) => document.getElementById(id);
const pageParams = new URLSearchParams(window.location.search);
const initialProjectId = pageParams.get("project");
const demoMode = pageParams.get("demo") === "1";
const workspacePages = new Set(["story", "chapter", "book"]);
if (workspacePages.has(pageParams.get("view"))) state.workspacePage = pageParams.get("view");

function projectDisplayTitle(project) {
  if (demoMode && project?.id === initialProjectId) return "周桂兰的人生自传";
  return project?.title || "";
}

function showWorkspacePage(page, { scroll = true, updateUrl = true } = {}) {
  const targetPage = workspacePages.has(page) ? page : "story";
  state.workspacePage = targetPage;
  document.querySelectorAll("[data-workspace-page]").forEach((element) => {
    element.classList.toggle("workspace-page-hidden", element.dataset.workspacePage !== targetPage);
  });
  document.querySelectorAll("[data-workspace-view]").forEach((button) => {
    const active = button.dataset.workspaceView === targetPage;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });

  if (targetPage === "chapter") {
    $("chapterPageEmpty").classList.toggle("hidden", Boolean(state.chapter));
    $("chapterPanel").classList.toggle("hidden", !state.chapter);
  }
  if (targetPage === "book") {
    $("bookPageEmpty").classList.toggle("hidden", Boolean(state.bookEdition));
    $("bookEditionPanel").classList.toggle("hidden", !state.bookEdition);
  }
  if (updateUrl && state.project) {
    const url = new URL(window.location.href);
    url.searchParams.set("project", state.project.id);
    url.searchParams.set("view", targetPage);
    window.history.replaceState({}, "", url);
  }
  if (scroll) $("workspacePageNav").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function toast(message, isError = false) {
  const element = $("toast");
  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.remove("hidden");
  window.setTimeout(() => element.classList.add("hidden"), 3500);
}

function setBusy(button, busy, label = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.original = button.textContent;
    button.textContent = label;
    button.disabled = true;
    button.classList.add("is-busy");
  } else {
    button.textContent = button.dataset.original || button.textContent;
    button.disabled = false;
    button.classList.remove("is-busy");
  }
}

let replyThinking = false;
let voiceListening = false;
let voiceRecognition = null;
let voiceBaseText = "";
let voiceFinalText = "";

function appendChatMessage(role, content, { pending = false } = {}) {
  const chat = $("chatMessages");
  const message = document.createElement("div");
  message.className = `message ${role}${pending ? " pending" : ""}`;
  message.textContent = content;
  message.style.setProperty("--message-index", Math.min(chat.children.length, 8));
  chat.appendChild(message);
  chat.scrollTop = chat.scrollHeight;
  return message;
}

function resizeReplyInput() {
  const input = $("replyText");
  input.style.height = "auto";
  input.style.height = `${Math.min(Math.max(input.scrollHeight, 54), 160)}px`;
}

function updateReplySendState() {
  $("replySendButton").disabled = replyThinking || voiceListening || !$("replyText").value.trim();
}

function setReplyThinking(busy) {
  replyThinking = busy;
  const form = $("replyForm");
  const input = $("replyText");
  const send = $("replySendButton");
  const voice = $("voiceInputButton");
  form.setAttribute("aria-busy", busy ? "true" : "false");
  send.classList.toggle("is-thinking", busy);
  send.setAttribute("aria-label", busy ? "正在等待回复" : "发送讲述");
  input.disabled = busy;
  voice.disabled = busy;
  updateReplySendState();
}

function setVoiceListening(active, status = "") {
  voiceListening = active;
  const button = $("voiceInputButton");
  const input = $("replyText");
  button.classList.toggle("is-listening", active);
  button.setAttribute("aria-pressed", active ? "true" : "false");
  button.setAttribute("aria-label", active ? "停止语音输入" : "开始语音输入");
  input.readOnly = active;
  $("voiceInputStatus").textContent = status || (active ? "正在听，点一下结束" : "");
  updateReplySendState();
}

function setupVoiceInput() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const button = $("voiceInputButton");
  if (!Recognition) {
    button.classList.add("is-unsupported");
    button.title = "当前浏览器不支持语音识别，请使用新版 Edge 或 Chrome";
    button.addEventListener("click", () => toast("当前浏览器暂不支持语音输入，请使用新版 Edge 或 Chrome", true));
    return;
  }

  voiceRecognition = new Recognition();
  voiceRecognition.lang = "zh-CN";
  voiceRecognition.continuous = true;
  voiceRecognition.interimResults = true;

  voiceRecognition.addEventListener("start", () => setVoiceListening(true));
  voiceRecognition.addEventListener("result", (event) => {
    let interimText = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const transcript = event.results[index][0].transcript.trim();
      if (event.results[index].isFinal) voiceFinalText += `${transcript} `;
      else interimText += transcript;
    }
    $("replyText").value = [voiceBaseText, voiceFinalText.trim(), interimText]
      .filter(Boolean).join(" ").trim();
    resizeReplyInput();
  });
  voiceRecognition.addEventListener("end", () => setVoiceListening(false));
  voiceRecognition.addEventListener("error", (event) => {
    const messages = {
      "not-allowed": "没有获得麦克风权限，请在浏览器地址栏允许访问麦克风",
      "no-speech": "这次没有听清，您可以再试一次",
      network: "语音识别服务暂时不可用，请稍后再试",
    };
    setVoiceListening(false);
    toast(messages[event.error] || "语音输入没有成功，请再试一次", true);
  });

  button.addEventListener("click", () => {
    if (voiceListening) {
      voiceRecognition.stop();
      return;
    }
    voiceBaseText = $("replyText").value.trim();
    voiceFinalText = "";
    try {
      voiceRecognition.start();
    } catch (_) {
      toast("语音输入正在启动，请稍等一下", true);
    }
  });
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    if (demoMode) {
      $("systemStatus").textContent = "本地保存";
      $("systemStatus").title = "内容已安全保存在本机 · 展示模式";
      return;
    }
    $("systemStatus").textContent = health.llm_mode === "mock" ? "本地模式" : "已连接";
    $("systemStatus").title = health.llm_mode === "mock"
      ? "本地 Mock 模式 · 不发送文字"
      : `DeepSeek · ${health.model}${health.vision_enabled ? " · 视觉识图已开启" : ""}`;
  } catch (error) {
    $("systemStatus").textContent = "未连接";
    $("systemStatus").title = "服务未连接";
  }
}

async function loadProjects(selectId = null) {
  state.projects = await api("/api/projects");
  if (demoMode && initialProjectId) {
    state.projects = state.projects.filter((project) => project.id === initialProjectId);
  }
  renderProjectList();
  if (selectId) await selectProject(selectId);
}

function renderProjectList() {
  const list = $("projectList");
  if (!state.projects.length) {
    list.className = "item-list empty-text";
    list.textContent = "还没有项目";
    return;
  }
  list.className = "item-list";
  list.innerHTML = "";
  state.projects.forEach((project) => {
    const button = document.createElement("button");
    button.className = `project-item ${state.project?.id === project.id ? "active" : ""}`;
    const title = document.createElement("strong");
    title.textContent = projectDisplayTitle(project);
    button.append(title);
    button.addEventListener("click", () => selectProject(project.id));
    list.appendChild(button);
  });
}

async function selectProject(projectId) {
  state.project = await api(`/api/projects/${projectId}`);
  state.session = null;
  state.chapter = null;
  state.bookEdition = null;
  state.peopleCatalog = null;
  $("welcome").classList.add("hidden");
  $("projectWorkspace").classList.remove("hidden");
  $("workspacePageNav").classList.remove("hidden");
  $("activeProjectTitle").textContent = projectDisplayTitle(state.project);
  $("activePersonSelect").value = state.project.narrative_person;
  $("peopleCount").classList.add("hidden");
  $("interviewPanel").classList.add("hidden");
  $("chapterPanel").classList.add("hidden");
  $("bookEditionPanel").classList.add("hidden");
  $("chapterPageEmpty").classList.add("hidden");
  $("bookPageEmpty").classList.add("hidden");
  renderProjectList();
  renderTimeline(state.project.timeline || []);
  renderChapterList(state.project.chapters || []);
  $("weaveBookButton").disabled = (state.project.chapters || []).length < 1;
  $("weaveBookButton").title = $("weaveBookButton").disabled ? "先完成一篇照片故事，就能生成第一版" : "";
  const emptyBookButton = document.querySelector("[data-generate-book]");
  emptyBookButton.disabled = $("weaveBookButton").disabled;
  emptyBookButton.title = $("weaveBookButton").title;
  if (demoMode) {
    if ($("projectCreateDialog").open) $("projectCreateDialog").close();
    $("newProjectButton").classList.add("hidden");
    $("deleteProjectButton").classList.add("hidden");
  }

  // 完整自传是照片故事之上的独立版本，不会覆盖原始章节。
  const editions = await api(`/api/projects/${projectId}/autobiography-editions`);
  const preferredEdition = editions.find((edition) => edition.status === "confirmed") || editions[0];
  if (preferredEdition) {
    renderBookEdition(await api(`/api/autobiography-editions/${preferredEdition.id}`), false);
  }
  if (state.workspacePage === "chapter" && state.project.chapters?.length) {
    renderChapter(await api(`/api/chapters/${state.project.chapters[0].id}`), false);
  }
  showWorkspacePage(state.workspacePage, { scroll: false, updateUrl: true });
}

function timelineLabel(event) {
  if (event.start_year && event.end_year && event.end_year !== event.start_year) return `${event.start_year}—${event.end_year}`;
  if (event.start_year && event.sort_basis === "relative_date") return `约${event.start_year}年`;
  if (event.start_year) return `${event.start_year}年`;
  if (event.time_text) return event.time_text;
  return "时间待确认";
}

function renderTimeline(events) {
  const list = $("timelineList");
  if (!events.length) {
    list.className = "timeline-list empty-text";
    list.textContent = "还没有时间线记录。上传一张照片后，这里会出现第一个人生事件。";
    return;
  }
  list.className = "timeline-list";
  list.innerHTML = "";
  events.forEach((event) => {
    const item = document.createElement("article");
    item.className = "timeline-item";
    const marker = document.createElement("div");
    marker.className = "timeline-marker";
    marker.textContent = timelineLabel(event);
    const body = document.createElement("div");
    body.className = "timeline-body";
    const heading = document.createElement("div");
    heading.className = "timeline-heading";
    const title = document.createElement("h3");
    title.textContent = event.display_title || event.title;
    heading.appendChild(title);
    if (event.needs_chapter_refresh) {
      const notice = document.createElement("span");
      notice.className = "memory-update-tag";
      notice.textContent = "有新材料待补充";
      heading.appendChild(notice);
    }
    if (event.photo_removed) {
      const removed = document.createElement("span");
      removed.className = "photo-removed-tag";
      removed.textContent = "照片已移除 · 故事已保留";
      heading.appendChild(removed);
    }
    const meta = document.createElement("p");
    meta.className = "timeline-meta";
    meta.textContent = [event.time_text, event.location, `${event.facts?.length || 0} 条事实`, `${event.photos?.length || 0} 张照片`].filter(Boolean).join(" · ");
    const summary = document.createElement("p");
    summary.className = "timeline-summary";
    summary.textContent = event.display_summary || event.summary || "这段记忆还在整理中，可以继续通过照片和讲述补充。";
    const actions = document.createElement("div");
    actions.className = "timeline-actions";
    const conversation = document.createElement("button");
    conversation.className = "secondary small";
    conversation.type = "button";
    conversation.textContent = (event.chapters || []).length
      ? "查看访谈记录"
      : "继续聊这张照片";
    conversation.addEventListener("click", async () => {
      try {
        const session = await api(`/api/sessions/${event.primary_session_id}`);
        showWorkspacePage("story", { scroll: false });
        renderSession(session);
      } catch (error) {
        toast(error.message, true);
      }
    });
    const edit = document.createElement("button");
    edit.className = "secondary small";
    edit.type = "button";
    edit.textContent = "更正时间或事件名称";
    edit.addEventListener("click", () => editTimelineEvent(event));
    actions.append(conversation, edit);
    (event.chapters || []).forEach((chapter) => {
      const open = document.createElement("button");
      open.className = "text-button";
      open.type = "button";
      open.textContent = (event.chapters || []).length === 1
        ? "阅读完整章节"
        : `阅读《${chapter.title}》`;
      open.addEventListener("click", async () => renderChapter(await api(`/api/chapters/${chapter.id}`)));
      actions.appendChild(open);
    });
    const copy = document.createElement("div");
    copy.className = "timeline-copy";
    copy.append(heading, meta, summary, actions);

    const photos = document.createElement("div");
    photos.className = `timeline-photos ${event.photos?.length > 1 ? "has-many" : ""}`;
    (event.photos || []).slice(0, 3).forEach((photo, index) => {
      const frame = document.createElement("figure");
      frame.className = "timeline-photo-frame";
      const image = document.createElement("img");
      image.className = "timeline-photo";
      image.src = photo.media_url;
      image.alt = photo.note || `${event.title}的记忆照片`;
      image.loading = "lazy";
      image.decoding = "async";
      frame.appendChild(image);
      if (!demoMode) {
        const remove = document.createElement("button");
        remove.className = "photo-delete-button";
        remove.type = "button";
        remove.title = "删除这张照片";
        remove.setAttribute("aria-label", "删除这张照片");
        remove.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5"/></svg>';
        remove.addEventListener("click", (clickEvent) => {
          clickEvent.stopPropagation();
          openPhotoDeleteDialog(photo);
        });
        frame.appendChild(remove);
      }
      if (index === 0 && event.photos.length > 1) {
        const count = document.createElement("figcaption");
        count.textContent = `共 ${event.photos.length} 张`;
        frame.appendChild(count);
      }
      photos.appendChild(frame);
    });

    const content = document.createElement("div");
    content.className = "timeline-content";
    if (event.photos?.length) content.appendChild(photos);
    content.appendChild(copy);
    body.appendChild(content);
    item.append(marker, body);
    list.appendChild(item);
  });
}

function personStatusLabel(kind) {
  if (kind === "protagonist") return "主人公";
  if (kind === "confirmed") return "关系已确认";
  return "身份待补充";
}

function renderPeopleCatalog(catalog) {
  state.peopleCatalog = catalog;
  $("peopleOverview").textContent = catalog.overview || "人物关系会随着照片和讲述继续丰富。";
  const count = catalog.counts?.catalog_entries || catalog.counts?.confirmed || 0;
  $("peopleCount").textContent = `· ${count}`;
  $("peopleCount").classList.toggle("hidden", count === 0);

  const stats = $("peopleStats");
  stats.innerHTML = "";
  [
    ["人物词条", count],
    ["参与经历", new Set((catalog.people || []).flatMap((person) => person.event_ids || [])).size],
    ["待确认线索", catalog.counts?.visual_unknown || 0],
  ].forEach(([label, value]) => {
    const item = document.createElement("span");
    item.innerHTML = `<strong>${value}</strong>${label}`;
    stats.appendChild(item);
  });

  const container = $("peopleCatalog");
  container.innerHTML = "";
  const groups = [["protagonist", "故事中心"], ["confirmed", "生命中的重要人物"]];
  groups.forEach(([kind, label]) => {
    const people = (catalog.people || []).filter((person) => person.kind === kind);
    if (!people.length) return;
    const section = document.createElement("section");
    section.className = "people-group";
    const heading = document.createElement("h3");
    heading.textContent = label;
    section.appendChild(heading);
    const grid = document.createElement("div");
    grid.className = "people-grid";
    people.forEach((person) => {
      const card = document.createElement("article");
      card.className = `person-card ${person.kind}`;
      const head = document.createElement("header");
      head.className = "person-card-head";
      const identity = document.createElement("div");
      const name = document.createElement("h4");
      name.textContent = person.display_name;
      const relation = document.createElement("p");
      relation.textContent = person.relationship || "关系待补充";
      identity.append(name, relation);
      head.appendChild(identity);
      card.appendChild(head);

      const addEntryRow = (label, contentNode) => {
        const row = document.createElement("div");
        row.className = "person-entry-row";
        const term = document.createElement("span");
        term.className = "person-entry-label";
        term.textContent = label;
        row.append(term, contentNode);
        card.appendChild(row);
      };
      const attributes = document.createElement("div");
      attributes.className = "person-attributes";
      (person.key_attributes || [person.relationship]).filter(Boolean).forEach((value) => {
        const tag = document.createElement("span");
        tag.textContent = value;
        attributes.appendChild(tag);
      });
      addEntryRow("关键属性", attributes);

      const events = document.createElement("ol");
      events.className = "person-event-list";
      (person.appearances || []).forEach((value) => {
        const item = document.createElement("li");
        item.textContent = value;
        events.appendChild(item);
      });
      if (!events.children.length) {
        const item = document.createElement("li");
        item.textContent = "共同经历仍在整理中";
        events.appendChild(item);
      }
      addEntryRow("参与事件", events);

      const summary = document.createElement("p");
      summary.className = "person-entry-copy";
      summary.textContent = person.summary;
      addEntryRow("人物词条", summary);
      if (person.story_role && person.kind !== "protagonist") {
        const role = document.createElement("p");
        role.className = "person-entry-copy muted";
        role.textContent = person.story_role;
        addEntryRow("人物作用", role);
      }
      if ((person.chapter_titles || []).length) {
        const chapters = document.createElement("p");
        chapters.className = "person-entry-copy chapter-links";
        chapters.textContent = person.chapter_titles.map((title) => `《${title}》`).join("、");
        addEntryRow("相关章节", chapters);
      }
      grid.appendChild(card);
    });
    section.appendChild(grid);
    container.appendChild(section);
  });
  const pendingCount = catalog.counts?.visual_unknown || 0;
  if (pendingCount) {
    const pending = document.createElement("p");
    pending.className = "pending-people-note";
    pending.textContent = `另有 ${pendingCount} 条身份待确认的人物线索，后续访谈确认姓名或关系后会自动进入人物词条。`;
    container.appendChild(pending);
  }
  if (!container.children.length) container.textContent = "还没有足够的人物线索，继续讲述照片后会逐渐形成。";
}

async function openPeopleCatalog() {
  const dialog = $("peopleDialog");
  dialog.showModal();
  if (state.peopleCatalog) return;
  $("peopleCatalog").innerHTML = '<div class="people-loading">正在从照片、访谈和自传中整理人物档案……</div>';
  $("peopleButton").disabled = true;
  $("peopleButton").classList.add("is-busy");
  try {
    renderPeopleCatalog(await api(`/api/projects/${state.project.id}/people`));
  } catch (error) {
    $("peopleCatalog").textContent = error.message;
    toast(error.message, true);
  } finally {
    $("peopleButton").disabled = false;
    $("peopleButton").classList.remove("is-busy");
  }
}

function openPhotoDeleteDialog(photo) {
  state.photoToDelete = photo;
  const dialog = $("photoDeleteDialog");
  const defaultChoice = dialog.querySelector('input[value="asset_only"]');
  defaultChoice.checked = true;
  updatePhotoDeleteChoice();
  dialog.showModal();
}

function updatePhotoDeleteChoice() {
  const dialog = $("photoDeleteDialog");
  const selected = dialog.querySelector('input[name="photoDeleteMode"]:checked')?.value || "asset_only";
  dialog.querySelectorAll(".delete-choice").forEach((label) => {
    label.classList.toggle("selected", label.querySelector("input").checked);
  });
  $("confirmPhotoDelete").textContent = selected === "asset_only" ? "只删除照片" : "照片和故事一起删除";
}

async function editTimelineEvent(event) {
  const title = window.prompt("这段人生经历叫什么？", event.title);
  if (title === null || !title.trim()) return;
  const yearText = window.prompt("发生年份（记不清可留空）：", event.start_year || "");
  if (yearText === null) return;
  const year = yearText.trim() ? Number(yearText.trim()) : null;
  if (year !== null && (!Number.isInteger(year) || year < 1800 || year > 2200)) return toast("请输入四位年份，记不清可以留空", true);
  try {
    await api(`/api/timeline-events/${event.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: title.trim(), start_year: year, end_year: year,
        time_text: year ? `${year}年` : "",
        time_precision: year ? "year" : "unknown",
      }),
    });
    state.project = await api(`/api/projects/${state.project.id}`);
    renderTimeline(state.project.timeline || []);
    renderChapterList(state.project.chapters || []);
    toast("人生时间线已经重新排序");
  } catch (error) { toast(error.message, true); }
}

function renderChapterList(chapters) {
  const list = $("chapterList");
  if (!chapters.length) {
    list.className = "chapter-list empty-text";
    list.textContent = "还没有章节，从一张照片开始吧。";
    return;
  }
  list.className = "chapter-list";
  list.innerHTML = "";
  chapters.forEach((chapter) => {
    const button = document.createElement("button");
    button.className = "chapter-card";
    const title = document.createElement("h3");
    title.textContent = chapter.title;
    const preview = document.createElement("p");
    preview.textContent = chapter.preview || "点击查看章节";
    button.append(title, preview);
    button.addEventListener("click", async () => renderChapter(await api(`/api/chapters/${chapter.id}`)));
    list.appendChild(button);
  });
}

function renderSession(session) {
  state.session = session;
  $("interviewPanel").classList.remove("hidden");
  const photoFrame = $("storyPhoto").closest(".story-photo-frame");
  photoFrame.classList.toggle("hidden", !session.photo.media_url);
  if (session.photo.media_url) $("storyPhoto").src = session.photo.media_url;
  $("turnCount").textContent = `已回答 ${session.turn_count} 次`;
  renderVisionObservation(session.photo_observation);
  const chat = $("chatMessages");
  chat.innerHTML = "";
  session.turns.forEach((turn) => appendChatMessage(turn.role, turn.content));
  chat.scrollTop = chat.scrollHeight;
  const facts = $("factList");
  facts.innerHTML = "";
  if (!session.facts.length) {
    const item = document.createElement("li");
    item.textContent = "还没有提取到事实线索";
    facts.appendChild(item);
  } else {
    session.facts.forEach((fact) => {
      const item = document.createElement("li");
      item.className = `fact-item ${fact.status === "retracted" ? "retracted" : ""} ${!fact.include_in_book ? "excluded" : ""}`;
      const text = document.createElement("span");
      const suffix = fact.status === "retracted" ? "（旧记录）" : (!fact.include_in_book ? "（不写入书稿）" : "");
      const event = (state.project?.timeline || []).find((item) => item.id === fact.event_id);
      const pending = fact.event_link_status === "needs_confirmation" ? "（归属待确认）" : "";
      const destination = event ? ` → ${timelineLabel(event)} ${event.title}${pending}` : " → 归属待确认";
      text.textContent = `[${fact.fact_type}] ${fact.value}${destination}${suffix}`;
      item.appendChild(text);
      if (fact.status !== "retracted") {
        const actions = document.createElement("span");
        actions.className = "fact-actions";
        const edit = document.createElement("button");
        edit.type = "button";
        edit.textContent = "更正";
        edit.addEventListener("click", () => correctFact(fact));
        const include = document.createElement("button");
        include.type = "button";
        include.textContent = fact.include_in_book ? "不写入" : "恢复写入";
        include.addEventListener("click", () => changeFact(fact, { include_in_book: !Boolean(fact.include_in_book) }));
        const move = document.createElement("button");
        move.type = "button";
        move.textContent = "调整归属";
        move.addEventListener("click", () => moveFact(fact));
        actions.append(edit, move, include);
        item.appendChild(actions);
      }
      facts.appendChild(item);
    });
  }
  const ready = session.status === "ready_to_draft";
  const interviewClosed = ["drafting", "pending_confirmation", "confirmed"].includes(session.status);
  $("replyForm").classList.toggle("hidden", interviewClosed);
  $("generateButton").textContent = ready ? "先看看整理后的故事" : "先整理一版给我看看";
  resizeReplyInput();
  updateReplySendState();
  $("interviewPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderVisionObservation(observation) {
  const box = $("visionBox");
  if (!observation) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = "";
  const status = document.createElement("span");
  status.className = "vision-status-dot";
  const text = document.createElement("span");
  if (observation.status === "ready") {
    text.textContent = "我已经先看过这张照片了，会在聊天中一次只和您核对一个线索。";
  } else if (observation.status === "vision_unavailable") {
    text.textContent = "这次没有成功看清照片，我们仍然可以从您的讲述慢慢聊起。";
  } else {
    text.textContent = "照片已经保存，我们从您最记得的地方慢慢说起。";
  }
  box.append(status, text);
  if (state.session?.photo_id) {
    const reanalyze = document.createElement("button");
    reanalyze.type = "button";
    reanalyze.className = "secondary small vision-reanalyze";
    reanalyze.textContent = observation.status === "ready" ? "重新识图" : "现在识别这张照片";
    reanalyze.addEventListener("click", async () => {
      setBusy(reanalyze, true, "正在仔细看照片…");
      try {
        await api(`/api/photos/${state.session.photo_id}/analyze`, { method: "POST" });
        renderSession(await api(`/api/sessions/${state.session.id}`));
        toast("照片已经重新看过，后面的聊天会使用新的线索");
      } catch (error) { toast(error.message, true); }
      finally { setBusy(reanalyze, false); }
    });
    box.appendChild(reanalyze);
  }
}

async function moveFact(fact) {
  const events = state.project?.timeline || [];
  const choices = events.map((event, index) => `${index + 1}. ${timelineLabel(event)} ${event.title}`).join("\n");
  const selected = window.prompt(`这条记忆属于哪段人生经历？\n${choices}`, String(Math.max(1, events.findIndex((event) => event.id === fact.event_id) + 1)));
  if (!selected) return;
  const event = events[Number(selected) - 1];
  if (!event) return toast("没有找到这个时间节点", true);
  await changeFact(fact, { event_id: event.id });
}

async function changeFact(fact, changes) {
  try {
    await api(`/api/facts/${fact.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
    });
    state.project = await api(`/api/projects/${state.project.id}`);
    renderTimeline(state.project.timeline || []);
    renderSession(await api(`/api/sessions/${state.session.id}`));
    toast("事实线索已更新；下次生成或修改章节时会使用新记录");
  } catch (error) { toast(error.message, true); }
}

function correctFact(fact) {
  const value = window.prompt("请按您的记忆更正这条事实：", fact.value);
  if (value && value.trim() && value.trim() !== fact.value) changeFact(fact, { value: value.trim() });
}

function renderReview(version) {
  const box = $("reviewInfo");
  let review = {};
  try { review = JSON.parse(version.review_json); } catch (_) {}
  const issues = Array.isArray(review.issues) ? review.issues : [];
  box.className = `review-box ${issues.length ? "warn" : "ok"}`;
  box.textContent = issues.length
    ? `审校发现：${issues.join("；")}`
    : "已完成硬事实与文学表达审校。这是一版可以继续修改的文学初稿。";
}

function renderRevisionCandidate(candidate) {
  state.revisionCandidate = candidate || null;
  const panel = $("revisionCandidatePanel");
  if (!candidate) {
    panel.classList.add("hidden");
    $("revisionCandidateContent").textContent = "";
    return;
  }
  panel.classList.remove("hidden");
  $("revisionCandidateTitle").textContent = candidate.title || state.chapter?.title || "修改候选稿";
  $("revisionCandidateContent").textContent = candidate.content || "";

  const correction = candidate.correction || {};
  const proposals = correction.proposals || [];
  const unmatched = correction.unmatched || [];
  const memory = $("revisionCandidateMemory");
  memory.innerHTML = "";
  const heading = document.createElement("strong");
  heading.textContent = proposals.length ? "采用后将同步更正这些记忆" : "此次修改不会改变个人记忆";
  memory.appendChild(heading);
  if (!proposals.length && !unmatched.length) {
    const note = document.createElement("p");
    note.textContent = "人物、时间线、事件和事实记录保持不变，只调整章节的叙事表达。";
    memory.appendChild(note);
  }
  proposals.forEach((proposal) => {
    const row = document.createElement("div");
    row.className = "candidate-memory-change";
    const before = document.createElement("span");
    before.textContent = `原记忆：${proposal.old_value}`;
    const after = document.createElement("span");
    after.textContent = `更正为：${proposal.new_value}`;
    row.append(before, after);
    memory.appendChild(row);
  });
  unmatched.forEach((item) => {
    const warning = document.createElement("p");
    warning.className = "candidate-memory-warning";
    warning.textContent = `尚未写入记忆：${item.reason}`;
    memory.appendChild(warning);
  });

  const reviewBox = $("revisionCandidateReview");
  const review = candidate.review || {};
  const issues = Array.isArray(review.issues) ? review.issues : [];
  reviewBox.className = `review-box ${issues.length ? "warn" : "ok"}`;
  reviewBox.textContent = issues.length
    ? `候选稿审校发现：${issues.join("；")}`
    : "候选稿已通过事实与文学表达审校。采用前，当前章节仍保持不变。";
  $("adoptRevisionButton").disabled = unmatched.length > 0;
  $("adoptRevisionButton").title = unmatched.length ? "请按提示明确事实更正后重新生成" : "";
}

function renderRelation(suggestion) {
  const box = $("relationBox");
  if (!suggestion) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const label = { new: "另起一章", attach: "只作为已有章节的补充图片", merge: "与已有章节合并" }[suggestion.choice] || "另起一章";
  box.innerHTML = "";
  const text = document.createElement("p");
  text.textContent = `新照片关系建议：${label}。${suggestion.reason || ""}`;
  const actions = document.createElement("div");
  actions.className = "relation-actions";
  const select = document.createElement("select");
  const targets = (state.project?.chapters || []).filter((chapter) => chapter.id !== state.chapter?.id);
  targets.forEach((chapter) => {
    const option = document.createElement("option");
    option.value = chapter.id;
    option.textContent = chapter.title;
    if (suggestion.chapter_id === chapter.id) option.selected = true;
    select.appendChild(option);
  });
  const choices = [
    ["new", "独立成章"],
    ["attach", "只补充图片"],
    ["merge", "合并并生成新版本"],
  ];
  choices.forEach(([choice, caption]) => {
    const button = document.createElement("button");
    button.className = choice === suggestion.choice ? "primary small" : "secondary small";
    button.type = "button";
    button.textContent = caption;
    button.disabled = choice !== "new" && !targets.length;
    button.addEventListener("click", () => applyRelation(choice, select.value));
    actions.appendChild(button);
  });
  if (targets.length) actions.prepend(select);
  box.append(text, actions);
}

async function applyRelation(choice, targetChapterId) {
  try {
    const result = await api(`/api/photos/${state.session.photo_id}/relation`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        choice,
        chapter_id: choice === "new" ? null : targetChapterId,
        source_chapter_id: state.chapter.id,
      }),
    });
    state.project = await api(`/api/projects/${state.project.id}`);
    renderChapterList(state.project.chapters || []);
    renderChapter(result.chapter);
    const messages = {
      new: "已保留为独立章节",
      attach: "照片已加入所选章节；新故事草稿仍保留在本地记录中",
      merge: "已创建合并后的新版本，旧版本和旧分享链接保持不变",
    };
    toast(messages[choice]);
  } catch (error) { toast(error.message, true); }
}

function renderShare(link) {
  const box = $("shareBox");
  if (!link) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = "";
  const row = document.createElement("div");
  row.className = "share-link";
  const input = document.createElement("input");
  const path = link.share_url || `/share/${link.token}`;
  input.value = new URL(path, window.location.origin).href;
  input.readOnly = true;
  const copy = document.createElement("button");
  copy.className = "secondary small";
  copy.textContent = "复制链接";
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(input.value);
    toast("私密链接已复制");
  });
  const revoke = document.createElement("button");
  revoke.className = "danger-button small";
  revoke.textContent = "撤回分享";
  revoke.addEventListener("click", async () => {
    await api(`/api/shares/${link.id}`, { method: "DELETE" });
    renderShare(null);
    toast("分享链接已撤回，原链接不能再访问");
  });
  row.append(input, copy, revoke);
  box.append("此链接只显示当前已确认版本，不包含原图：", row);
}

function renderChapter(chapter, shouldScroll = true) {
  state.chapter = chapter;
  const version = chapter.current_version;
  $("chapterPageEmpty").classList.add("hidden");
  $("chapterPanel").classList.remove("hidden");
  $("chapterTitle").textContent = chapter.title;
  $("chapterStatus").textContent = chapter.status === "confirmed" ? "已确认" : `文学草稿 · 第 ${version.version_number} 版`;
  $("chapterContent").textContent = version.content;
  renderRevisionCandidate(chapter.revision_candidate || null);
  $("confirmButton").disabled = chapter.status === "confirmed";
  $("confirmButton").textContent = chapter.status === "confirmed" ? "这一版已确认" : "我确认这一版";
  renderReview(version);
  renderRelation(chapter.relation_suggestion);
  $("nextStorySuggestion").textContent = chapter.next_story_suggestion || "";
  $("shareButton").disabled = chapter.status !== "confirmed";
  $("shareButton").title = chapter.status === "confirmed" ? "" : "请先确认当前版本";
  renderShare((chapter.active_shares || [])[0] || null);
  showWorkspacePage("chapter", { scroll: false });
  if (shouldScroll) {
    $("chapterPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function renderBookEdition(edition, shouldScroll = true) {
  state.bookEdition = edition;
  $("bookPageEmpty").classList.add("hidden");
  $("bookEditionPanel").classList.remove("hidden");
  const manuscript = edition.manuscript || {};
  $("bookEditionKicker").textContent = `第 ${edition.edition_number} 版 · 第三人称个人自传`;
  $("bookEditionTitle").textContent = edition.title;
  $("bookEditionSubtitle").textContent = edition.subtitle || "一部随着照片不断生长的人生作品";
  const passed = Boolean(edition.review?.passed);
  $("bookEditionStatus").textContent = edition.status === "confirmed"
    ? "已确认"
    : passed ? "终审通过" : "需要继续修改";
  const scopeLabels = { micro: "微型自传", growing: "成长中的自传", full: "完整人生版" };
  $("bookEditionScope").textContent = scopeLabels[edition.scope] || "个人自传";
  $("bookCharacterPortrait").textContent = edition.character_portrait || manuscript.character_portrait || "";
  $("bookPreface").textContent = manuscript.preface || "";
  $("bookAfterword").textContent = manuscript.afterword || "";
  $("bookArc").textContent = edition.core_theme || manuscript.core_theme || "照片让零散的人生经历成为一部可以完整阅读的作品。";
  const threads = $("bookThreads");
  threads.innerHTML = "";
  (edition.review?.character_traits || []).forEach((trait) => {
    const chip = document.createElement("span");
    chip.className = "thread-chip";
    chip.textContent = trait;
    threads.appendChild(chip);
  });
  const changes = $("bookChanges");
  changes.innerHTML = "";
  (manuscript.sections || []).forEach((section, index) => {
    const item = document.createElement("article");
    item.className = "autobiography-section";
    const heading = document.createElement("header");
    const number = document.createElement("span");
    number.textContent = String(index + 1).padStart(2, "0");
    const title = document.createElement("h3");
    title.textContent = section.title;
    heading.append(number, title);
    item.appendChild(heading);

    if ((section.photos || []).length) {
      const gallery = document.createElement("div");
      gallery.className = "autobiography-photo-gallery";
      section.photos.forEach((photo) => {
        const visual = document.createElement("figure");
        visual.className = "autobiography-photo";
      const image = document.createElement("img");
      image.src = photo.media_url;
        image.alt = photo.note || `${section.title}对应的照片`;
      image.loading = "lazy";
      image.decoding = "async";
      visual.appendChild(image);
        const caption = document.createElement("figcaption");
        caption.textContent = photo.note || "人生照片";
        visual.appendChild(caption);
        gallery.appendChild(visual);
      });
      item.appendChild(gallery);
    }
    const prose = document.createElement("div");
    prose.className = "autobiography-prose autobiography-section-prose";
    prose.textContent = section.content;
    item.appendChild(prose);
    const insight = document.createElement("aside");
    insight.className = "autobiography-insight";
    const meaning = document.createElement("p");
    meaning.innerHTML = `<strong>照片留下的意义</strong><span></span>`;
    meaning.querySelector("span").textContent = section.photo_meaning || "";
    const revelation = document.createElement("p");
    revelation.innerHTML = `<strong>这一章照见的她</strong><span></span>`;
    revelation.querySelector("span").textContent = section.character_revelation || "";
    insight.append(meaning, revelation);
    item.appendChild(insight);
    changes.appendChild(item);
  });
  const review = edition.review || {};
  $("bookReview").className = `review-box ${passed ? "ok" : "warn"}`;
  $("bookReview").textContent = passed
    ? `第三人称终审通过 · 照片覆盖 ${Math.round((review.photo_coverage || 0) * 100)}% · 故事覆盖 ${Math.round((review.source_coverage || 0) * 100)}% · 文学性 ${review.literary_quality_score || "—"}/5 · 人物价值 ${review.value_expression_score || "—"}/5`
    : `终审发现：${(review.issues || []).join("；") || "仍需继续修改"}`;
  $("confirmBookEditionButton").disabled = !passed || edition.status === "confirmed";
  $("confirmBookEditionButton").textContent = edition.status === "confirmed" ? "这一版自传已确认" : "确认这一版自传";
  if (shouldScroll) {
    showWorkspacePage("book", { scroll: false });
    $("bookEditionPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

$("projectForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  setBusy(button, true, "正在创建…");
  try {
    const project = await api("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("projectTitle").value, narrative_person: $("personSelect").value }),
    });
    $("projectCreateDialog").close();
    await loadProjects(project.id);
    toast("自传项目已经建好，可以上传第一张照片了");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

$("newProjectButton").addEventListener("click", () => {
  const dialog = $("projectCreateDialog");
  if (!dialog.open) dialog.showModal();
  $("newProjectButton").setAttribute("aria-expanded", "true");
  window.setTimeout(() => $("projectTitle").select(), 0);
});

$("projectCreateDialog").addEventListener("close", () => {
  $("newProjectButton").setAttribute("aria-expanded", "false");
});

$("projectCreateDialog").addEventListener("cancel", () => {
  $("newProjectButton").setAttribute("aria-expanded", "false");
});

$("projectCreateDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});

$("closeProjectCreateDialog").addEventListener("click", () => $("projectCreateDialog").close());
$("cancelProjectCreate").addEventListener("click", () => $("projectCreateDialog").close());

$("activePersonSelect").addEventListener("change", async (event) => {
  try {
    state.project = await api(`/api/projects/${state.project.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ narrative_person: event.target.value }),
    });
    toast("叙述人称已更新，新生成版本会使用这个设置");
    await loadProjects();
  } catch (error) { toast(error.message, true); }
});

$("photoInput").addEventListener("change", (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const preview = $("uploadPreview");
  preview.src = URL.createObjectURL(file);
  preview.classList.remove("hidden");
  $("dropZone").querySelectorAll("span,strong,small").forEach((el) => el.classList.add("hidden"));
});

$("uploadForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const file = $("photoInput").files[0];
  if (!file) return toast("请先选择一张照片", true);
  const form = new FormData();
  form.append("image", file);
  form.append("title", $("photoTitle").value);
  form.append("note", $("photoNote").value);
  setBusy(button, true, "正在看照片并整理线索…");
  try {
    const result = await api(`/api/projects/${state.project.id}/photos`, { method: "POST", body: form });
    state.project = await api(`/api/projects/${state.project.id}`);
    renderTimeline(state.project.timeline || []);
    renderSession(result.session);
    $("photoTitle").value = "";
    $("photoNote").value = "";
    $("photoInput").value = "";
    $("uploadPreview").src = "";
    $("uploadPreview").classList.add("hidden");
    $("dropZone").querySelectorAll("span,strong,small").forEach((el) => el.classList.remove("hidden"));
    toast("照片已安全保存，我们开始聊聊它的故事吧");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

$("replyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("replyText").value.trim();
  if (!text || replyThinking || voiceListening) return;
  const optimisticMessage = appendChatMessage("user", text, { pending: true });
  $("replyText").value = "";
  resizeReplyInput();
  setReplyThinking(true);
  try {
    const session = await api(`/api/sessions/${state.session.id}/reply`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
    });
    state.project = await api(`/api/projects/${state.project.id}`);
    renderTimeline(state.project.timeline || []);
    renderSession(session);
  } catch (error) {
    optimisticMessage.remove();
    $("replyText").value = text;
    resizeReplyInput();
    toast(error.message, true);
  } finally {
    setReplyThinking(false);
  }
});

$("replyText").addEventListener("input", () => {
  resizeReplyInput();
  updateReplySendState();
});

$("replyText").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (!$("replySendButton").disabled) $("replyForm").requestSubmit($("replySendButton"));
});

$("generateButton").addEventListener("click", async (event) => {
  setBusy(event.currentTarget, true, "正在写作和审校…");
  try {
    const chapter = await api(`/api/sessions/${state.session.id}/generate`, { method: "POST" });
    const currentSession = state.session;
    state.project = await api(`/api/projects/${state.project.id}`);
    state.session = currentSession;
    renderTimeline(state.project.timeline || []);
    renderChapterList(state.project.chapters || []);
    renderChapter(chapter);
    toast("第一版章节已经生成，请您仔细看看");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(event.currentTarget, false); }
});

$("revisionForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const instruction = $("revisionText").value.trim();
  if (!instruction) return toast("请写下想修改的地方", true);
  setBusy(button, true, "正在生成新版本…");
  try {
    const chapter = await api(`/api/chapters/${state.chapter.id}/revise`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        instruction,
        mode: state.revisionMode,
        base_candidate_id: state.revisionBaseCandidateId,
      }),
    });
    $("revisionText").value = "";
    state.revisionMode = "auto";
    state.revisionBaseCandidateId = null;
    renderRevisionCandidate(chapter);
    $("revisionCandidatePanel").scrollIntoView({ behavior: "smooth", block: "start" });
    toast("修改候选稿已经生成，当前章节和个人记忆都还没有变化");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

document.querySelectorAll(".revision-chip[data-revision]").forEach((button) => {
  button.addEventListener("click", () => {
    $("revisionText").value = button.dataset.revision || "";
    state.revisionMode = "style";
    state.revisionBaseCandidateId = null;
    $("revisionText").focus();
  });
});

$("correctionChip").addEventListener("click", () => {
  $("revisionText").value = "这个细节不准确：请把‘’改成‘’，其余硬事实和文学表达保持不变。";
  $("revisionText").focus();
  state.revisionMode = "fact";
  state.revisionBaseCandidateId = null;
  const start = $("revisionText").value.indexOf("‘’") + 1;
  $("revisionText").setSelectionRange(start, start);
});

$("continueRevisionButton").addEventListener("click", () => {
  if (!state.revisionCandidate) return;
  state.revisionBaseCandidateId = state.revisionCandidate.id;
  state.revisionMode = "auto";
  $("revisionText").value = "";
  $("revisionText").placeholder = "继续说明希望怎样调整这份候选稿";
  $("revisionText").focus();
  $("revisionForm").scrollIntoView({ behavior: "smooth", block: "center" });
});

$("discardRevisionButton").addEventListener("click", async (event) => {
  if (!state.revisionCandidate) return;
  setBusy(event.currentTarget, true, "正在放弃…");
  try {
    await api(`/api/chapter-revision-candidates/${state.revisionCandidate.id}/discard`, { method: "POST" });
    renderRevisionCandidate(null);
    state.revisionBaseCandidateId = null;
    toast("候选稿已放弃，当前章节和个人记忆没有变化");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(event.currentTarget, false); }
});

$("adoptRevisionButton").addEventListener("click", async (event) => {
  if (!state.revisionCandidate) return;
  setBusy(event.currentTarget, true, "正在采用…");
  try {
    const chapter = await api(`/api/chapter-revision-candidates/${state.revisionCandidate.id}/adopt`, { method: "POST" });
    state.project = await api(`/api/projects/${state.project.id}`);
    renderTimeline(state.project.timeline || []);
    renderChapterList(state.project.chapters || []);
    renderChapter(chapter);
    state.revisionBaseCandidateId = null;
    toast("候选稿已成为当前章节；其中明确的事实更正也已同步到个人记忆");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(event.currentTarget, false); }
});

$("confirmButton").addEventListener("click", async (event) => {
  setBusy(event.currentTarget, true, "正在确认…");
  try {
    renderChapter(await api(`/api/chapters/${state.chapter.id}/confirm`, { method: "POST" }));
    toast("这一版已经确认并锁定，之后修改会创建新版本");
    state.project = await api(`/api/projects/${state.project.id}`);
    renderChapterList(state.project.chapters);
  } catch (error) { toast(error.message, true); }
  finally { setBusy(event.currentTarget, false); }
});

$("shareButton").addEventListener("click", async (event) => {
  setBusy(event.currentTarget, true, "正在创建…");
  try {
    const link = await api(`/api/chapters/${state.chapter.id}/shares`, { method: "POST" });
    renderShare(link);
    toast("私密分享链接已经生成，并绑定当前确认版本");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(event.currentTarget, false); }
});

$("weaveBookButton").addEventListener("click", async (event) => {
  setBusy(event.currentTarget, true, "正在把照片故事写成一本书…");
  try {
    const edition = await api(`/api/projects/${state.project.id}/autobiography/compile`, { method: "POST" });
    renderBookEdition(edition);
    toast(edition.review?.passed ? "新的第三人称自传已经生成，原照片故事保持不变" : "自传版本已保存，但终审发现了需要处理的问题", !edition.review?.passed);
  } catch (error) { toast(error.message, true); }
  finally { setBusy(event.currentTarget, false); }
});

$("confirmBookEditionButton").addEventListener("click", async (event) => {
  if (!state.bookEdition) return;
  setBusy(event.currentTarget, true, "正在确认整书版本…");
  try {
    const edition = await api(`/api/autobiography-editions/${state.bookEdition.id}/confirm`, { method: "POST" });
    renderBookEdition(edition);
    toast("这一版完整自传已经确认，原照片故事仍保持独立版本");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(event.currentTarget, false); }
});

$("updateAutobiographyButton").addEventListener("click", () => {
  $("weaveBookButton").click();
});

$("exportButton").addEventListener("click", async () => {
  try {
    const data = await api(`/api/projects/${state.project.id}/export`);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${state.project.title}-数据导出.json`;
    link.click();
    URL.revokeObjectURL(link.href);
    toast("项目数据已经导出");
  } catch (error) { toast(error.message, true); }
});

$("deleteProjectButton").addEventListener("click", async () => {
  const accepted = window.confirm(`确定删除“${state.project.title}”吗？对话、事实、章节、分享链接和本地照片都会删除，无法恢复。`);
  if (!accepted) return;
  try {
    await api(`/api/projects/${state.project.id}`, { method: "DELETE" });
    state.project = null;
    state.session = null;
    state.chapter = null;
    $("projectWorkspace").classList.add("hidden");
    $("workspacePageNav").classList.add("hidden");
    $("welcome").classList.remove("hidden");
    await loadProjects();
    toast("项目及其本地数据已经删除");
  } catch (error) { toast(error.message, true); }
});

$("peopleButton").addEventListener("click", openPeopleCatalog);
$("closePeopleDialog").addEventListener("click", () => $("peopleDialog").close());
$("peopleDialog").addEventListener("click", (event) => {
  if (event.target === $("peopleDialog")) $("peopleDialog").close();
});

document.querySelectorAll('input[name="photoDeleteMode"]').forEach((input) => {
  input.addEventListener("change", updatePhotoDeleteChoice);
});

$("cancelPhotoDelete").addEventListener("click", () => {
  $("photoDeleteDialog").close();
  state.photoToDelete = null;
});

$("photoDeleteForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.photoToDelete || !state.project) return;
  const mode = new FormData(event.currentTarget).get("photoDeleteMode") || "asset_only";
  const deleteContent = mode === "asset_and_story";
  if (deleteContent) {
    const accepted = window.confirm("这会删除相关独立章节，并清除由它生成的完整自传版本，无法恢复。确定继续吗？");
    if (!accepted) return;
  }
  const button = $("confirmPhotoDelete");
  setBusy(button, true, deleteContent ? "正在删除照片和故事…" : "正在删除照片…");
  try {
    const photoId = state.photoToDelete.id;
    const result = await api(`/api/photos/${photoId}?delete_content=${deleteContent}`, { method: "DELETE" });
    $("photoDeleteDialog").close();
    if (state.session?.photo_id === photoId) {
      state.session = null;
      $("interviewPanel").classList.add("hidden");
    }
    state.project = await api(`/api/projects/${state.project.id}`);
    renderTimeline(state.project.timeline || []);
    renderChapterList(state.project.chapters || []);
    if (deleteContent) {
      state.chapter = null;
      state.bookEdition = null;
      $("chapterPanel").classList.add("hidden");
      $("bookEditionPanel").classList.add("hidden");
      $("chapterPageEmpty").classList.remove("hidden");
      $("bookPageEmpty").classList.remove("hidden");
      toast(`照片和相关故事已删除，共移除 ${result.removed_chapters || 0} 个章节`);
    } else {
      if (state.chapter) renderChapter(await api(`/api/chapters/${state.chapter.id}`), false);
      toast("照片已经删除，访谈记忆、章节和自传内容都已保留");
    }
    state.photoToDelete = null;
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

$("refreshProjects").addEventListener("click", () => loadProjects().catch((error) => toast(error.message, true)));

document.querySelectorAll("[data-workspace-view]").forEach((button) => {
  button.addEventListener("click", async () => {
    const page = button.dataset.workspaceView;
    if (page === "chapter" && !state.chapter && state.project?.chapters?.length) {
      try {
        renderChapter(await api(`/api/chapters/${state.project.chapters[0].id}`));
      } catch (error) { toast(error.message, true); }
      return;
    }
    showWorkspacePage(page);
  });
});

document.querySelectorAll("[data-go-workspace]").forEach((button) => {
  button.addEventListener("click", () => showWorkspacePage(button.dataset.goWorkspace));
});

document.querySelector("[data-generate-book]").addEventListener("click", () => {
  $("weaveBookButton").click();
});

window.requestAnimationFrame(() => document.body.classList.add("page-ready"));
setupVoiceInput();
resizeReplyInput();
updateReplySendState();
Promise.all([loadHealth(), loadProjects(initialProjectId)]).catch((error) => toast(error.message, true));
