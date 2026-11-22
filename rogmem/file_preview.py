"""文件资料的原文预览：磁盘上的原文件 + 随包带上的离线渲染器。

预览用的开源库见 vendor/preview/REVISION。它完全在浏览器里跑，不把文件传到
任何外部服务；服务端只做两件事：把扫描目录里的原始字节按 inline 发回，以及
把 vendor/preview 里的静态文件发出去。
"""

from __future__ import annotations

from html import escape as esc
from pathlib import Path

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


def preview_body(sid: str, filename: str) -> str:
    """详情页「文件预览」这一段的 HTML。"""
    safe_name = esc(str(filename or "未命名文件"))
    suffix = suffix_of(filename)
    kind = esc(suffix.upper() if suffix else "文件")
    file_url = f"/admin/api/sources/{esc(str(sid))}/file"
    # 下载链接另带一个参数，避免和预览链接拼出两个 ?。
    download_url = file_url + "?download=1"
    return (
        _PREVIEW_HTML.replace("__FILENAME__", safe_name)
        .replace("__KIND__", kind)
        .replace("__DOWNLOAD_URL__", download_url)
        .replace("__FILE_URL__", file_url)
        .replace("__SCRIPT_URL__", SCRIPT_URL)
        .replace("__WORKER_URL__", PDF_WORKER_URL)
    )
