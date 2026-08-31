from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_chat_style_project_sidebar_structure():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    styles = (STATIC / "minimal.css").read_text(encoding="utf-8")

    assert html.index('id="newProjectButton"') < html.index('id="projectList"')
    assert html.index("</main>") < html.index('id="projectCreateDialog"')
    assert "最近项目" in html
    assert 'class="project-create-dialog"' in html
    assert "sidebar-create-panel" not in html
    assert 'button.append(title);' in script
    assert 'button.append(title, meta);' not in script
    assert '$("newProjectButton").addEventListener("click"' in script
    assert ".layout {\n  width: 100%;\n  margin: 0;" in styles
    assert "dialog.showModal()" in script
    assert ".project-create-dialog::backdrop" in styles
    assert ".privacy-note {\n  margin-top: auto;" in styles
    assert ".privacy-note strong { color: #47544c; font-size: 11px;" in styles


def test_workspace_tabs_belong_to_selected_project():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    project_workspace = html.index('id="projectWorkspace"')
    project_heading = html.index('id="activeProjectTitle"')
    project_navigation = html.index('id="workspacePageNav"')
    first_project_page = html.index('data-workspace-page="story"')

    assert project_workspace < project_heading < project_navigation < first_project_page
    assert 'aria-label="当前项目内容"' in html


def test_brand_aligns_with_sidebar_and_has_memoir_album_icon():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    styles = (STATIC / "minimal.css").read_text(encoding="utf-8")

    assert 'class="brand-mark"' in html
    assert 'class="brand-album"' in html
    assert 'class="brand-memory-head"' in html
    assert "岁影" in html and "Memoir Album" in html
    assert "padding: 9px 22px 9px 29px;" in styles
    assert ".brand-block {\n  grid-column: 1;" in styles
    assert ".status-pill {\n  grid-column: 2;" in styles


def test_interview_composer_supports_voice_arrow_and_optimistic_message():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    styles = (STATIC / "minimal.css").read_text(encoding="utf-8")

    assert 'id="voiceInputButton"' in html
    assert 'id="replySendButton"' in html
    assert 'class="send-arrow"' in html
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in script
    assert 'appendChatMessage("user", text, { pending: true })' in script
    assert "setReplyThinking(true)" in script
    assert ".reply-send-button.is-thinking .send-arrow" in styles


def test_book_page_hides_internal_review_metrics():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    styles = (STATIC / "minimal.css").read_text(encoding="utf-8")

    assert 'id="bookReview"' not in html
    assert "第三人称终审通过 · 照片覆盖" not in script
    opening = html.index('class="autobiography-opening"')
    portrait = html.index('id="bookCharacterPortrait"')
    preface = html.index('id="bookPreface"')
    assert opening < portrait < preface
    assert ".autobiography-cover {\n  width: 50%;" in styles
    assert "grid-template-columns: minmax(0, 1fr);" in styles
    assert "max-width: 920px;" in styles
    assert "padding: 12px clamp(22px, 4vw, 36px);" in styles
    assert ".autobiography-opening .autobiography-portrait" in styles
    assert ".autobiography-opening .autobiography-preface" in styles
    assert 'id="bookCoverPhoto"' in html
    assert 'coverPhoto.src = coverVisual.media_url;' in script
    assert ".autobiography-cover-photo" in styles
    assert "#bookEditionScope" in styles
