"""HTTP service: health, admin, enroll, and stateless MCP."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import hashlib
import inspect
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from rogmem import auth, cost, document_view, engine, file_preview, rate_limit, ui
from rogmem.version import product_version

mcp = FastMCP("rogmem", stateless_http=True, json_response=True, host="0.0.0.0")
# Core and the door check do not use the default pool. A stuck opening read must not be what /healthz waits on.
_TOOL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="rogmem-tool")
_GATE_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="rogmem-gate")
_CORE_DEADLINE = 8
_COOKIE = "rogmem_admin"
CURRENT = contextvars.ContextVar("agent", default=None)
# The plugin version the calling host sent. Empty means an old plugin, or a call that is not a hook.
PLUGIN_REPORTED = contextvars.ContextVar("plugin_reported", default="")
PLUGIN_HEADER = "X-Rogmem-Plugin"


def _secret() -> bytes:
    path = Path(os.environ["ROGMEM_DATA"]) / "cookie.key"
    if not path.exists():
        path.write_bytes(os.urandom(32))
        os.chmod(path, 0o600)
    generation = engine.get_setting("admin_password_generation") or "0"
    return hashlib.sha256(path.read_bytes() + b"|" + generation.encode()).digest()


def _password_bypass() -> bool:
    raw = os.environ.get("ROGMEM_DATA", "").strip()
    if not raw:
        return False
    return auth.allow_password_bypass(Path(raw))


def _admin_ok(request: Request) -> bool:
    """管理页和 /admin/api/* 在需要口令时才查登录会话。

    免密登录默认开启，首次进入不检查口令。用户在基础设置里设了密码，或关掉免密，
    管理写操作才检查口令。临时目录里的 ROGMEM_NO_PASSWORD=1 仍是测试口。
    /healthz、/plugin.json、/mcp、/host-bootstrap、/host-client.whl 不经过这里。
    """
    if _password_bypass():
        return True
    try:
        if not engine.admin_password_required():
            return True
    except (KeyError, OSError, RuntimeError):
        return False
    token = request.cookies.get(_COOKIE, "")
    if not token:
        return False
    try:
        return auth.verify_cookie(_secret(), token) == "ok"
    except (KeyError, OSError):
        return False


def _login_page() -> str:
    return ui.login_page()


_CSRF_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _normalized_origin(url: str) -> tuple[str, str, int] | None:
    """Scheme + hostname + effective port，低大小写归一（评审 R2 P2-9）。
    返回 None 表示 URL 不规范（空、无 scheme、带 userinfo 等），应直接拒绝。
    """
    from urllib.parse import urlsplit

    if not url:
        return None
    try:
        p = urlsplit(url)
        scheme = (p.scheme or "").lower()
        if scheme not in {"http", "https"}:
            return None
        if p.username or p.password:
            # 带 userinfo 的 Origin 一律视作异常，拒绝
            return None
        host = (p.hostname or "").lower()
        if not host:
            return None
        # .port 对非数字或超出 0–65535 的端口抛 ValueError，按无法辨认拒绝。
        port = p.port if p.port is not None else (443 if scheme == "https" else 80)
    except (ValueError, UnicodeError):
        return None
    return (scheme, host, port)


def _same_origin_or_safe(request: Request) -> bool:
    """CSRF 防线（不是身份认证）。

    管理台经常从本机门口或极空间远程入口打开。反代改写 Host 之后，浏览器的
    Origin 和中枢自己的地址不是同一个，本页按钮会被误判成跨站。

    浏览器在 Sec-Fetch-Site 里写的是页面和这次请求的关系，页面改不了这个头。
    只有 same-origin 表示请求就从这页发出。同一父域的另一个源是 same-site，
    仍然拒绝。不读 X-Forwarded-Host：这个头客户端自己就能填，不拿来放行。

    没有这个头时，仍要求 Origin 或 Referer 与中枢自己的地址一致。作业端点在
    两者都没有时拒绝。非浏览器在可信网络里伪造请求头，不在这道防线里。
    """
    if request.method not in _CSRF_METHODS:
        return True
    origin_hdr = request.headers.get("origin", "").strip()
    if origin_hdr and origin_hdr.lower() == "null":
        return False
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site == "same-origin":
        return True
    if site in {"cross-site", "same-site"}:
        return False
    expected = _normalized_origin(str(request.base_url))
    if expected is None:
        return False
    referer_hdr = request.headers.get("referer", "").strip()
    if origin_hdr:
        got = _normalized_origin(origin_hdr)
        return got is not None and got == expected
    if referer_hdr:
        got = _normalized_origin(referer_hdr)
        return got is not None and got == expected
    if request.url.path.startswith("/admin/api/hosts/jobs"):
        return False
    return True


def _device(request: Request) -> str | None:
    return request.headers.get("x-rogmem-device")


def _viewing_computer(request: Request) -> tuple[str, str]:
    """The computer opening this page, when the request can prove it.

    The local door sends the saved identity. A page opened straight at the hub
    has no such header; a private address that this computer just proved with
    its own key is the same computer. A tunnel or a public proxy is not.
    """
    from rogmem.device_identity import read_device_name_header

    viewer_id = (_device(request) or "").strip()
    viewer_name = read_device_name_header(request.headers.get("x-rogmem-device-name") or "")
    if not viewer_id and request.client is not None:
        viewer_id = engine.viewer_for_peer(request.client.host or "")
    return viewer_id, viewer_name


def _requested_device_name(request: Request, extra: str = "") -> str:
    header = (request.headers.get("x-rogmem-device-name") or "").strip()
    return engine.resolve_device_name(header or extra or None)


def _bearer(request: Request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    return engine.agent_by_token(header[7:], _device(request))


def _mark_channel(response: Response) -> Response:
    ui.CHANNEL.set(True)
    response.set_cookie("rogmem_channel", "1", path="/", samesite="lax")
    return response


def _graph_label_flags() -> tuple[bool, int]:
    """记忆星图和结晶页关联图谱共用这一组设置，不另做开关。"""
    return engine.graph_auto_labels(), engine.graph_label_budget()


def _graph_document() -> str:
    """The console front door. Remote open and /admin/graph render this same page."""
    auto_labels, label_budget = _graph_label_flags()
    return ui.graph_page(auto_labels=auto_labels, label_budget=label_budget)


async def channel_home(request: Request) -> Response:
    """Remote open loads /. The graph is the admin door, so it needs the password session."""
    if not _admin_ok(request):
        return RedirectResponse("/admin/login", status_code=302)
    ui.CHANNEL.set(True)
    return _mark_channel(HTMLResponse(_graph_document()))


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request) -> Response:
    return await channel_home(request)


@mcp.custom_route("/index.html", methods=["GET"])
async def index_html(request: Request) -> Response:
    return await channel_home(request)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> Response:
    # On the event loop, from memory. A core call must not be what keeps this from answering.
    return JSONResponse(engine.health_snapshot())


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics_prometheus(_request: Request) -> Response:
    """Prometheus text exposition. May touch the DB; not a liveness probe."""
    from rogmem import metrics as metrics_mod

    snap = await asyncio.to_thread(metrics_mod.snapshot, engine.db())
    body = metrics_mod.render_prometheus(snap)
    return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")


@mcp.custom_route("/metrics.json", methods=["GET"])
async def metrics_json(_request: Request) -> Response:
    from rogmem import metrics as metrics_mod

    snap = await asyncio.to_thread(metrics_mod.snapshot, engine.db())
    return JSONResponse(snap)


@mcp.custom_route("/admin/api/diag/write-lock", methods=["GET"])
async def write_lock_diag(request: Request) -> Response:
    """谁开着写事务、开了多久、开在哪一行；收口/自愈过几次。只读内存，不碰数据库。"""
    denied = _guard(request)
    if denied:
        return denied
    return JSONResponse(engine.tx_health())


def _client_response(payload: bytes | None) -> Response:
    from rogmem.host_client import bundle_filename

    if not payload:
        return JSONResponse({"ok": False, "error": "宿主客户端不在这个服务里"}, status_code=404)
    return Response(
        payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle_filename()}"',
            "Cache-Control": "no-store",
        },
    )


@mcp.custom_route("/host-client", methods=["GET"])
async def host_client(request: Request) -> Response:
    return await host_client_whl(request)


@mcp.custom_route("/host-client.whl", methods=["GET"])
async def host_client_whl(_request: Request) -> Response:
    """The computer being enrolled installs this wheel from the same origin as /healthz."""
    from rogmem.host_client import bundle_bytes

    payload = await asyncio.to_thread(bundle_bytes)
    return _client_response(payload)


@mcp.custom_route("/plugin.json", methods=["GET"])
async def plugin_manifest(_request: Request) -> Response:
    """The plugin version this hub serves. Public: it says nothing a host could not read from the wheel."""
    from rogmem import plugin

    from rogmem.host_client import bundle_bytes

    manifest = plugin.hub_manifest(
        engine.health_snapshot().get("hub") or "", engine.current_plugin_label(), engine.plugin_known_labels()
    )
    wheel = await asyncio.to_thread(bundle_bytes)
    if wheel:
        import hashlib

        # A computer checks the wheel it downloaded against this before installing it.
        manifest["wheel_sha256"] = hashlib.sha256(wheel).hexdigest()
        manifest["wheel_bytes"] = len(wheel)
    return JSONResponse(manifest, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/host-bootstrap", methods=["GET"])
async def host_bootstrap(_request: Request) -> Response:
    """Stdlib installer. It runs before python -m rogmem exists."""
    from rogmem import host_bootstrap as bootstrap

    text = Path(bootstrap.__file__).read_text(encoding="utf-8")
    return Response(text, media_type="text/x-python; charset=utf-8", headers={"Cache-Control": "no-store"})


@mcp.custom_route("/client/open-browser", methods=["GET"])
async def open_browser(request: Request) -> Response:
    """NAS cannot open the visitor's browser. The local installer proxy does that before forwarding."""
    del request
    return Response(status_code=204)


@mcp.custom_route("/enroll/apply", methods=["POST"])
async def enroll_apply(request: Request) -> Response:
    data = await request.json()
    result = engine.enroll_apply(
        data["device_id"],
        engine.resolve_device_name(data.get("device_name")),
        data["program"],
        data["pubkey"],
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 403)


@mcp.custom_route("/enroll/reclaim", methods=["POST"])
async def enroll_reclaim(request: Request) -> Response:
    data = await request.json()
    result = engine.enroll_reclaim(
        str(data.get("pubkey") or ""),
        str(data.get("device_id") or ""),
        str(data.get("program") or ""),
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 403)


@mcp.custom_route("/enroll/redeem", methods=["POST"])
async def enroll_redeem(request: Request) -> Response:
    data = await request.json()
    presented = str(data.get("ticket") or "").strip()
    if presented:
        # A new client redeems with the whole ticket. The hub re-checks its own
        # signature and binds the grant to that ticket, so a forged entrance
        # policy or a swapped credential cannot be replayed here. Legacy clients
        # still post only the bare grant, which the engine validates by hash.
        from rogmem import admission

        ticket = admission.verify(presented, admission.hub_key())
        if ticket is None:
            return JSONResponse({"ok": False, "error": "票据无效或已过期"}, status_code=403)
        if ticket.secret != str(data.get("grant") or "").strip():
            return JSONResponse({"ok": False, "error": "凭证与票据不一致"}, status_code=403)
    result = engine.redeem_enroll_grant(
        str(data.get("grant") or ""),
        str(data.get("device_id") or ""),
        engine.resolve_device_name(data.get("device_name")),
        str(data.get("program") or ""),
        str(data.get("pubkey") or ""),
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 403)


@mcp.custom_route("/admin/api/enroll-grants", methods=["POST"])
async def admin_enroll_grant(request: Request) -> Response:
    """Mint one self-contained admission ticket, rendered once, on the hub.

    The entrance policy is derived here — from the request that reached the
    hub, an operator-declared public origin and the local door — and signed.
    The browser no longer contributes ``location.origin``: that address
    describes the computer which opened this page, not the one being enrolled.
    """
    if not _admin_ok(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not _same_origin_or_safe(request):
        return JSONResponse({"error": "跨站请求"}, status_code=403)
    from rogmem import admission
    from rogmem.onboard import onboard_prompt

    minted = await asyncio.to_thread(engine.create_enroll_grant)
    identity = await asyncio.to_thread(engine.hub_id)
    entrances = admission.derive_entrances(str(request.base_url))
    ticket = admission.AdmissionTicket(
        hub=identity,
        secret=minted["secret"],
        expires_at=minted["expires_at"],
        grant_id=minted["id"],
        entrances=entrances,
    )
    token = admission.encode(ticket, admission.hub_key())
    canonical = entrances[0].url if entrances else str(request.base_url).rstrip("/")
    prompt = admission.render_prompt(
        onboard_prompt(minted["secret"]),
        token,
        identity,
        minted["secret"],
        canonical,
        entrances,
    )
    return JSONResponse(
        {
            "ok": True,
            "id": minted["id"],
            "secret": minted["secret"],
            "expires_at": minted["expires_at"],
            "hub": identity,
            "ticket": token,
            "entrances": [item.url for item in entrances],
            "prompt": prompt,
        }
    )


@mcp.custom_route("/enroll/claim", methods=["POST"])
async def enroll_claim(request: Request) -> Response:
    data = await request.json()
    result = engine.enroll_claim(data["pubkey"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 403)


@mcp.custom_route("/admin/login", methods=["GET", "POST"])
async def admin_login(request: Request) -> Response:
    if request.method == "GET":
        # 已经登录就进记忆星图。还没有会话时停在口令页，不再跳过口令。
        if _admin_ok(request):
            return RedirectResponse("/admin/graph", status_code=302)
        return HTMLResponse(_login_page())
    data = await _body(request)
    password = str(data.get("password") or "")
    if not engine.admin_login(password):
        return JSONResponse({"ok": False, "error": "口令不对"}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        _COOKIE,
        auth.sign_cookie(_secret(), "ok"),
        httponly=True,
        samesite="lax",
        path="/",
        max_age=86400 * 14,
    )
    return response


@mcp.custom_route("/admin", methods=["GET"])
async def admin_home(_request: Request) -> Response:
    # 默认入口进"记忆星图"（t205）。深链 /admin/<page> 自身路由不走这里。
    return RedirectResponse("/admin/graph", status_code=302)


def _guard(request: Request) -> Response | None:
    if _admin_ok(request):
        return None
    if request.url.path.startswith("/admin/api/"):
        return JSONResponse({"error": "未登录"}, status_code=401)
    return RedirectResponse("/admin/login", status_code=302)


@mcp.custom_route("/admin/sources", methods=["GET"])
async def admin_sources(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    tab = request.query_params.get("tab", "session")
    filters = {
        key: request.query_params.get(key, "")
        for key in ("q", "kind", "device", "agent", "project", "status", "session", "turn")
    }
    try:
        page_no = int(request.query_params.get("page") or "1")
    except ValueError:
        page_no = 1
    return await asyncio.to_thread(_sources_response, tab, filters, page_no)


def _sources_response(tab: str, filters: dict, page_no: int) -> HTMLResponse:
    listed = engine.list_sources(
        tab,
        query=filters["q"],
        kind=filters["kind"],
        device=filters["device"],
        agent=filters["agent"],
        project=filters["project"],
        status=filters["status"],
        session=filters["session"],
        turn=filters["turn"],
        page=page_no,
    )
    return HTMLResponse(ui.sources_page(listed["rows"], tab, filters, engine.source_facets(tab), listed))


@mcp.custom_route("/admin/api/sources/latest", methods=["GET"])
async def sources_latest(request: Request) -> Response:
    """Whether the top of the list moved. One marker on a slow beat, never a whole page.

    The page keeps its own baseline and only raises a flag; it never redraws itself here.
    """
    denied = _guard(request)
    if denied:
        return denied
    filters = {
        key: request.query_params.get(key, "")
        for key in ("q", "kind", "device", "agent", "project", "session", "turn")
    }
    probe = await asyncio.to_thread(
        engine.source_latest_probe,
        request.query_params.get("tab", "session"),
        filters["q"],
        filters["kind"],
        filters["device"],
        filters["agent"],
        filters["project"],
        filters["session"],
        filters["turn"],
    )
    return JSONResponse(probe)


def _import_receipt(item: dict) -> dict:
    """A committed import is a list row. A duplicate already has its row."""
    receipt = dict(item)
    source_id = str(item.get("id") or "")
    if not source_id or item.get("duplicate"):
        return receipt
    row = engine.db().execute(
        """SELECT id, kind, filename, device_name, agent_name, project_ref, ingested_at,
                  status, pipeline, last_error, redacted, coverage
           FROM sources WHERE id=?""",
        (source_id,),
    ).fetchone()
    if row is None:
        return receipt
    shown = dict(row)
    shown["note"] = engine.list_note(shown)
    shown["progress"] = engine.source_stage_note(shown.get("pipeline") or "", shown.get("status") or "")
    shown["active"] = engine.source_is_active(shown.get("pipeline") or "", shown.get("status") or "")
    receipt["row_html"] = ui.source_row_html(shown, "file")
    receipt["active"] = shown["active"]
    return receipt


def _too_large_receipt() -> JSONResponse:
    item = {"ok": False, "error": "文件太大"}
    return JSONResponse({"ok": False, "error": "文件太大", "imported": [item]}, status_code=413)


def _land_upload(name: str, upload, label: str) -> dict:
    """Put one upload on disk and make its row at once. The read waits for the reader thread.

    The bytes are streamed to a hidden staged file, so a large document never lands in
    memory and the request does not hold the read lock while it travels.
    """
    safe = Path(name).name
    suffix = Path(safe).suffix.lower().lstrip(".")
    if not safe or suffix not in engine.accepted_suffixes():
        return {"ok": False, "error": "不接受的文件类型", "filename": safe}
    root = engine.active_scan()
    if root is None:
        return {"ok": False, "error": "没有扫描目录", "filename": safe}
    root.mkdir(parents=True, exist_ok=True)
    staged = root / f".part-{secrets.token_hex(8)}"
    digest = hashlib.sha256()
    try:
        handle = getattr(upload, "file", None)
        if handle is None:
            return {"ok": False, "error": "没有文件", "filename": safe}
        handle.seek(0)
        total = 0
        with staged.open("wb") as out:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > engine.DOCUMENT_MAX_BYTES:
                    return {"ok": False, "error": "文件太大", "filename": safe}
                digest.update(chunk)
                out.write(chunk)
        return _import_receipt(engine.queue_staged(safe, staged, digest.hexdigest(), label))
    finally:
        staged.unlink(missing_ok=True)


@mcp.custom_route("/admin/api/sources/import", methods=["POST"])
async def import_sources(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    raw_length = request.headers.get("content-length", "")
    if raw_length.isdigit() and int(raw_length) > engine.DOCUMENT_MAX_BYTES + 65536:
        return _too_large_receipt()
    form = await request.form()
    uploads = form.getlist("files")
    if not uploads:
        return JSONResponse({"ok": False, "error": "没有文件"}, status_code=400)
    label = _requested_device_name(request, str(form.get("device_name") or ""))
    imported = []
    for upload in uploads:
        name = getattr(upload, "filename", "") or ""
        imported.append(await asyncio.to_thread(_land_upload, name, upload, label))
    failed = [item for item in imported if not item.get("ok")]
    return JSONResponse({"ok": not failed or any(item.get("ok") for item in imported), "imported": imported})


def _nas_http(result: dict) -> JSONResponse:
    if "imported" in result:
        copied = dict(result)
        copied["imported"] = [_import_receipt(item) for item in result["imported"]]
        return JSONResponse(copied)
    if result.get("ok") or result.get("error") == "极空间目录还没挂进容器":
        return JSONResponse(result)
    return JSONResponse(result, status_code=400)


@mcp.custom_route("/admin/api/sources/nas", methods=["GET"])
async def list_import_nas(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    raw_offset = request.query_params.get("offset", "")
    try:
        offset = int(raw_offset) if str(raw_offset).strip() else 0
    except ValueError:
        offset = 0
    result = await asyncio.to_thread(engine.list_nas, request.query_params.get("path", ""), offset)
    return _nas_http(result)


@mcp.custom_route("/admin/api/sources/import-nas", methods=["POST"])
async def import_from_nas(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    has_paths = "paths" in data
    has_dir = "dir" in data
    if has_paths == has_dir:
        return JSONResponse({"ok": False, "error": "没有文件"}, status_code=400)
    label = _requested_device_name(request, str(data.get("device_name") or ""))
    if has_paths:
        paths = data.get("paths")
        if not isinstance(paths, list) or not paths:
            return JSONResponse({"ok": False, "error": "没有文件"}, status_code=400)
        result = await asyncio.to_thread(engine.import_nas, paths, label)
    else:
        folder = data.get("dir")
        if not isinstance(folder, str):
            return JSONResponse({"ok": False, "error": "没有文件"}, status_code=400)
        result = await asyncio.to_thread(engine.import_nas_dir, folder, label)
    return _nas_http(result)


@mcp.custom_route("/admin/api/sources/progress", methods=["GET"])
async def sources_progress(request: Request) -> Response:
    """The rows the file list is watching. One renderer paints both the list and this."""
    denied = _guard(request)
    if denied:
        return denied
    ids = [part for part in (request.query_params.get("ids") or "").split(",") if part]
    rows = await asyncio.to_thread(engine.source_rows_by_ids, ids)
    painted = []
    for row in rows:
        item = dict(row)
        origin = "file" if item.get("kind") == "file" else "session"
        item["row_html"] = ui.source_row_html(item, origin)
        item["active"] = bool(row.get("active"))
        item.pop("preview", None)
        painted.append(item)
    return JSONResponse({"rows": painted})


@mcp.custom_route("/admin/sources/{sid}", methods=["GET"])
async def admin_source(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    sid = request.path_params["sid"]
    # 详情页要读全文、算覆盖与命运；放进工作线程，慢材料不能按住事件循环。
    detail = await asyncio.to_thread(engine.source_detail, sid)
    if not detail:
        return HTMLResponse("没有这条", status_code=404)
    origin = request.query_params.get("from", "")
    source = detail["source"]
    source["_part"] = request.query_params.get("part", "")
    source["_cite"] = request.query_params.get("cite", "")
    source["_file_size"] = await asyncio.to_thread(_source_file_size, sid)
    return HTMLResponse(ui.source_detail(source, detail["statements"], detail["mate"], origin))


def _source_file_path(sid: str) -> Path | None:
    """把 source id 解析到扫描目录里的原件。只认文件资料与白名单后缀。"""
    row = engine.db().execute(
        "SELECT kind, filename FROM sources WHERE id=?", (sid,)
    ).fetchone()
    if row is None or (row["kind"] or "") != "file":
        return None
    filename = Path(str(row["filename"] or "")).name
    if not filename or file_preview.suffix_of(filename) not in engine.accepted_suffixes():
        return None
    root = engine.active_scan()
    if root is None:
        return None
    base = root.resolve()
    target = (base / filename).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        return None
    return target


def _source_file_size(sid: str) -> int:
    target = _source_file_path(sid)
    try:
        return target.stat().st_size if target else 0
    except OSError:
        return 0


@mcp.custom_route("/admin/api/sources/{sid}/file", methods=["GET"])
async def source_file_bytes(request: Request) -> Response:
    """文件资料的原始字节。预览、新窗口打开、下载共用这一处。"""
    denied = _guard(request)
    if denied:
        return denied
    sid = str(request.path_params.get("sid") or "")

    def read() -> tuple[str, bytes, str] | None:
        target = _source_file_path(sid)
        if target is None or target.stat().st_size > engine.DOCUMENT_MAX_BYTES:
            return None
        media = file_preview.mime_for(target.name)
        if media.startswith("text/") and "charset=" in media:
            # 原件可能是 GBK：只改 charset 声明，不改字节，下载仍是原始文件。
            media = media.split(";", 1)[0] + "; charset=" + document_view.sniff_encoding(target)
        return target.name, target.read_bytes(), media

    found = await asyncio.to_thread(read)
    if found is None:
        return JSONResponse({"error": "找不到这份文件的原件"}, status_code=404)
    filename, data, media = found
    disposition = "attachment" if request.query_params.get("download") == "1" else "inline"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    return Response(data, media_type=media, headers=headers)


@mcp.custom_route("/admin/api/sources/{sid}/text-window", methods=["GET"])
async def source_text_window(request: Request) -> Response:
    """大文件按片读取：一次只把一片读进内存并发给浏览器。"""
    denied = _guard(request)
    if denied:
        return denied
    sid = str(request.path_params.get("sid") or "")
    offset = request.query_params.get("offset", "0")
    limit = request.query_params.get("limit", str(document_view.WINDOW_BYTES))

    def read() -> dict | None:
        target = _source_file_path(sid)
        if target is None or target.stat().st_size > engine.DOCUMENT_MAX_BYTES:
            return None
        if file_preview.suffix_of(target.name) not in document_view.TEXT_SUFFIXES:
            return None
        return document_view.read_window(target, offset, limit)

    found = await asyncio.to_thread(read)
    if found is None:
        return JSONResponse({"error": "找不到这份文件的原件"}, status_code=404)
    return JSONResponse(found)


@mcp.custom_route("/admin/api/crystals/nav", methods=["GET"])
async def crystal_nav(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    type_name = request.query_params.get("type", "")
    status = request.query_params.get("status", "active") or "active"
    try:
        offset = int(request.query_params.get("offset") or "0")
    except ValueError:
        offset = 0
    return JSONResponse(engine.crystal_nav_slice(type_name, status, offset))


@mcp.custom_route("/admin/crystals", methods=["GET"])
async def admin_crystals(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return await asyncio.to_thread(_crystal_page, request, "owner")


@mcp.custom_route("/admin/api/crystals/{cid}/body", methods=["POST"])
async def save_crystal_body(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    cid = request.path_params["cid"]
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else None
    chapters = data.get("chapters") if isinstance(data.get("chapters"), dict) else None
    if "markdown" in data:
        address = data.get("address") if "address" in data else None
        result = engine.save_crystal_markdown(
            cid, str(data.get("markdown") or ""), data.get("sketch"), address=address
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)
    if fields is not None or chapters is not None:
        result = engine.save_crystal_fields(cid, fields or {}, chapters or {})
    else:
        result = engine.save_crystal_article(cid, str(data.get("text") or ""))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/crystals/{cid}", methods=["GET"])
async def admin_crystal(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return await asyncio.to_thread(_crystal_page, request, request.path_params["cid"])


def _open_matches_nav_filter(detail: dict, type_name: str, status: str) -> bool:
    """The page already open belongs in this filter. A blank type is every type."""
    if type_name and (detail.get("type") or "") != type_name:
        return False
    if status and status != "全部" and (detail.get("status") or "") != status:
        return False
    return True


def crystal_nav_decision(detail: dict, type_name: str, status: str) -> dict:
    """Nav for the open page. A filter that excludes it jumps to a page that matches.

    The old path pinned the open page onto every result, so choosing 组织 while
    reading a 人物 still left that 人物 at the top and looked like the type was ignored.
    """
    window = engine.crystal_nav_slice(type_name, status, 0)
    nav = list(window["items"])
    if any(item["id"] == detail["id"] for item in nav):
        return {"nav": nav, "window": window, "redirect": ""}
    if _open_matches_nav_filter(detail, type_name, status):
        nav.insert(0, {"id": detail["id"], "type": detail["type"], "name": detail["name"], "status": detail["status"]})
        return {"nav": nav, "window": window, "redirect": ""}
    if nav:
        return {"nav": nav, "window": window, "redirect": nav[0]["id"]}
    return {"nav": [], "window": window, "redirect": ""}


def _crystal_filter_location(cid: str, type_name: str, status: str, view: str) -> str:
    from urllib.parse import urlencode

    params = {}
    if type_name:
        params["type"] = type_name
    if status and status != "active":
        params["status"] = status
    if view and view not in ("", "body"):
        params["view"] = view
    query = urlencode(params)
    return f"/admin/crystals/{cid}" + (f"?{query}" if query else "")


def _crystal_page(request: Request, cid: str) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    type_name = request.query_params.get("type", "")
    status = request.query_params.get("status", "active") or "active"
    view = request.query_params.get("view", "body") or "body"
    editing = request.query_params.get("edit") == "1"
    if editing:
        view = "body"
    detail = engine.crystal_detail(cid)
    if not detail:
        return HTMLResponse("没有这篇", status_code=404)
    decision = crystal_nav_decision(detail, type_name, status)
    if decision["redirect"]:
        return RedirectResponse(
            _crystal_filter_location(decision["redirect"], type_name, status, view),
            status_code=302,
        )
    nav = decision["nav"]
    window = decision["window"]
    revs = engine.revision_index(detail["id"]) if view == "log" else []
    auto_labels, label_budget = _graph_label_flags()
    return HTMLResponse(
        ui.crystal_page(
            detail,
            revs,
            nav,
            type_name,
            status,
            view,
            {"total": window["total"], "next": window["next"]},
            editing,
            auto_labels=auto_labels,
            label_budget=label_budget,
        )
    )


@mcp.custom_route("/admin/hosts", methods=["GET"])
async def admin_hosts(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    filters = {
        "device": request.query_params.get("device", ""),
        "agent": request.query_params.get("agent", ""),
    }
    viewer_id, viewer_name = _viewing_computer(request)

    def render() -> str:
        fleet = engine.list_connected_hosts(
            viewer_device_id=viewer_id,
            viewer_device_name=viewer_name,
            device=filters["device"],
            agent=filters["agent"],
        )
        return ui.hosts_page(engine.list_hosts(), fleet, filters)

    return HTMLResponse(await asyncio.to_thread(render))


@mcp.custom_route("/admin/api/hosts/progress", methods=["GET"])
async def hosts_progress(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    viewer_id = _device(request) or ""
    viewer_name = request.headers.get("x-rogmem-device-name") or ""

    def load() -> dict:
        fleet = engine.list_connected_hosts(viewer_device_id=viewer_id, viewer_device_name=viewer_name)
        return {
            "rows": [
                {
                    "device_id": row.get("device_id") or "",
                    "program": row.get("program") or "",
                    "imported": int(row.get("history_count") or 0),
                }
                for row in fleet.get("rows") or []
            ]
        }

    return JSONResponse(await asyncio.to_thread(load))


@mcp.custom_route("/admin/api/hosts/report", methods=["POST"])
async def admin_hosts_report(request: Request) -> Response:
    if not _admin_ok(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    data = await request.json()
    items = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return JSONResponse({"error": "没有宿主清单"}, status_code=400)
    return JSONResponse(engine.report_hosts(items))


@mcp.custom_route("/admin/api/hosts/connect", methods=["POST"])
async def admin_hosts_connect(request: Request) -> Response:
    if not _admin_ok(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    data = await request.json()
    program = str(data.get("program") or "") if isinstance(data, dict) else ""
    confirm = data.get("confirm") is True if isinstance(data, dict) else False
    result = engine.confirm_host(program, confirm)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@mcp.custom_route("/admin/api/hosts/import", methods=["POST"])
async def admin_hosts_import(request: Request) -> Response:
    if not _admin_ok(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    data = await request.json()
    program = str(data.get("program") or "") if isinstance(data, dict) else ""
    confirm = data.get("confirm") is True if isinstance(data, dict) else False
    result = engine.confirm_import(program, confirm)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@mcp.custom_route("/admin/api/hosts/jobs", methods=["POST"])
async def admin_hosts_jobs_create(request: Request) -> Response:
    if not _admin_ok(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not _same_origin_or_safe(request):
        return JSONResponse({"ok": False, "refused": "cross-origin"}, status_code=403)
    data = await request.json()
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "refused": "bad-request"}, status_code=400)
    agent_id = str(data.get("agent_id") or "").strip()
    program = str(data.get("program") or "").strip()
    request_id = str(data.get("request_id") or "").strip() or f"admin:{int(time.time()*1000)}"
    if not agent_id or not program:
        return JSONResponse({"ok": False, "refused": "missing-agent-or-program"}, status_code=400)
    result = await asyncio.to_thread(
        engine.create_host_import_job,
        agent_id=agent_id,
        program=program,
        request_id=request_id,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/host/presence", methods=["POST"])
async def host_presence(request: Request) -> Response:
    """The computer's door says which machine it is. Last-used stays untouched."""
    header = request.headers.get("authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    device = await asyncio.to_thread(engine.device_for_key, token, _device(request))
    if not device:
        return JSONResponse({"ok": False}, status_code=401)
    peer = request.client.host if request.client is not None else ""
    engine.note_viewer_peer(device, peer)
    return JSONResponse({"ok": True})


@mcp.custom_route("/admin/api/hosts/jobs", methods=["GET"])
async def admin_hosts_jobs_list(request: Request) -> Response:
    if not _admin_ok(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    agent_id = request.query_params.get("agent_id", "") or None
    device_id = request.query_params.get("device_id", "") or None
    program = request.query_params.get("program", "") or None
    status = request.query_params.get("status", "") or None
    rows = await asyncio.to_thread(
        engine.list_host_import_jobs,
        agent_id=agent_id, device_id=device_id, program=program, status=status,
    )
    return JSONResponse({"ok": True, "jobs": rows})


@mcp.custom_route("/admin/api/hosts/jobs/{job_id}/cancel", methods=["POST"])
async def admin_hosts_jobs_cancel(request: Request) -> Response:
    if not _admin_ok(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    if not _same_origin_or_safe(request):
        return JSONResponse({"ok": False, "refused": "cross-origin"}, status_code=403)
    job_id = request.path_params["job_id"]
    result = await asyncio.to_thread(engine.cancel_host_import_job, job_id=job_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/host/jobs/{job_id}/upload", methods=["POST"])
async def host_job_upload(request: Request) -> Response:
    """受作业与 lease 约束的上传入口。

    评审 PR #116 §3 与 §4：
    - 必须带 Bearer 对应本 agent（通过 BearerGate 已装入 CURRENT）。
    - 必须 Header `X-Rogmem-Lease-Token` 与 job 当前 lease 完全匹配。
    - job 必须是本 agent 的，且处于 claimed/running/partial。
    - 服务器调用 engine.ingest_history 真实入库并回真实计数；
      客户端不能自报 posted。
    - 若 job 已 cancelled/expired/terminal，整批拒绝。
    """
    agent = CURRENT.get()
    if agent is None:
        return JSONResponse({"error": "未授权"}, status_code=401)
    job_id = request.path_params["job_id"]
    lease_token = request.headers.get("x-rogmem-lease-token", "")
    data = await request.json()
    turns = data.get("turns") or [] if isinstance(data, dict) else []
    label = _requested_device_name(request, str((data or {}).get("device_name") or ""))
    result = await asyncio.to_thread(
        engine.ingest_history_for_job,
        agent_id=str(agent["id"]),
        job_id=job_id,
        lease_token=lease_token,
        program=str((data or {}).get("program") or ""),
        turns=turns,
        device_name=label,
    )
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@mcp.custom_route("/admin/graph", methods=["GET"])
async def admin_graph(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return HTMLResponse(_graph_document())


@mcp.custom_route("/admin/api/graph", methods=["GET"])
async def admin_graph_api(request: Request) -> Response:
    if not _admin_ok(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(engine.graph))


def _log_page(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    try:
        page_no = int(request.query_params.get("page") or "1")
    except ValueError:
        page_no = 1
    log = engine.operation_log_view(request.query_params.get("kind", ""), page_no)
    return HTMLResponse(
        ui.logs_page(
            log["entries"],
            log["kinds"],
            log["selected"],
            engine.get_setting("alert_webhook"),
            page=int(log.get("page") or 1),
            total=int(log["total"]) if log.get("total") is not None else len(log["entries"]),
            page_size=log.get("page_size"),
        )
    )


@mcp.custom_route("/admin/settings", methods=["GET"])
async def admin_settings(request: Request) -> Response:
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_log_page, request)


@mcp.custom_route("/admin/settings/about", methods=["GET"])
async def admin_about(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return HTMLResponse(ui.about_page(product_version(), engine.release_view(), engine.current_plugin_label()))


@mcp.custom_route("/admin/settings/models", methods=["GET"])
async def admin_models(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return HTMLResponse(ui.models_page(engine.public_providers(), engine.assignments(), engine.llm_public()))


@mcp.custom_route("/admin/settings/agents", methods=["GET"])
async def admin_agents(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return RedirectResponse("/admin/hosts", status_code=302)


@mcp.custom_route("/admin/settings/basic", methods=["GET"])
async def admin_basic(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    cfg = engine.basic_config()
    cfg.update(engine.collect_config())
    return HTMLResponse(ui.basic_page(cfg))


@mcp.custom_route("/admin/settings/pipeline", methods=["GET"])
async def admin_pipeline(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return HTMLResponse(
        ui.pipeline_page(
            engine.public_providers(),
            engine.assignments(),
            engine.llm_public(),
            engine.pipeline_config(),
            engine.pipeline_knobs_view(),
        )
    )


@mcp.custom_route("/admin/settings/queue", methods=["GET"])
async def admin_queue(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    from rogmem.queue_board import list_view, view

    try:
        page_no = int(request.query_params.get("page") or "1")
    except ValueError:
        page_no = 1
    span = request.query_params.get("span", "24h")
    tab = request.query_params.get("tab", "all")
    headers = {"Cache-Control": "no-store"}
    if request.query_params.get("part") == "queue":
        listed = await asyncio.to_thread(list_view, span, tab, page_no)
        return HTMLResponse(ui.queue_list_fragment(listed), headers=headers)
    board = await asyncio.to_thread(lambda: view(span, tab, page_no, sync=False))
    return HTMLResponse(ui.queue_page(board), headers=headers)


@mcp.custom_route("/admin/settings/collect", methods=["GET"])
async def admin_collect(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return RedirectResponse("/admin/settings/basic", status_code=302)


def _cost_page_no(value: str) -> int:
    try:
        return int(value or "1")
    except ValueError:
        return 1


_COST_FILTER_KEYS = ("span", "detail", "worker", "model", "dworker", "dmodel")


def _cost_page(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    from datetime import datetime, timezone

    query = request.query_params
    url_mode = any(key in query for key in _COST_FILTER_KEYS)
    stored = {} if url_mode else (engine.cost_filters() or {})

    def pick(key: str, default: str) -> str:
        if url_mode:
            return str(query.get(key) or default)
        return str(stored.get(key) or default)

    view = {
        "now": datetime.now(timezone.utc),
        "span": pick("span", "30d"),
        "worker": pick("worker", ""),
        "model": pick("model", ""),
        "detail": pick("detail", "30d"),
        "dworker": pick("dworker", ""),
        "dmodel": pick("dmodel", ""),
        "page": _cost_page_no(str(query.get("page") or "")),
    }
    headers = {"Cache-Control": "no-store"}
    if query.get("part") == "calls":
        listed = cost.call_board(
            now=view["now"],
            detail=str(query.get("detail") or "30d"),
            dworker=str(query.get("dworker") or ""),
            dmodel=str(query.get("dmodel") or ""),
            page=view["page"],
        )
        listed["span"] = str(query.get("span") or "30d")
        listed["worker"] = str(query.get("worker") or "")
        listed["model"] = str(query.get("model") or "")
        return HTMLResponse(ui.cost_calls_fragment(listed), headers=headers)
    wants_overview = query.get("part") == "overview"
    wants_status = (
        not wants_overview
        and (request.query_params.get("observe") == "1" or request.headers.get(ui.COST_OBSERVE_HEADER) == "1")
    )
    board, prices = cost.board_and_prices(**view)
    obs = board.get("observation") if isinstance(board.get("observation"), dict) else {}
    # A switch asks for a view that is not published yet. The document may wait
    # for that gather. A status probe must not: it only reports the current
    # observation, and waiting here would hold the worker the next page needs.
    # Full first paint may wait briefly. Partial card refresh returns immediately so the
    # UI can paint a pending shell and finish via observe, without blocking the worker.
    if (
        obs.get("state") == "pending"
        and cost.VIEW_WAIT_SECONDS > 0
        and not wants_status
        and not wants_overview
    ):
        cost.wait_for_view(cost.view_key(**view), cost.VIEW_WAIT_SECONDS)
        board, prices = cost.board_and_prices(**view)
        obs = board.get("observation") if isinstance(board.get("observation"), dict) else {}
    if wants_status:
        obs = board.get("observation") if isinstance(board.get("observation"), dict) else {}
        state = str(obs.get("state") or ("pending" if board.get("observed") is False else "ready"))
        return JSONResponse(
            {
                "state": state,
                "epoch": obs.get("epoch"),
                "as_of": obs.get("as_of"),
                "observed": state == "ready",
            },
            headers=headers,
        )
    if wants_overview:
        return JSONResponse(
            ui.cost_overview_payload(board, engine.public_providers(), prices),
            headers=headers,
        )
    # Call list is independent of card observation. Pending cards must not hide rows.
    board = cost.with_call_page(
        board,
        now=view["now"],
        detail=view["detail"],
        dworker=view["dworker"],
        dmodel=view["dmodel"],
        page=view["page"],
    )
    return HTMLResponse(ui.cost_page(board, engine.public_providers(), prices), headers=headers)


@mcp.custom_route("/admin/costs", methods=["GET"])
async def admin_costs(request: Request) -> Response:
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_cost_page, request)


@mcp.custom_route("/admin/api/costs/filters", methods=["POST"])
async def save_cost_filters(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "成本筛选不是一份清单"}, status_code=400)
    result = engine.save_cost_filters(data)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/costs", methods=["POST"])
async def save_costs(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return JSONResponse({"ok": False, "error": "价格不是一份清单"}, status_code=400)
    try:
        cost.save_book(data.get("rate"), items)
    except cost.BadBook as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


@mcp.custom_route("/admin/api/agents/approve-device", methods=["POST"])
async def approve_device(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await request.json()
    device_id = str(data.get("device_id") or "") if isinstance(data, dict) else ""
    result = engine.approve_device(device_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/agents/{aid}/rename", methods=["POST"])
async def rename_agent(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await request.json()
    name = str(data.get("display_name") or "") if isinstance(data, dict) else ""
    result = engine.rename_agent(request.path_params["aid"], name)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/agents/{aid}/approve", methods=["POST"])
async def approve(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return JSONResponse(engine.enroll_approve(request.path_params["aid"]))


@mcp.custom_route("/admin/api/agents/{aid}/disable", methods=["POST"])
async def disable(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    engine.disable_agent(request.path_params["aid"])
    return JSONResponse({"ok": True})


@mcp.custom_route("/admin/api/agents/{aid}/enable", methods=["POST"])
async def enable(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = engine.enable_agent(request.path_params["aid"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/sweep", methods=["POST"])
async def admin_sweep(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    return JSONResponse(await asyncio.to_thread(engine.sweep))


@mcp.custom_route("/admin/api/crystals/{cid}/revisions", methods=["GET"])
async def admin_revisions(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    # List endpoint stays on the thin index. One reading uses /revisions/{rid}.
    return JSONResponse(engine.revision_index(request.path_params["cid"]))


@mcp.custom_route("/admin/api/crystals/{cid}/revision-cards", methods=["GET"])
async def admin_revision_cards(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    cid = request.path_params["cid"]
    try:
        offset = max(0, int(request.query_params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(request.query_params.get("limit") or ui.REVISION_LOG_PAGE)
    except (TypeError, ValueError):
        limit = ui.REVISION_LOG_PAGE
    limit = max(1, min(limit, 100))
    detail = engine.crystal_detail(cid)
    if not detail:
        return HTMLResponse("没有这篇", status_code=404)
    newest_first = list(reversed(engine.revision_index(cid)))
    page = newest_first[offset : offset + limit]
    return HTMLResponse(
        ui.revision_cards_html(cid, page, name=detail.get("name") or "", start_index=offset)
    )


@mcp.custom_route("/admin/api/crystals/{cid}/revisions/{rid}", methods=["GET"])
async def admin_revision_detail(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    try:
        revision_id = int(request.path_params["rid"])
    except (TypeError, ValueError):
        return HTMLResponse("没有这一条", status_code=404)
    rows = engine.revisions(request.path_params["cid"], revision_id)
    if not rows:
        return HTMLResponse("没有这一条", status_code=404)
    from rogmem.ui import revision_detail

    return HTMLResponse(revision_detail(rows[0]))


@mcp.custom_route("/admin/api/crystals/{cid}/rollback", methods=["POST"])
async def admin_rollback(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    revision = data.get("revision_id")
    return JSONResponse(engine.rollback(request.path_params["cid"], int(revision) if revision else None))


@mcp.custom_route("/admin/api/sources/{sid}", methods=["GET"])
async def admin_source_snapshot(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    snap = engine.source_snapshot(request.path_params["sid"])
    if not snap:
        return JSONResponse({"ok": False, "error": "没有这条"}, status_code=404)
    return JSONResponse({"ok": True, **snap})


@mcp.custom_route("/admin/api/sources/{sid}/origin", methods=["GET"])
async def admin_source_origin(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    origin = engine.resolve_source_origin(request.path_params["sid"])
    status = 200 if origin.get("ok") else 404
    return JSONResponse(origin, status_code=status)


@mcp.custom_route("/admin/api/sources/{sid}/retry", methods=["POST"])
async def admin_retry(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = await asyncio.to_thread(engine.retry_source, request.path_params["sid"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/sources/{sid}/rebench", methods=["POST"])
async def admin_rebench(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = engine.rebench_source(request.path_params["sid"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/agents/{aid}/reject", methods=["POST"])
async def reject(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = engine.reject_agent(request.path_params["aid"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/agents/{aid}/delete", methods=["POST"])
async def delete_agent(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = engine.delete_agent(request.path_params["aid"])
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


async def _body(request: Request) -> dict:
    if request.headers.get("content-type", "").startswith("application/json"):
        return await request.json()
    return {}


@mcp.custom_route("/admin/api/settings/models", methods=["POST"])
async def save_model(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    result = engine.save_provider(
        str(data.get("name", "")),
        str(data.get("base_url", "")),
        str(data.get("api_key", "")),
        str(data.get("models", "")),
        str(data.get("kind") or ""),
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/settings/models/key", methods=["POST"])
async def save_model_key(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    result = engine.set_provider_key(str(data.get("id", "")), str(data.get("api_key", "")))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/settings/models/delete", methods=["POST"])
async def remove_model(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    return JSONResponse(engine.delete_provider(str(data.get("id", ""))))


@mcp.custom_route("/admin/api/settings/assign", methods=["POST"])
async def assign_models(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = engine.assign_models(await _body(request))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/settings/embed", methods=["POST"])
async def save_embed(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    result = engine.select_embed_model(str(data.get("model") or ""))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/settings/effective", methods=["GET"])
async def effective_model(request: Request) -> Response:
    if not _admin_ok(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    return JSONResponse(engine.llm_public())


@mcp.custom_route("/admin/api/settings/webhook", methods=["POST"])
async def save_webhook(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    result = engine.save_webhook(str(data.get("url") or ""))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/password", methods=["POST"])
async def change_password(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    data = await _body(request)
    new = str(data.get("password") or "")
    confirm = None if "confirm" not in data else str(data.get("confirm") or "")
    result = engine.change_password(str(data.get("current") or ""), new, confirm)
    response = JSONResponse(result, status_code=200 if result.get("ok") else 400)
    if result.get("ok"):
        response.set_cookie(
            _COOKIE,
            auth.sign_cookie(_secret(), "ok"),
            httponly=True,
            samesite="lax",
            path="/",
            max_age=86400 * 14,
        )
    return response


@mcp.custom_route("/admin/api/settings/basic", methods=["POST"])
async def save_basic(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = engine.save_basic(await _body(request))
    response = JSONResponse(result, status_code=200 if result.get("ok") else 400)
    if result.get("sessions_reset"):
        response.delete_cookie(_COOKIE, path="/")
    return response


@mcp.custom_route("/admin/api/settings/knobs", methods=["POST"])
async def save_pipeline_knobs(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = engine.save_pipeline_knobs(await _body(request))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/settings/pipeline", methods=["POST"])
async def save_pipeline(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = engine.save_pipeline(await _body(request))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/settings/collect", methods=["POST"])
async def save_collect(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = engine.save_collect(await _body(request))
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/settings/release-refresh", methods=["POST"])
async def refresh_release_now(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    result = await asyncio.to_thread(engine.refresh_release)
    view = result.get("view") or {}
    result["html"] = ui._about_line_html(view)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/settings/release-view", methods=["GET"])
async def view_release(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    view = await asyncio.to_thread(engine.release_view)
    return JSONResponse({"ok": True, "view": view, "html": ui._about_line_html(view)})


@mcp.custom_route("/admin/api/settings/release-source", methods=["POST"])
async def set_release_source(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    body = await _body(request)
    url = str(body.get("url") or "").strip()
    result = await asyncio.to_thread(engine.set_release_source_url, url)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@mcp.custom_route("/admin/api/history", methods=["POST"])
async def import_history(request: Request) -> Response:
    denied = _guard(request)
    if denied:
        return denied
    body = await _body(request)
    label = _requested_device_name(request, str(body.get("device_name") or ""))
    projects = body.get("projects") or []
    turns = body.get("turns") or []
    if turns:
        result = await asyncio.to_thread(
            engine.ingest_history,
            turns,
            str(body.get("program") or "历史导入"),
            label,
        )
    else:
        result = {"ok": True, "imported": [], "duplicates": 0, "refused": 0}
    if projects:
        bound = await asyncio.to_thread(engine.bind_session_projects, projects)
        result["projects"] = int(bound.get("updated") or 0)
    return JSONResponse(result)


@mcp.custom_route("/admin/api/pump", methods=["POST"])
async def pump(request: Request) -> Response:
    if os.environ.get("ROGMEM_TEST_PUMP") != "1":
        return JSONResponse({"error": "没有这个接口"}, status_code=404)
    denied = _guard(request)
    if denied:
        return denied
    engine.scan_dir()
    engine.ensure_pending_vectors()
    return JSONResponse({"next": engine.queue_next_lane(), "done": engine.pump_one()})


@mcp.custom_route("/admin/static/preview/{path:path}", methods=["GET"])
async def preview_asset(request: Request) -> Response:
    """文件预览的随包渲染器，离线自带不走 CDN。只放行 vendor/preview 里的文件。"""
    denied = _guard(request)
    if denied:
        return denied
    rel = str(request.path_params.get("path") or "")
    try:
        target = (file_preview.ASSET_ROOT / rel).resolve()
    except OSError:
        return Response(status_code=404)
    if not rel or not target.is_file() or not target.is_relative_to(file_preview.ASSET_ROOT):
        return Response(status_code=404)
    return Response(
        target.read_bytes(),
        media_type=file_preview.asset_media_type(target),
        headers={"Cache-Control": "private, max-age=3600"},
    )


@mcp.custom_route("/admin/static/three/{path:path}", methods=["GET"])
async def three_module(request: Request) -> Response:
    """登录页的 3D 视效自带 three.js，不走 CDN。只放行 vendor/three 里的文件。"""
    rel = str(request.path_params.get("path") or "")
    root = (Path(__file__).parent / "vendor" / "three").resolve()
    try:
        target = (root / rel).resolve()
    except OSError:
        return Response(status_code=404)
    if not rel or not target.is_file() or not target.is_relative_to(root):
        return Response(status_code=404)
    media = "text/javascript" if target.suffix == ".js" else "application/octet-stream"
    return Response(target.read_bytes(), media_type=media, headers={"Cache-Control": "no-cache"})


_GRAPH_AUDIO_ROOT = (Path(__file__).parent / "vendor" / "audio").resolve()


@mcp.custom_route("/admin/static/audio/{name}", methods=["GET"])
async def graph_audio(request: Request) -> Response:
    """全站随包自带的 BGM：白名单 + 本地缓存，曲目不变就不再回传整首。"""
    name = str(request.path_params.get("name") or "")
    if name not in ui.GRAPH_AUDIO_FILES:
        return Response(status_code=404)
    try:
        target = (_GRAPH_AUDIO_ROOT / name).resolve()
    except OSError:
        return Response(status_code=404)
    if not target.is_file() or not target.is_relative_to(_GRAPH_AUDIO_ROOT):
        return Response(status_code=404)
    etag, last_modified, _ = ui.graph_audio_fingerprint()
    if not etag:
        etag = '"' + hashlib.sha256(target.read_bytes()).hexdigest() + '"'
        last_modified = ""
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "ETag": etag,
    }
    if last_modified:
        headers["Last-Modified"] = last_modified
    # 协商缓存：内容没变就只回 304，不重传整首曲子。
    none_match = request.headers.get("if-none-match", "")
    if none_match == etag or (
        not none_match and last_modified
        and request.headers.get("if-modified-since") == last_modified
    ):
        return Response(status_code=304, headers=headers)
    return Response(target.read_bytes(), media_type="audio/mpeg", headers=headers)


def _count_memory_read() -> None:
    agent = CURRENT.get()
    if agent is None:
        return
    try:
        engine.note_memory_call(str(agent["id"]))
    except Exception as exc:  # 计数是记账，读本身已经成功，不能因为它报错
        print(f"ROGMEM_CALL_COUNT_SKIPPED {type(exc).__name__}: {exc}", flush=True)


def off_loop(fn):
    """Run a sync tool off the request thread. Memory work must not stall the admin page."""
    signature = inspect.signature(fn)

    def settled(*args, **kwargs):
        failed = True
        try:
            result = fn(*args, **kwargs)
            failed = False
            return result
        finally:
            # 工具跑在线程池里，线程会被下一个请求复用。它开着的写事务不能跟着线程活下去。
            engine.settle_thread_transaction(f"tool:{fn.__name__}", failed=failed)

    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        return await asyncio.to_thread(functools.partial(settled, *args, **kwargs))

    wrapped.__signature__ = signature
    return wrapped


def _run_core(budget: int, project: str, before: str) -> str:
    failed = True
    try:
        payload = engine.core_memory(engine.core_budget_for(budget), project, before=before)
        failed = False
    finally:
        engine.settle_thread_transaction("tool:core", failed=failed)
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
async def core(budget: int = 0, project: str = "", before: str = "") -> str:
    """装入历史包。budget 为 0 时用基础设置里的新会话字数。本对话已有成功返回时禁止再调，除非 before 是上次给出的续读位置。"""
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_TOOL_POOL, _run_core, budget, project, before),
            timeout=_CORE_DEADLINE,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError("core timed out") from exc
    # 读取计数靠请求上下文里的 CURRENT。run_in_executor 不带这个上下文，
    # 所以计数改在工具这一层发出，走 to_thread：既拿到智能体，也不把写库压回事件循环。
    await asyncio.to_thread(_count_memory_read)
    return _with_plugin_verdict(result)


def _with_plugin_verdict(result: str) -> str:
    """Every packet says whether this host's plugin is the hub's. Behind → the agent tells the person.

    Only a call that carries the plugin header gets a verdict: an MCP tool call made by the
    agent itself carries none, and the hook's own report already rode on the opening call.
    """
    from rogmem import plugin

    reported = PLUGIN_REPORTED.get()
    agent = CURRENT.get()
    if not reported or agent is None:
        return result
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return result
    if not isinstance(payload, dict):
        return result
    status = plugin.compare(reported, plugin.version())
    installed = engine.plugin_label_of(reported) or plugin.OLD_LABEL
    latest = engine.current_plugin_label()
    payload["plugin"] = {
        "installed": installed,
        "latest": latest,
        "installed_id": reported,
        "latest_id": plugin.version(),
        "status": status,
        "update": plugin.UPDATE_COMMAND,
        "notice": plugin.notice(status, installed, latest, str(agent["program"] or "")),
    }
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
@off_loop
def search(query: str, limit: int = 5, trace: bool = False) -> str:
    """检索结晶和原文。身份/全文/图谱/时间多路按相关度合并；问密钥、法条或字段值时只返回原文。

    trace=True 时附带各路命中、cap 与 RRF/证据计数诊断，默认关闭。
    """
    payload = engine.search(query, limit, trace=bool(trace))
    _count_memory_read()
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
@off_loop
def read_section(crystal: str, section: str) -> str:
    """读这一页正文里的一节。节名是该页的二级标题。对不上时，返回里的 sections 是这一页实际有的标题。

    段末的〔源 …〕是这段的原文编号，source_marks 给出每个编号对应的来源；要原话或细节时用 read_source 读它。
    """
    payload = engine.read_section(crystal, section)
    _count_memory_read()
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
@off_loop
def read_chain(crystal: str, section: str) -> str:
    """按这一页上的一节，取出先后写下的说法和每一环记下的修改理由。节名与 read_section 相同。"""
    payload = engine.read_chain(crystal, section)
    _count_memory_read()
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
@off_loop
def read_source(source_id: str) -> str:
    """按来源编号读原文。编号可以是完整编号，也可以是正文〔源 …〕里的短编号。"""
    wanted = engine.resolve_source_mark(source_id) or source_id
    row = engine.db().execute("SELECT id, body, redacted, status, last_error FROM sources WHERE id=?", (wanted,)).fetchone()
    _count_memory_read()
    if not row:
        return json.dumps({"error": "没有这条"}, ensure_ascii=False)
    return json.dumps({"id": row["id"], "body": row["body"], "redacted": row["redacted"], "status": row["status"], "last_error": row["last_error"]}, ensure_ascii=False)


@mcp.tool()
@off_loop
def submit_turn(
    session_id: str,
    turn_id: str,
    seq: int,
    role: str,
    text: str,
    event_at: str = "",
    project: str = "",
    event_basis: str = "",
) -> str:
    """提交用户输入或看得见的回复。"""
    agent = CURRENT.get()
    if agent is None:
        return json.dumps({"error": "未授权"}, ensure_ascii=False)
    kind = {"user": "user", "assistant": "agent", "summary": "summary"}.get(role, "agent")
    lane = "slow" if kind == "summary" else "fast"
    return json.dumps(
        engine.submit_text(
            kind=kind,
            text=text,
            session_id=session_id,
            turn_id=turn_id,
            seq=seq,
            agent=agent,
            event_at=event_at or None,
            project=project or None,
            lane=lane,
            event_basis=event_basis,
        ),
        ensure_ascii=False,
    )


@mcp.tool()
@off_loop
def read_revisions(crystal: str) -> str:
    """按名称或编号读取一篇结晶的修订记录，顺着变更看它为什么是现在这样。"""
    if CURRENT.get() is None:
        return json.dumps({"error": "未授权"}, ensure_ascii=False)
    payload = engine.revisions_for_agent(crystal)
    _count_memory_read()
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
@off_loop
def pending_host_import_jobs() -> str:
    """认领本机历史导入作业。只认 Authorization 身份对应的 agent，拒绝越权。"""
    agent = CURRENT.get()
    if agent is None:
        return json.dumps({"error": "未授权"}, ensure_ascii=False)
    claimed = engine.claim_host_import_jobs(agent_id=str(agent["id"]))
    public = [
        {
            "job_id": item["id"],
            "program": item["program"],
            "request_id": item["request_id"],
            "lease_until": item["lease_until"],
            "lease_token": item["lease_token"],
            "max_attempts": item["max_attempts"],
            "attempts": item["attempts"],
        }
        for item in claimed
    ]
    return json.dumps({"ok": True, "jobs": public}, ensure_ascii=False)


@mcp.tool()
@off_loop
def ack_host_import_job(
    job_id: str,
    lease_token: str,
    status: str,
    posted: int = 0,
    duplicates: int = 0,
    refused: int = 0,
    failed: int = 0,
    last_error_class: str = "",
) -> str:
    """回传作业结果。必须带 claim 时服务端签发的 lease_token。"""
    agent = CURRENT.get()
    if agent is None:
        return json.dumps({"error": "未授权"}, ensure_ascii=False)
    result = engine.ack_host_import_job(
        agent_id=str(agent["id"]),
        job_id=job_id,
        lease_token=lease_token,
        status=status,
        posted=int(posted or 0),
        duplicates=int(duplicates or 0),
        refused=int(refused or 0),
        failed=int(failed or 0),
        last_error_class=last_error_class,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
@off_loop
def renew_host_import_lease(job_id: str, lease_token: str, lease_s: int = 600) -> str:
    """续租。必须带 claim 时签发的 lease_token。"""
    agent = CURRENT.get()
    if agent is None:
        return json.dumps({"error": "未授权"}, ensure_ascii=False)
    result = engine.renew_host_import_lease(
        agent_id=str(agent["id"]),
        job_id=job_id,
        lease_token=lease_token,
        lease_s=int(lease_s or 600),
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
@off_loop
def announce_capabilities(capabilities: str) -> str:
    """客户端上报自身能力。老 client 从不调用这一步，capabilities 保持空字符串。"""
    agent = CURRENT.get()
    if agent is None:
        return json.dumps({"error": "未授权"}, ensure_ascii=False)
    result = engine.set_agent_capabilities(agent_id=str(agent["id"]), capabilities=capabilities)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
@off_loop
def submit_draft(markdown: str) -> str:
    """提交一页和首蒸相同的 Markdown 草稿。篇名必须对上已有结晶。收下后插队进入并页、速写，不改正在用的正文和速写。"""
    agent = CURRENT.get()
    if agent is None:
        return json.dumps({"error": "未授权"}, ensure_ascii=False)
    payload = engine.submit_draft(markdown, agent)
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def guide(level: str = "brief") -> str:
    """读取怎么用这份记忆。"""
    brief = (
        "本对话还没有 core 的成功返回时，必须先 core，读完再回答。已经有了，绝对禁止再调不带 before 的 core。"
        "历史包是人物速写、工程速写，余量才是最近摘录。返回写明截断时，要更早原文必须按 continuation.before 再调 core，这次只有更早摘录。"
        "这一问的事实包里没有，才 search，绝对禁止用 search 翻页。要读某一节，用 read_section，节名是这一页正文里的二级标题；对不上时看返回的 sections。"
        "要看这一节先后怎么变的、每次为什么改，用 read_chain，节名与那一节相同。"
        "页面是摘要，段末的〔源 …〕是原文编号，决定需要原话或细节时用 read_source(编号) 读原文。整页为什么变成现在这样，用 read_revisions。"
        "要改已经有的一页，用 submit_draft，交一页和首蒸相同的 Markdown，篇名对上那一页。不按这个形状写的不会收下，收下也不直接改正在用的正文和速写。本场原话优先于历史包。"
    )
    full = brief + " submit_turn 只收用户句和看得见的回复。系统提示、召回、压缩摘要和隐藏推理不要提交。"
    return full if level == "full" else brief


_BEARER_PROTECTED_PREFIXES = ("/mcp", "/host/jobs/")


class BearerGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and any(
            scope.get("path", "").startswith(prefix)
            for prefix in _BEARER_PROTECTED_PREFIXES
        ):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            token = headers.get("authorization", "")
            device = headers.get("x-rogmem-device")
            agent = None
            if token.startswith("Bearer "):
                agent = await asyncio.get_running_loop().run_in_executor(
                    _GATE_POOL, engine.agent_by_token, token[7:], device
                )
            if agent is not None:
                try:
                    client = scope.get("client")
                    peer = str(client[0] or "") if client else ""
                    engine.note_viewer_peer(str(agent["device_id"] or ""), peer)
                except Exception:
                    pass
                reported = headers.get(PLUGIN_HEADER.lower(), "")
                PLUGIN_REPORTED.set(reported)
                if reported:
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            _GATE_POOL, engine.note_plugin_version, str(agent["id"]), reported
                        )
                    except Exception:
                        pass
            if agent is None:
                await JSONResponse({"error": "未授权"}, status_code=401)(scope, receive, send)
                return
            CURRENT.set(agent)
        await self.app(scope, receive, send)


def _client_ip(scope) -> str:
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])
    return "unknown"


def _rate_bucket(method: str, path: str) -> tuple[str, str] | None:
    if method != "POST":
        return None
    if path == "/enroll/apply":
        return "enroll-apply", "enroll"
    if path in {"/enroll/claim", "/enroll/reclaim", "/enroll/redeem"}:
        return "enroll-claim", "enroll"
    if path == "/admin/login":
        return "admin-login", "admin"
    if path.startswith("/admin/api/"):
        return "admin-write", "admin"
    return None


class RateGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            bucket = _rate_bucket(scope.get("method", ""), scope.get("path", ""))
            if bucket is not None:
                name, kind = bucket
                limit, window = rate_limit.configured_quota(kind)
                if not rate_limit.allow(f"{name}:{_client_ip(scope)}", limit, window):
                    response = JSONResponse({"error": "太频繁"}, status_code=429, headers={"Retry-After": str(window)})
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class ChannelMark:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            ui.CHANNEL.set(_channel_cookie(scope))
        await self.app(scope, receive, send)


def _channel_cookie(scope) -> bool:
    for key, value in scope.get("headers") or []:
        if key.lower() != b"cookie":
            continue
        for part in value.decode("latin1").split(";"):
            name, _, raw = part.strip().partition("=")
            if name == "rogmem_channel" and raw == "1":
                return True
    return False


def _login_asset(path: str) -> bool:
    """登录页在还没有会话时也要能画出 3D 视效。

    three.js 子集不要求登录：路径由静态路由再校验一次，不越出 vendor/three。
    """
    if path.startswith("/admin/static/three/"):
        rest = path[len("/admin/static/three/"):]
        return bool(rest) and ".." not in rest.split("/")
    return False


class AdminGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path != "/admin/login" and not _login_asset(path) and (path == "/admin" or path.startswith("/admin/")):
                # 门口这一问要读口令代次、读 cookie 钥匙，`_secret()` 还要碰文件系统。
                # 直接在这里调就是把整条事件循环按在磁盘上：一次读卡住，连不经过这道门
                # 的 `/healthz` 和读另一本账的 `/admin/costs` 都得跟着等。放到线程里问。
                if not await asyncio.to_thread(_admin_ok, Request(scope)):
                    if path.startswith("/admin/api/"):
                        response = JSONResponse({"error": "未登录"}, status_code=401)
                    else:
                        response = RedirectResponse("/admin/login", status_code=302)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def build_app():
    app = mcp.streamable_http_app()
    return ChannelMark(RateGate(AdminGate(BearerGate(app))))


def _poll_seconds() -> float:
    return float(os.environ.get("ROGMEM_POLL", "0.2"))


# 暂停时 worker 不再按 0.2 秒空转一圈（那会让三个 worker 每秒撞库十几次）。
# 慢转一圈只为看开关有没有被抬起，抬起后最多等这么久就接着干。
_PAUSED_POLL_SECONDS = 1.0


# The console may change the concurrency while the workers are already running, and
# threads are spawned once. So the live limit is an admission count held around one
# model call, not a thread count: a worker that finds no slot waits a tick and looks again.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_WORKERS = 0


def _admit_distill(limit: int) -> bool:
    """Take one running slot. False means this worker waits a tick."""
    global _ACTIVE_WORKERS
    with _ACTIVE_LOCK:
        if _ACTIVE_WORKERS >= max(1, int(limit)):
            return False
        _ACTIVE_WORKERS += 1
        return True


def _release_distill() -> None:
    global _ACTIVE_WORKERS
    with _ACTIVE_LOCK:
        _ACTIVE_WORKERS = max(0, _ACTIVE_WORKERS - 1)


def _distill_loop(stop: threading.Event | None = None) -> None:
    """One unit per round, and always hand the interpreter back between rounds.

    A lease succeeds whenever the queue is deep, so "more work -> go straight
    round again" turned this into a spin: the worker held the GIL and kept
    re-taking engine.LOCK, and every request thread queued up behind it until
    the whole console looked hung. One unit is one model call, so pausing like
    the probe and reconcile loops do costs nothing worth having.

    The workers are spawned once, so a concurrency changed in the console is an
    admission count, not a thread count: lowering it takes effect on the next round.

    Paused, the whole pipeline stands still: no unit is taken, and the worker stops
    hot-polling. It wakes on a slow tick only to see whether the switch came back up,
    so a paused hub is idle instead of hammering the database fifteen times a second.
    """

    while stop is None or not stop.is_set():
        if engine.distill_paused():
            wait = _PAUSED_POLL_SECONDS
            if stop is None:
                time.sleep(wait)
            elif stop.wait(wait):
                break
            continue
        if _admit_distill(engine.distill_concurrency()):
            try:
                engine.pump_one()
            except Exception as exc:
                # A fault that one unit cannot survive must not take the worker with it: a
                # silent thread death drops concurrency from three to two and nobody is told.
                print(f"ROGMEM_DISTILL {type(exc).__name__} {exc}", flush=True)
            finally:
                _release_distill()
        if stop is None:
            time.sleep(_poll_seconds())
        elif stop.wait(_poll_seconds()):
            break


def start_distill_workers(n: int | None = None, stop: threading.Event | None = None) -> list[threading.Thread]:
    count = engine.distill_concurrency() if n is None else max(1, n)
    threads = []
    for i in range(count):
        thread = threading.Thread(
            target=_distill_loop,
            kwargs={"stop": stop},
            daemon=True,
            name=f"rogmem-distill-{i}",
        )
        thread.start()
        threads.append(thread)
    return threads


# A step that fails waits this long before another try. The maintenance loop
# itself keeps polling, so a locked database cannot spin the log or end the thread.
_SCHEDULE_BACKOFF = 30.0
_PROBE_THREAD: threading.Thread | None = None
_PROBE_LOCK = threading.Lock()
_RECONCILE_THREAD: threading.Thread | None = None
_RECONCILE_LOCK = threading.Lock()


# 维护时钟每 0.2 秒转一圈，但每一步都要走一张表并取 engine.LOCK。一步一圈的结果
# 是锁被占掉近一半时间，管理台的每次读都要在锁后面排队（实测每一圈 11-13 次、
# 每次约 0.35 秒）。每一步有自己的最小间隔：作业照样收，锁不再被一圈一圈占住。
_MAINTENANCE_EVERY = {
    "scan": 5.0,
    "release": 20.0,
    "sweep": 20.0,
    "host_import": 15.0,
    "maintain": 30.0,
}
_MAINTENANCE_LAST: dict[str, float] = {}


def _maintenance_steps():
    """Resolved when the tick runs, so a step sees the engine as it is now.

    Reconcile is not in this list. A scan that never returns must not be what
    keeps same-name pages from being folded.
    """

    return (
        ("scan", lambda: engine.scan_dir(force=False)),
        ("release", lambda: engine.probe_release_if_due()),
        ("sweep", lambda: engine.run_scheduled_sweep()),
        ("host_import", lambda: engine.sweep_host_import_jobs()),
        ("maintain", lambda: engine.maintain()),
    )


def _schedule_backoff() -> float:
    return _SCHEDULE_BACKOFF


def _run_guarded(name: str, fn, quiet_until: dict[str, float]) -> None:
    """One scheduled step. A failure is remembered, then the caller moves on.

    The quiet window stops a step that raises every time from being called
    again on the next 0.2s tick. Other steps keep their own windows.
    """

    now = time.monotonic()
    if now < quiet_until.get(name, 0.0):
        return
    try:
        fn()
    except Exception as exc:
        quiet_until[name] = now + _schedule_backoff()
        print(f"ROGMEM_SCHEDULE {name} {type(exc).__name__}", flush=True)


def run_probe_tick(quiet_until: dict[str, float] | None = None) -> None:
    _run_guarded("probe", engine.probe_providers_if_due, quiet_until if quiet_until is not None else {})


def run_maintenance_tick(quiet_until: dict[str, float] | None = None) -> None:
    window = quiet_until if quiet_until is not None else {}
    now = time.monotonic()
    for name, fn in _maintenance_steps():
        if now - _MAINTENANCE_LAST.get(name, 0.0) < _MAINTENANCE_EVERY.get(name, 15.0):
            continue
        _MAINTENANCE_LAST[name] = now
        _run_guarded(name, fn, window)


def _probe_loop(stop: threading.Event | None = None) -> None:
    quiet: dict[str, float] = {}
    while stop is None or not stop.is_set():
        try:
            run_probe_tick(quiet)
        except Exception as exc:
            quiet["probe"] = time.monotonic() + _schedule_backoff()
            print(f"ROGMEM_SCHEDULE probe {type(exc).__name__}", flush=True)
        if stop is None:
            time.sleep(_poll_seconds())
        elif stop.wait(_poll_seconds()):
            break


def start_probe_loop(stop: threading.Event | None = None) -> threading.Thread:
    """Probe on its own thread. A stuck scan, sweep, or reconcile cannot pause it.

    A private loop (one that is given stop) does not replace the process loop.
    The process loop is started again when its thread has exited.
    """

    global _PROBE_THREAD
    if stop is not None:
        thread = threading.Thread(
            target=_probe_loop,
            kwargs={"stop": stop},
            daemon=True,
            name="rogmem-probe",
        )
        thread.start()
        return thread
    with _PROBE_LOCK:
        if _PROBE_THREAD is not None and _PROBE_THREAD.is_alive():
            return _PROBE_THREAD
        thread = threading.Thread(
            target=_probe_loop,
            kwargs={"stop": None},
            daemon=True,
            name="rogmem-probe",
        )
        _PROBE_THREAD = thread
        thread.start()
        return thread


def run_reconcile_tick(quiet_until: dict[str, float] | None = None) -> None:
    _run_guarded("reconcile", engine.run_scheduled_reconcile, quiet_until if quiet_until is not None else {})


def _reconcile_loop(stop: threading.Event | None = None) -> None:
    quiet: dict[str, float] = {}
    while stop is None or not stop.is_set():
        try:
            run_reconcile_tick(quiet)
        except Exception as exc:
            quiet["reconcile"] = time.monotonic() + _schedule_backoff()
            print(f"ROGMEM_SCHEDULE reconcile {type(exc).__name__}", flush=True)
        if stop is None:
            time.sleep(_poll_seconds())
        elif stop.wait(_poll_seconds()):
            break


def start_reconcile_loop(stop: threading.Event | None = None) -> threading.Thread:
    """Fold same-named pages on their own thread. A stuck scan cannot pause it.

    A private loop (one that is given stop) does not replace the process loop.
    The process loop is started again when its thread has exited.
    """

    global _RECONCILE_THREAD
    if stop is not None:
        thread = threading.Thread(
            target=_reconcile_loop,
            kwargs={"stop": stop},
            daemon=True,
            name="rogmem-reconcile",
        )
        thread.start()
        return thread
    with _RECONCILE_LOCK:
        if _RECONCILE_THREAD is not None and _RECONCILE_THREAD.is_alive():
            return _RECONCILE_THREAD
        thread = threading.Thread(
            target=_reconcile_loop,
            kwargs={"stop": None},
            daemon=True,
            name="rogmem-reconcile",
        )
        _RECONCILE_THREAD = thread
        thread.start()
        return thread


def start_ingest_worker(stop: threading.Event | None = None) -> threading.Thread:
    """One reader. Files taken in fast are read here, off the request thread."""
    thread = threading.Thread(
        target=_ingest_loop,
        kwargs={"stop": stop},
        daemon=True,
        name="rogmem-ingest",
    )
    thread.start()
    return thread


_INGEST_IDLE = 1.0


def _ingest_loop(stop: threading.Event | None = None) -> None:
    """Read queued files in order. Reading is not dispatching: a paused line still reads."""
    while stop is None or not stop.is_set():
        try:
            for source_id in engine.pending_ingests(2):
                engine.ingest_queued(source_id)
        except Exception as exc:
            print("ROGMEM_INGEST " + type(exc).__name__, flush=True)
        time.sleep(_INGEST_IDLE)


def worker() -> None:
    n = engine.distill_concurrency()
    print("ROGMEM_DISTILL_CONCURRENCY " + str(n), flush=True)
    start_distill_workers(n)
    start_ingest_worker()
    # Start probes before the first maintenance tick. A scan that never returns
    # must not be what keeps the first probe from running.
    start_probe_loop()
    start_reconcile_loop()
    quiet: dict[str, float] = {}
    while True:
        run_maintenance_tick(quiet)
        start_probe_loop()
        start_reconcile_loop()
        time.sleep(_poll_seconds())


def main() -> None:
    import uvicorn

    data = Path(os.environ.get("ROGMEM_DATA", "/data"))
    scan = Path(os.environ.get("ROGMEM_SCAN", "/scan"))
    engine.connect(data, scan)
    os.environ["ROGMEM_DATA"] = str(data)
    threading.Thread(target=engine.opening_backfill_loop, daemon=True, name="rogmem-opening-backfill").start()
    threading.Thread(target=_load_embed, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    print("ROGMEM_LISTEN " + os.environ.get("PORT", "18080"), flush=True)
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "18080")), lifespan="on")


def _load_embed() -> None:
    mode = os.environ.get("ROGMEM_EMBED", "fastembed")
    try:
        engine.set_embed_mode(mode)
        print("ROGMEM_EMBED_READY " + engine.health().get("embed", ""), flush=True)
    except Exception as exc:
        engine.set_embed_mode("off")
        detail = str(exc).strip().splitlines()[-1][:180] if str(exc).strip() else ""
        print("ROGMEM_EMBED_OFF " + type(exc).__name__ + (" " + detail if detail else ""), flush=True)
