"""文件资料页拖入任意文件即走既有批量上传队列，原始会话页不接这套入口。"""

from rogmem.ui import sources_page


def _import_script(html: str) -> str:
    return html.split("function showDuplicates", 1)[1].split("</script>", 1)[0]


def test_knowledge_page_wires_drop_onto_queue_files():
    html = sources_page([], "file", {}, {})
    script = _import_script(html)
    queue = script.split("function queueFiles", 1)[1].split("function sendFiles", 1)[0]
    drop = script.split("function bindKnowledgeDrop", 1)[1]

    assert "main .content.drop-ready" in html
    assert "POST', '/admin/api/sources/import'" in script
    assert queue.index("revealFiles(files)") < queue.index("uploadFile(")
    assert "failRow(row, '文件太大')" in queue
    assert "failRow(row, '没有传上去')" in script
    assert drop.index("'dragenter'") < drop.index("'drop'")
    assert "queueFiles(event.dataTransfer && event.dataTransfer.files)" in drop
    assert "paint(true)" in drop
    assert "paint(false)" in drop
    assert "dialog.open" in drop


def test_session_page_does_not_offer_knowledge_drop():
    html = sources_page([], "session", {}, {})
    assert "import-files" not in html
    assert "function queueFiles" not in html
    assert "bindKnowledgeDrop" not in html
