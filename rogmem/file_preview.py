"""文件资料的原文预览：磁盘上的原文件 + 随包带上的离线渲染器。

预览用的开源库见 vendor/preview/REVISION。它完全在浏览器里跑，不把文件传到
任何外部服务；服务端只做两件事：把扫描目录里的原始字节按 inline 发回，以及
把 vendor/preview 里的静态文件发出去。
"""

from __future__ import annotations

from html import escape as esc
from pathlib import Path

from rogmem import document_view

ASSET_ROOT = (Path(__file__).parent / "vendor" / "preview").resolve()
ASSET_URL = "/admin/static/preview"
SCRIPT_URL = ASSET_URL + "/omni-doc-viewer.iife.js"
PDF_WORKER_URL = ASSET_URL + "/pdfjs/build/pdf.worker.min.mjs"

# 与 engine.FILE_TYPES 一一对应。text/* 一律带 charset，中文才不会乱码。
MIME = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
}


def suffix_of(filename: str) -> str:
    name = Path(str(filename or "")).name
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def mime_for(filename: str) -> str:
    return MIME.get(suffix_of(filename), "application/octet-stream")


def asset_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".js", ".mjs"}:
        return "text/javascript; charset=utf-8"
    if suffix in {".css"}:
        return "text/css; charset=utf-8"
    if suffix in {".txt", ".md"}:
        return "text/plain; charset=utf-8"
    if suffix == ".json":
        return "application/json; charset=utf-8"
    if suffix in {".svg"}:
        return "image/svg+xml"
    if suffix in {".ttf", ".otf"}:
        return "font/ttf"
    if suffix == ".wasm":
        return "application/wasm"
    return "application/octet-stream"


# 占位符用 replace，不进 f-string：JS 里的花括号原样保留。
_PREVIEW_HTML = """<div class='file-preview'>
<div class='file-preview-bar'>
<span class='file-preview-name'>__FILENAME__</span>
<span class='file-preview-kind'>__KIND__</span>
<a href='__FILE_URL__' target='_blank' rel='noopener'>在新窗口打开原文件</a>
<a href='__DOWNLOAD_URL__'>下载原文件</a>
</div>
<div id='file-preview-stage' class='file-preview-stage'><p class='muted'>正在载入文件预览…</p></div>
<p id='file-preview-error' class='file-preview-error' hidden></p>
</div>
<script src='__SCRIPT_URL__'></script>
<script>
(function () {
  var stage = document.getElementById('file-preview-stage');
  var error = document.getElementById('file-preview-error');
  function fail(message) {
    if (stage) stage.innerHTML = '';
    if (error) {
      error.textContent = '预览失败：' + message + '。可以直接用上边的「在新窗口打开原文件」查看。';
      error.hidden = false;
    }
  }
  if (!window.RogMemDoc || !window.RogMemDoc.renderDocument) {
    fail('预览组件没有载入');
    return;
  }
  var worker = '__WORKER_URL__';
  try {
    window.RogMemDoc.setPdfWorkerSrc(worker);
  } catch (e) { /* 非 PDF 文件用不到 worker */ }
  try {
    window.RogMemDoc.renderDocument({
      container: stage,
      source: '__FILE_URL__',
      fetchOptions: { credentials: 'same-origin' },
      pdf: { workerSrc: worker }
    }).catch(function (e) {
      fail(e && e.message ? e.message : String(e));
    });
  } catch (e) {
    fail(e && e.message ? e.message : String(e));
  }
})();
</script>"""


# 大文件降级：服务端按片给文本，前端用 textContent 写入，整本不进 DOM。
_WINDOW_HTML = """<div class='file-preview'>
<div class='file-preview-bar'>
<span class='file-preview-name'>__FILENAME__</span>
<span class='file-preview-kind'>__KIND__</span>
<a href='__FILE_URL__' target='_blank' rel='noopener'>在新窗口打开原文件</a>
<a href='__DOWNLOAD_URL__'>下载原文件</a>
</div>
<p class='note'>这份文件有 __SIZE__，超过整本预览预算，改为按片显示以免页面卡死。要看完整内容请用上面的链接下载或在新窗口打开。</p>
<div class='file-preview-window-bar'>
<button type='button' id='file-window-prev'>上一片</button>
<span id='file-window-info' class='muted'>正在载入…</span>
<button type='button' id='file-window-next'>下一片</button>
</div>
<pre id='file-preview-window' class='src-body'></pre>
<p id='file-preview-error' class='file-preview-error' hidden></p>
</div>
<script>
(function () {
  var url = '__WINDOW_URL__';
  var piece = __WINDOW_BYTES__;
  var stage = document.getElementById('file-preview-window');
  var info = document.getElementById('file-window-info');
  var prev = document.getElementById('file-window-prev');
  var next = document.getElementById('file-window-next');
  var error = document.getElementById('file-preview-error');
  var offset = 0;
  var prevAt = 0;
  var nextAt = 0;
  function fail(message) {
    if (error) {
      error.textContent = '按片读取失败：' + message + '。可以直接用上边的「在新窗口打开原文件」查看。';
      error.hidden = false;
    }
  }
  function kb(n) { return (n / 1024).toFixed(1) + ' KB'; }
  function show(data) {
    stage.textContent = data.text || '';
    offset = data.offset;
    info.textContent = kb(data.offset) + ' / ' + kb(data.total) + '（编码 ' + data.encoding + '）';
    prevAt = data.prev_offset;
    nextAt = data.next_offset;
    prev.disabled = !data.has_prev;
    next.disabled = !data.has_more;
  }
  function load(at) {
    fetch(url + '?offset=' + at, { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(show)
      .catch(function (e) { fail(e && e.message ? e.message : String(e)); });
  }
  // 翻页只认服务端给的字节位置：一片可能因多字节字符回退几字节，
  // 不能用固定片长去猜，否则起点会落在字符中间。
  prev.addEventListener('click', function () { load(prevAt); });
  next.addEventListener('click', function () { load(nextAt); });
  load(0);
})();
</script>"""


_DOWNLOAD_ONLY_HTML = """<div class='file-preview'>
<div class='file-preview-bar'>
<span class='file-preview-name'>__FILENAME__</span>
<span class='file-preview-kind'>__KIND__</span>
<a href='__FILE_URL__' target='_blank' rel='noopener'>在新窗口打开原文件</a>
<a href='__DOWNLOAD_URL__'>下载原文件</a>
</div>
<p class='note'>这份文件有 __SIZE__，超过浏览器整本预览的体量上限，已停用在线预览以免页面卡死。请用上面的链接打开或下载。</p>
</div>"""


def _human_size(size: int) -> str:
    size = max(0, int(size or 0))
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size / 1024:.0f} KB"


def preview_body(sid: str, filename: str, size: int = 0) -> str:
    """详情页「文件预览」这一段的 HTML。

    小文件交给随包的离线渲染器整本渲染；超过体量预算的文件按片读取，
    服务端只发一片、前端只排版一片，整本书不再进入浏览器 DOM。
    """
    safe_name = esc(str(filename or "未命名文件"))
    suffix = suffix_of(filename)
    kind = esc(suffix.upper() if suffix else "文件")
    file_url = f"/admin/api/sources/{esc(str(sid))}/file"
    # 下载链接另带一个参数，避免和预览链接拼出两个 ?。
    download_url = file_url + "?download=1"
    if document_view.needs_window(size, suffix):
        return (
            _WINDOW_HTML.replace("__FILENAME__", safe_name)
            .replace("__KIND__", kind)
            .replace("__FILE_URL__", file_url)
            .replace("__DOWNLOAD_URL__", download_url)
            .replace("__WINDOW_URL__", f"/admin/api/sources/{esc(str(sid))}/text-window")
            .replace("__WINDOW_BYTES__", str(document_view.WINDOW_BYTES))
            .replace("__SIZE__", _human_size(size))
        )
    if document_view.needs_download_only(size, suffix):
        return (
            _DOWNLOAD_ONLY_HTML.replace("__FILENAME__", safe_name)
            .replace("__KIND__", kind)
            .replace("__FILE_URL__", file_url)
            .replace("__DOWNLOAD_URL__", download_url)
            .replace("__SIZE__", _human_size(size))
        )
    return (
        _PREVIEW_HTML.replace("__FILENAME__", safe_name)
        .replace("__KIND__", kind)
        .replace("__DOWNLOAD_URL__", download_url)
        .replace("__FILE_URL__", file_url)
        .replace("__SCRIPT_URL__", SCRIPT_URL)
        .replace("__WORKER_URL__", PDF_WORKER_URL)
    )
