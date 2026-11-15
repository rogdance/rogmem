"""知识文件导入菜单与极空间目录窗的页面契约。"""

from rogmem.ui import page, sources_page


def _style(html: str) -> str:
    return html.split("<style>", 1)[1].split("</style>", 1)[0]


def test_pages_declare_a_device_width_viewport():
    html = page("标题", "crystals", "<p>demo</p>")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html


def test_only_the_file_page_is_marked_for_the_narrow_column_fix():
    files = sources_page([], "file", {}, {})
    session = sources_page([], "session", {}, {})
    assert 'class="file-screen"' in files
    assert "file-screen" not in session.split("<body", 1)[1].split(">", 1)[0]
    style = _style(files)
    assert "body.file-screen .content" in style
    assert "align-self: stretch" in style
    assert "max-width: 100%" in style
    narrow = style.split("@media (max-width: 760px)", 1)[1]
    assert "body.file-screen .content" in narrow


def test_import_choices_sit_in_a_row_under_the_primary_trigger():
    html = sources_page([], "file", {}, {})
    style = _style(html)
    trigger = html.split('id="import-open"', 1)[0]
    assert 'class="btn"' in trigger[-40:]
    assert 'class="import-panel"' in html
    panel = style.split(".import-menu .import-panel", 1)[1].split("}", 1)[0]
    assert "flex-direction: row" in panel
    item = style.split('.import-menu [role="menuitem"]', 1)[1].split("}", 1)[0]
    assert "background: transparent" in item
    assert "min-height: 44px" in item
    menu = html.split('id="import-menu"', 1)[1].split('id="import-files"', 1)[0]
    assert menu.index('id="import-local"') < menu.index('id="import-nas"')
    assert "选择这台设备" not in html
    assert "个人空间挑选" not in html


def test_directory_dialog_has_a_title_and_quiet_empty_states():
    html = sources_page([], "file", {}, {})
    dialog = html.split('id="nas-import"', 1)[1].split("</dialog>", 1)[0]
    assert 'id="nas-title"' in dialog
    assert ">从极空间导入</h2>" in dialog
    assert 'class="empty"' in dialog
    assert "暂无文件" in html
    assert "个人空间还没挂上" in html
    assert "列出中" in html
    unmounted = html.split("var unmounted = message === '极空间目录还没挂进容器';", 1)[1]
    unmounted = unmounted.split("if (!unmounted && nasAsk)", 1)[0]
    assert "where.href = '/admin/settings/basic'" in unmounted
    assert "where.textContent = '基础设置'" in unmounted
    assert "里填写。" in unmounted
    assert "详情" not in dialog
    style = _style(html)
    plain = style.split("#nas-import .nas-list", 1)[1].split("}", 1)[0]
    assert "flex: 1" not in plain
    browse = style.split("#nas-import.nas-browse .nas-list", 1)[1].split("}", 1)[0]
    assert "flex: 1" in browse
    assert "#nas-import.nas-browse" in style.split("@media (max-width: 760px)", 1)[-1]


def test_dialog_session_and_menu_phase_are_explicit():
    html = sources_page([], "file", {}, {})
    assert "var nasSession" in html
    assert "var nasRequest" in html
    assert "function nasOwns" in html
    assert "function disarmNasSelection" in html
    load = html.split("async function loadNas", 1)[1].split("async function postNas", 1)[0]
    assert load.index("disarmNasSelection(") < load.index("fetch(")
    assert load.index("if (!nasOwns(session, request)) return;") < load.index("paintNas(")
    post = html.split("async function postNas", 1)[1].split("function setImportPhase", 1)[0]
    assert "if (nasImporting) return;" in post
    assert post.index("if (!nasOwns(session, request)) return;") < post.index("location = '/admin/sources?tab=file'")
    close = html.split("addEventListener('close'", 1)[1].split("addEventListener", 1)[0]
    assert "location" not in close
    assert "nasSession" in close
    sched = html.split("function schedulePreviewClose", 1)[1].split("function importFinePointer", 1)[0]
    assert "importPhase !== 'preview'" in sched
    assert "mark !== importClock" in sched
