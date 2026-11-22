"""Local rogmem CLI. First access is a subcommand; the admin UI talks to this process.

Host config and history upload wrap installer/. Ingress takes material envelopes.
This module does not distill and does not write crystals. `python -m rogmem`
with no args still serves the NAS process so the image CMD stays unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.request import Request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rogmem",
        description="本机 rogmem CLI。宿主配置、历史上传、材料信封进 Ingress。不在本机萃取，不写 crystal。",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="启动记忆中枢（NAS / 容器；无子命令时也走这里）")
    channel = sub.add_parser("channel", help="把中枢接到本机 18780。能直接访问时，这扇门交给本次登录")
    channel.add_argument("--origin", default="", help="登录门口已经记住的入口。入口会变，身份不变")
    channel.add_argument("--ticket", default="", help="添加宿主签发的接入票据：按身份从入口清单里挑一扇能开的门")
    channel.add_argument("--hub", default="", help="这份中枢的身份。省略时读 ROGMEM_HUB")
    channel.add_argument("--session", action="store_true", help=argparse.SUPPRESS)
    _add_home(channel)

    hosts = sub.add_parser("hosts", help="本机宿主配置（扫描 / 接入 / 状态）")
    hosts_sub = hosts.add_subparsers(dest="hosts_command")
    scan = hosts_sub.add_parser("scan", help="扫描已安装宿主，不写入")
    _add_home(scan)
    _add_dry_run(scan)
    connect = hosts_sub.add_parser("connect", help="把宿主指到中枢；未 --confirm 只出 dry-run 形状")
    _add_home(connect)
    _add_dry_run(connect)
    connect.add_argument("--confirm", action="store_true", help="确认写入配置；真实主目录仍默认拒绝")
    connect.add_argument("--program", action="append", dest="programs", help="只接入这些宿主，可重复。不传则接入全部已发现且受支持的宿主")
    connect.add_argument("--base-url", default="http://127.0.0.1:18780", help="中枢基址，配置写 /mcp")
    connect.add_argument("--grant", default="", help="添加宿主签发的短时准入凭证（旧客户端）")
    connect.add_argument("--ticket", default="", help="添加宿主签发的接入票据，CLI 从中取准入凭证并让中枢复核签名")
    update = hosts_sub.add_parser("update", help="把已支持宿主的全局规则与 hook 更新到最新；未 --confirm 只出形状")
    _add_home(update)
    _add_dry_run(update)
    update.add_argument("--confirm", action="store_true", help="确认更新；真实主目录仍默认拒绝")
    update.add_argument("--program", action="append", dest="programs", help="只更新这些宿主，可重复。不传则更新全部已发现且受支持的宿主")
    status = hosts_sub.add_parser("status", help="查看本机宿主接入状态，不写入")
    _add_home(status)
    _add_dry_run(status)
    verify = hosts_sub.add_parser("verify", help="真的执行每个宿主登记的 hook 命令（探针事件，不写入、不调中枢）")
    _add_home(verify)
    verify.add_argument("--program", action="append", dest="programs", help="只检查这些宿主，可重复")

    plugin = sub.add_parser("plugin", help="rogmem 插件：看版本、从中枢更新（钩子、规则、Hermes 记忆、门口一起）")
    plugin_sub = plugin.add_subparsers(dest="plugin_command")
    plugin_status = plugin_sub.add_parser("status", help="本机插件版本、中枢版本、每个宿主的钩子是否真能跑")
    _add_home(plugin_status)
    plugin_status.add_argument("--no-verify", action="store_true", help="只比版本，不执行钩子")
    plugin_update = plugin_sub.add_parser("update", help="从本机门口取中枢的插件，装到本机所有宿主并复核")
    _add_home(plugin_update)
    plugin_update.add_argument("--keep-door", action="store_true", help="不重启本机门口进程")

    history = sub.add_parser("history", help="历史会话预览 / 上传")
    history_sub = history.add_subparsers(dest="history_command")
    preview = history_sub.add_parser("preview", help="预览可导入条数；不上传")
    _add_home(preview)
    _add_dry_run(preview)
    preview.add_argument("--envelope", action="store_true", help="同时打出材料信封，不送 Ingress")
    preview.add_argument("--program", default="", help="只预览这一个程序")
    upload = history_sub.add_parser("upload", help="历史上传；未 --confirm 只出 dry-run 形状")
    _add_home(upload)
    _add_dry_run(upload)
    upload.add_argument("--confirm", action="store_true", help="确认上传。这是用户同意导入这台电脑")
    upload.add_argument("--program", default="", help="只上传这一个程序")
    upload.add_argument("--envelope", action="store_true", help="经 Ingress 信封接入点，默认 dry-run")
    upload.add_argument("--base-url", default="http://127.0.0.1:18780", help="中枢基址")
    follow = history_sub.add_parser("follow", help="后台持续导入已启用宿主的会话账本")
    _add_home(follow)
    follow.add_argument("--base-url", default="http://127.0.0.1:18780", help="中枢基址")
    follow.add_argument("--once", action="store_true", help="只送一轮还没记过的回合")

    ingress = sub.add_parser("ingress", help="材料信封进入 Ingress")
    ingress_sub = ingress.add_subparsers(dest="ingress_command")
    accept = ingress_sub.add_parser("accept", help="验收信封形状；默认 dry-run，不萃取、不写 crystal")
    _add_dry_run(accept)
    accept.add_argument("--file", type=Path, help="信封 JSON（对象或数组）")
    accept.add_argument("--stdin", action="store_true", help="从标准输入读信封 JSON")
    return parser


def _add_home(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", type=Path, help="扫描用的主目录；省略则用当前用户主目录（真实主目录默认只读）")


def _add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="只出形状，不写入、不上传（默认行为）")


def _dry_run(args: argparse.Namespace) -> bool:
    if getattr(args, "dry_run", False):
        return True
    return not getattr(args, "confirm", False)


def _home(args: argparse.Namespace) -> Path:
    return Path(args.home) if getattr(args, "home", None) else Path.home()


def _installer():
    try:
        from installer import rogmem_installer as inst
    except ImportError as exc:  # pragma: no cover - container image has no installer/
        raise SystemExit("宿主/历史命令需要仓库里的 installer 包。容器内只提供 serve。") from exc
    return inst


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


def _base_result(command: str, dry_run: bool, **extra: Any) -> dict[str, Any]:
    payload = {
        "ok": True,
        "command": command,
        "dry_run": dry_run,
        "wrote": False,
        "posted": 0,
        "distilled": False,
        "crystal_written": False,
    }
    payload.update(extra)
    return payload


def _sent_path(home: Path) -> Path:
    return home / ".rogmem" / "history.sent"


def _sent_keys(home: Path) -> set[str]:
    path = _sent_path(home)
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _remember_sent(home: Path, turns: list[dict]) -> None:
    path = _sent_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for turn in turns:
            handle.write(f"{turn.get('session_id')}:{turn.get('seq')}\n")


def _projects_path(home: Path) -> Path:
    return home / ".rogmem" / "history.projects"


def _projects_payload(projects: list[dict]) -> str:
    ordered = sorted(projects, key=lambda item: str(item.get("session_id") or ""))
    return json.dumps(ordered, ensure_ascii=False, sort_keys=True)


def _projects_current(home: Path, projects: list[dict]) -> bool:
    path = _projects_path(home)
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8") == _projects_payload(projects)


def _remember_projects(home: Path, projects: list[dict]) -> None:
    path = _projects_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_projects_payload(projects), encoding="utf-8")


def _fresh_turns(home: Path, turns: list[dict]) -> list[dict]:
    sent = _sent_keys(home)
    return [turn for turn in turns if f"{turn.get('session_id')}:{turn.get('seq')}" not in sent]


def _gap_path(home: Path) -> Path:
    return home / ".rogmem" / "upload-gap.json"


def note_upload_gap(home: Path, turns: list[dict]) -> None:
    """Replace the gap with this unsent set. The text stays in the host session."""
    if not turns:
        clear_upload_gap(home)
        return
    times = [str(turn.get("event_at") or "") for turn in turns if str(turn.get("event_at") or "")]
    path = _gap_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"since": min(times) if times else "", "pending": len(turns)}, ensure_ascii=False),
        encoding="utf-8",
    )


def _scope_path(home: Path) -> Path:
    return home / ".rogmem" / "history.scope"


def _scope_programs(home: Path) -> list[str]:
    """Empty means the confirmed upload covered every program on this computer."""
    path = _scope_path(home)
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded if str(item).strip()]


def _in_scope(turns: list[dict], programs: list[str]) -> list[dict]:
    if not programs:
        return list(turns)
    allowed = set(programs)
    return [turn for turn in turns if str(turn.get("program") or "") in allowed]


def _arm_resume(home: Path, base: str, programs: list[str]) -> None:
    """Remember where to resume. A later confirmation widens the set; it does not drop a program."""
    marker = home / ".rogmem" / "history.follow"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text((base or "http://127.0.0.1:18780") + "\n", encoding="utf-8")
    path = _scope_path(home)
    if path.is_file():
        current = _scope_programs(home)
        stored = [] if not current or not programs else list(dict.fromkeys([*current, *programs]))
    else:
        stored = programs
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")


def _scoped_fresh(home: Path) -> list[dict]:
    inst = _installer()
    turns = inst.collect_history(home, allow_real=True).get("turns") or []
    return _in_scope(_fresh_turns(home, turns), _scope_programs(home))


def project_upload_gap(home: Path) -> None:
    """The gap is the unsent turns inside the confirmed scope, recomputed from the ledger."""
    if not (home / ".rogmem" / "history.follow").is_file():
        return
    note_upload_gap(home, _scoped_fresh(home))


def clear_upload_gap(home: Path) -> None:
    _gap_path(home).unlink(missing_ok=True)


def _note_closed_gap(home: Path) -> None:
    """A confirmed upload that cannot reach the hub records the gap and keeps the sessions local."""
    project_upload_gap(home)


def _reconcile(home: Path, base: str) -> int:
    inst = _installer()
    from rogmem.ingress import accept, envelopes_from_turns

    turns = _scoped_fresh(home)
    if not turns:
        clear_upload_gap(home)
        return 0
    if not inst._channel_open(base.rstrip("/") + "/mcp"):
        note_upload_gap(home, turns)
        return 0
    result = accept(envelopes_from_turns(turns), dry_run=False, post=lambda items: _post_turns(base, items))
    posted = int(result.get("posted") or 0)
    if result.get("ok"):
        _remember_sent(home, turns)
    project_upload_gap(home)
    return posted


def _hub_file(home: Path) -> Path:
    return home / ".rogmem" / "hub"


def _remembered_hub(home: Path) -> str:
    path = _hub_file(home)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _remember_hub(home: Path, hub: str) -> None:
    text = (hub or "").strip()
    if not text:
        return
    path = _hub_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _channel_closed_text(refused: str, hub: str) -> str:
    if refused == "bad-ticket":
        return "这段接入票据不完整。回宿主管理再点一次「添加宿主」，复制整段提示里的票据。"
    if refused == "ticket-expired":
        return "这张接入票据已经过期。回宿主管理再点一次「添加宿主」，拿一张新的。"
    if refused == "login-wall":
        return "这个地址把 /healthz 转到了别处。把已经打开它的浏览器会话放进 ROGMEM_HUB_COOKIE 再试。不会在这台电脑上另存一份记忆。"
    if refused in {"no-matching-hub", "no-entrance"}:
        return "票据里的入口都没有回答这份中枢的身份。确认中枢在线、极空间远程访问开着，或让运维设置 ROGMEM_PUBLIC_ORIGIN 后再签发一张。不会在这台电脑上另存一份记忆。"
    if hub:
        return "没有找到这份中枢。不会在这台电脑上另存一份记忆。"
    return "这个地址没有返回 rogmem。不会在这台电脑上另存一份记忆。"


def channel_main(args: argparse.Namespace) -> int:
    """Hold the local door open. The entrance may move; the hub identity does not."""
    inst = _installer()
    home = _home(args)
    origin = str(getattr(args, "origin", "") or "").strip()
    hub = str(getattr(args, "hub", "") or "").strip() or os.environ.get("ROGMEM_HUB", "").strip()
    ticket_text = str(getattr(args, "ticket", "") or "").strip() or os.environ.get("ROGMEM_TICKET", "").strip()
    if ticket_text:
        # The ticket is the authority: it carries the identity and an ordered
        # entrance policy signed by the hub. The address that opened the admin
        # console never takes part in this decision.
        resolved = inst.resolve_ticket(ticket_text, cookie=os.environ.get("ROGMEM_HUB_COOKIE", ""))
        if not resolved.get("ok"):
            _note_closed_gap(home)
            refused = str(resolved.get("refused") or "no-matching-hub")
            return _emit(
                _base_result(
                    "channel",
                    False,
                    ok=False,
                    refused=refused,
                    hub=str(resolved.get("hub") or ""),
                    tried=resolved.get("tried") or [],
                    text=_channel_closed_text(refused, str(resolved.get("hub") or hub)),
                )
            )
        origin = str(resolved.get("origin") or "")
        hub = str(resolved.get("hub") or hub)
    if not origin and not hub and not ticket_text:
        hub = _remembered_hub(home)
    if origin or hub:
        plan = inst.plan_channel(origin, hub)
        if not plan.get("ok"):
            _note_closed_gap(home)
            refused = str(plan.get("refused") or "channel-closed")
            return _emit(
                _base_result(
                    "channel",
                    False,
                    ok=False,
                    refused=refused,
                    text=_channel_closed_text(refused, hub),
                )
            )
        _remember_hub(home, str(plan.get("hub") or hub))
        if plan.get("mode") == "ready":
            return _emit(
                _base_result(
                    "channel",
                    False,
                    base_url=f"http://127.0.0.1:{inst.FIXED_FORWARD_PORT}",
                    text="本机门口已经是这个地址。",
                )
            )
        bound = str(plan.get("hub") or hub)
        if not getattr(args, "session", False) and inst.door_stands_alone(str(plan.get("origin") or origin)):
            unit = inst.session_unit(
                inst.platform.system(),
                str(plan.get("origin") or origin),
                sys.executable,
                str(Path(__file__).resolve().parents[1]),
                bound,
            )
            if unit.get("kind"):
                path = inst.write_session_unit(home, unit)
                started = inst.start_session_unit(unit, path) if home.resolve() == Path.home().resolve() else False
                if home.resolve() == Path.home().resolve() and not started:
                    return _emit(
                        _base_result(
                            "channel",
                            False,
                            ok=False,
                            wrote=True,
                            base_url=f"http://127.0.0.1:{inst.FIXED_FORWARD_PORT}",
                            service=str(path),
                            started=False,
                            text="登录服务没有接上 18780。先停掉已经占用这扇门的进程，再运行一次。",
                        )
                    )
                return _emit(
                    _base_result(
                        "channel",
                        False,
                        wrote=True,
                        base_url=f"http://127.0.0.1:{inst.FIXED_FORWARD_PORT}",
                        service=str(path),
                        started=started,
                        text="这扇门跟这次登录走。关掉终端，本机宿主还从 18780 进中枢。",
                    )
                )
        try:
            if plan.get("mode") == "port":
                server = inst.forward(inst.FIXED_FORWARD_PORT, int(plan["target"]), home=home, hub=bound)
            else:
                server = inst.forward_origin(inst.FIXED_FORWARD_PORT, str(plan["origin"]), home=home, hub=bound)
        except OSError:
            door = f"http://127.0.0.1:{inst.FIXED_FORWARD_PORT}"
            held = inst.probe_origin(door)
            if held.get("ok") and inst.same_hub(bound, str(held.get("hub") or "")):
                return _emit(
                    _base_result(
                        "channel",
                        False,
                        base_url=door,
                        text="本机门口已经是这个地址。",
                    )
                )
            return _emit(
                _base_result(
                    "channel",
                    False,
                    ok=False,
                    refused="channel-closed",
                    base_url=door,
                    text="本机门口被占着，而且它不是这份中枢。停掉占用 18780 的进程后再运行一次。不会在这台电脑上另存一份记忆。",
                )
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "command": "channel",
                    "base_url": f"http://127.0.0.1:{inst.FIXED_FORWARD_PORT}",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    else:
        found = inst.find_rogmem()
        if found is None:
            _note_closed_gap(home)
            return _emit(
                _base_result(
                    "channel",
                    False,
                    ok=False,
                    refused="channel-closed",
                    text="极空间远程访问没开。不会在这台电脑上另存一份记忆。",
                )
            )
        identity = str(inst.probe_origin(f"http://127.0.0.1:{found}").get("hub") or "")
        _remember_hub(home, identity)
        server = inst.forward(inst.FIXED_FORWARD_PORT, found, home=home, hub=identity)
    from rogmem.host_hook import drain_outbox

    started_with = _installed_plugin(home)
    try:
        while True:
            if _door_should_hand_over(home, started_with):
                # A plugin update landed while this door ran. The door outlives whoever ran the
                # update (an agent shell kills its children), so the door replaces itself.
                server.shutdown()
                server.server_close()
                _reexec_door()
            try:
                drain_outbox(home)
                try:
                    _note_presence(home)
                except Exception:
                    pass
                marker = home / ".rogmem" / "history.follow"
                if marker.is_file():
                    follow_base = marker.read_text(encoding="utf-8").strip() \
                        or f"http://127.0.0.1:{inst.FIXED_FORWARD_PORT}"
                    _reconcile(home, follow_base)
                # 新路径：admin 下达的目标设备作业——每个 agent 用自己的
                # MCP url 推出中枢地址，不依赖 loopback，也不依赖 history.follow。
                try:
                    run_host_import_jobs(home)
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(10)
    finally:
        server.shutdown()


def _installed_plugin(home: Path) -> str:
    try:
        from rogmem import plugin
    except ImportError:
        return ""
    return str(plugin.installed(home).get("version") or "")


def _door_should_hand_over(home: Path, started_with: str) -> bool:
    """The manifest names a different plugin than the one this process loaded."""
    now = _installed_plugin(home)
    return bool(started_with and now and now != started_with)


def _reexec_door() -> None:
    """Start the same door command from the freshly installed code, then leave."""
    import subprocess

    command = [sys.executable, "-m", "rogmem", *sys.argv[1:]]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
        start_new_session=os.name != "nt",
    )
    os._exit(0)


def _note_presence(home: Path) -> None:
    """Tell the hub which computer this door is, without touching last-used."""
    bearers = _agent_bearers(home)
    item = next((row for row in bearers if row.get("token") and row.get("device")), None)
    if item is None:
        return
    base = _hub_base_from_mcp_url(str(item.get("url") or ""))
    if not base:
        return
    request = Request(
        base.rstrip("/") + "/host/presence",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + str(item["token"]),
            "X-Rogmem-Device": str(item["device"]),
        },
        method="POST",
    )
    try:
        with _installer().open_direct(request, 5) as resp:
            resp.read()
    except Exception:
        return


def _agent_bearers(home: Path) -> list[dict]:
    """从本机已写入的宿主配置里读出每个 agent 的 (program, url, token, device)。"""
    from rogmem.host_hook import _endpoint, program_of

    inst = _installer()
    programs = inst.plan(home)
    out: list[dict] = []
    for program in programs:
        try:
            url, token, device = _endpoint(home, program)
        except Exception:
            continue
        if not url or not token.startswith("rm_"):
            continue
        out.append({"program": program, "url": url, "token": token, "device": device})
    return out


def _mcp_tool_call(url: str, token: str, device: str, name: str, arguments: dict,
                   timeout: float = 30.0) -> dict:
    """走已鉴权 MCP 通道。复用 host_hook._mcp 的解码；永不把 payload 入日志。"""
    from rogmem.host_hook import _mcp, _tool_text

    body = _mcp(url, token, device, name, arguments, timeout=timeout)
    text = _tool_text(body)
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _hub_base_from_mcp_url(url: str) -> str:
    """从 agent MCP URL 还原中枢 base（去掉结尾 /mcp）。"""
    text = (url or "").strip().rstrip("/")
    if text.endswith("/mcp"):
        return text[: -len("/mcp")]
    return text


def _post_json_with_headers(url: str, payload: dict, headers: dict) -> dict:
    import urllib.error

    data = json.dumps(payload, ensure_ascii=False).encode()
    merged = {"Content-Type": "application/json", **(headers or {})}
    request = Request(url, data=data, headers=merged, method="POST")
    try:
        with _installer().open_direct(request, 60) as resp:
            loaded = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # 服务端 4xx/5xx 常带 JSON body 说明 refused 原因；把它透传给 worker
        try:
            body = json.loads(exc.read().decode())
            if isinstance(body, dict):
                body.setdefault("ok", False)
                body.setdefault("refused", f"http-{exc.code}")
                return body
        except Exception:
            pass
        return {"ok": False, "refused": f"http-{exc.code}"}
    except Exception:
        return {"ok": False, "refused": "post-failed"}
    return loaded if isinstance(loaded, dict) else {"ok": False, "refused": "post-failed"}


_INITIAL_LEASE_S = 600  # 默认 claim/续租窗口（秒）
_RENEW_MARGIN_S = 60     # lease 剩余不足该秒数时主动续租


def _now_mono() -> float:
    return time.monotonic()


class _JobHeartbeat:
    """评审 R4-1：后台心跳，覆盖扫描/长准备/上传全生命周期。

    - 后台 daemon 线程每 `max(1, lease_s // 3)` 秒检查一次 lease 剩余；
      剩余 < `_RENEW_MARGIN_S` 时发 renew；失败则 set `aborted` + 记类别。
    - 主线程通过 `heart.check()` 在 scan 前后/每批前确认未 aborted。
    - `heart.stop()` 幂等；`with _JobHeartbeat(...)` 语义保证 stop 一定被调用。
    """

    def __init__(self, url: str, token: str, device: str, *, job_id: str,
                 lease_token: str, lease_s: int = _INITIAL_LEASE_S,
                 margin_s: int = _RENEW_MARGIN_S):
        self.url = url
        self.token = token
        self.device = device
        self.job_id = job_id
        self.lease_token = lease_token
        self.lease_s = max(2, int(lease_s))
        self.margin_s = max(1, int(margin_s))
        self.lease_deadline = _now_mono() + self.lease_s
        self.aborted = threading.Event()
        self.aborted_reason = ""
        self._stop = threading.Event()
        self._tick_s = max(0.25, min(self.lease_s / 3.0, 5.0))
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._loop, name="rogmem-job-hb", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False  # 不吞异常

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=self._tick_s * 2 + 0.5)
        self._thread = None

    def _renew_once(self) -> None:
        try:
            resp = _mcp_tool_call(self.url, self.token, self.device,
                                  "renew_host_import_lease",
                                  {"job_id": self.job_id,
                                   "lease_token": self.lease_token,
                                   "lease_s": self.lease_s})
        except Exception:
            return  # 网络抖动不代表 abort；下一拍再试
        if resp.get("ok"):
            self.lease_deadline = _now_mono() + self.lease_s
            return
        reason = _classify_refused(resp.get("refused"))
        if reason in {"cancelled", "not-active", "stale-lease"}:
            self.aborted_reason = reason
            self.aborted.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            # 剩余 margin 之内才 renew；或当已过期
            remaining = self.lease_deadline - _now_mono()
            if remaining <= self.margin_s:
                self._renew_once()
                if self.aborted.is_set():
                    return
            # 再睡一个 tick 或等 stop
            if self._stop.wait(self._tick_s):
                return

    def check(self) -> tuple[bool, str]:
        """同步通知主线程：True = 仍可继续；False = 需 abort。"""
        return (not self.aborted.is_set(), self.aborted_reason)


def run_host_import_jobs(home: Path, _unused_base: str | None = None) -> dict:
    """评审 R2 P2-2/4/5/6 重写：
    - pull / claim 之前不扫描本地转写；
    - 只对**有作业**的 agent 扫描；并且扫描 scope 限定在该 program，不读其他宿主目录；
    - 续租按**时间**驱动：scan 开始前、每批前都检查 lease 剩余，不够则 renew；
    - 不写本机 history.sent cursor，不依赖它去重——服务端 sources UNIQUE 幂等是唯一真源；
      所以新人工 job 不会被旧全局 cursor 阻挡；
    - 上传经 /host/jobs/{id}/upload，posted/duplicates 完全以服务端回执为准。
    """
    inst = _installer()
    agents = _agent_bearers(home)
    stats = {"ok": True, "agents": len(agents), "jobs": 0, "posted": 0,
             "duplicates": 0, "refused_batches": 0, "failed_batches": 0}
    if not agents:
        return stats

    for ident in agents:
        program = ident["program"]
        url, token, device = ident["url"], ident["token"], ident["device"]
        hub_base = _hub_base_from_mcp_url(url)
        # 能力声明（幂等；老 client 从不调用这一步）
        try:
            _mcp_tool_call(url, token, device, "announce_capabilities",
                           {"capabilities": "host_import_jobs:v1"})
        except Exception:
            pass
        pending = _mcp_tool_call(url, token, device, "pending_host_import_jobs", {})
        jobs = pending.get("jobs") or []
        if not jobs:
            continue  # 评审 P2-5：没有作业 → **不**扫描本地转写

        for job in jobs:
            job_id = str(job.get("job_id") or "")
            lease_token = str(job.get("lease_token") or "")
            if not job_id or not lease_token:
                continue
            stats["jobs"] += 1
            posted = dup = failed = 0
            last_error = ""
            aborted = False
            # R4-1：后台心跳贯穿 scan + upload；上下文结束即 stop。
            with _JobHeartbeat(url, token, device, job_id=job_id,
                               lease_token=lease_token,
                               lease_s=_INITIAL_LEASE_S,
                               margin_s=_RENEW_MARGIN_S) as heart:
                # scan 前检查
                ok, err = heart.check()
                if not ok:
                    last_error = err or "stale-lease"
                    aborted = True
                else:
                    from rogmem.exchange import order_history_newest_first

                    def upload_batch(turns: list[dict]) -> bool:
                        nonlocal posted, dup, failed, aborted, last_error
                        if not turns:
                            return True
                        alive, reason = heart.check()
                        if not alive:
                            last_error = reason or "stale-lease"
                            aborted = True
                            return False
                        upload = _post_json_with_headers(
                            f"{hub_base}/host/jobs/{job_id}/upload",
                            {"program": program, "turns": turns},
                            {
                                "Authorization": f"Bearer {token}",
                                "X-Rogmem-Device": device,
                                "X-Rogmem-Lease-Token": lease_token,
                            },
                        )
                        if not upload.get("ok"):
                            refused = upload.get("refused") or "post-failed"
                            last_error = _classify_refused(refused)
                            if refused in {"cancelled", "terminal", "stale-lease",
                                           "not-active", "unknown-job"}:
                                stats["refused_batches"] += 1
                                aborted = True
                                return False
                            failed += len(turns)
                            stats["failed_batches"] += 1
                            return True
                        posted += int(upload.get("posted") or 0)
                        dup += int(upload.get("duplicates") or 0)
                        return True

                    # 近的文件先读、先交。一场没读完不必挡住下一场的第一批。
                    for session in inst.read_program_history(home, program, allow_real=True):
                        if aborted:
                            break
                        ordered = order_history_newest_first(session)
                        for offset in range(0, len(ordered), 40):
                            if not upload_batch(ordered[offset : offset + 40]):
                                break
            # 心跳 context 退出后再 ack；ack 本身也走 lease 保护
            if aborted and "cancelled" in last_error:
                ack_status = "cancelled_ack"
            elif not aborted and failed == 0:
                ack_status = "completed"
            else:
                ack_status = "partial"
            _ack(url, token, device, job_id, lease_token,
                 status=ack_status, failed=failed, last_error=last_error)
            stats["posted"] += posted
            stats["duplicates"] += dup
    return stats


def _ack(url: str, token: str, device: str, job_id: str, lease_token: str,
         *, status: str, failed: int, last_error: str) -> None:
    try:
        _mcp_tool_call(url, token, device, "ack_host_import_job", {
            "job_id": job_id, "lease_token": lease_token, "status": status,
            "failed": int(failed), "last_error_class": last_error,
        })
    except Exception:
        pass


_CLASSIFY = {
    "cancelled": "cancelled",
    "terminal": "cancelled",
    "stale-lease": "stale-lease",
    "not-active": "not-active",
    "unknown-job": "not-active",
    "channel-closed": "channel-closed",
    "post-failed": "post-failed",
}


def _classify_refused(reason) -> str:
    text = str(reason or "").strip()
    if text.startswith("http-"):
        return text if text in {"http-400", "http-401", "http-403", "http-404",
                                 "http-408", "http-409", "http-410", "http-413",
                                 "http-415", "http-429", "http-500", "http-502",
                                 "http-503", "http-504"} else "post-failed"
    return _CLASSIFY.get(text, "post-failed")


def serve() -> int:
    from rogmem.app import main as serve_main

    serve_main()
    return 0


def _post_json(url: str, payload: dict) -> dict:
    import urllib.error

    data = json.dumps(payload, ensure_ascii=False).encode()
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _installer().open_direct(request, 120) as resp:
            loaded = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "refused": f"http-{exc.code}"}
    except Exception:
        return {"ok": False, "refused": "post-failed"}
    return loaded if isinstance(loaded, dict) else {"ok": False, "refused": "post-failed"}


def _post_turns(base: str, turns: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for turn in turns:
        groups.setdefault(str(turn.get("program") or "历史导入"), []).append(turn)
    from rogmem.exchange import chunk_whole_sessions, order_history_newest_first

    results = []
    for program, items in groups.items():
        items = order_history_newest_first(items)
        for chunk in chunk_whole_sessions(items, 40):
            result = {"ok": False, "refused": "post-failed"}
            for attempt in range(6):
                result = _post_json(f"{base.rstrip('/')}/admin/api/history", {"program": program, "turns": chunk})
                if result.get("ok"):
                    break
                time.sleep(2 * (attempt + 1))
            results.append(result)
            time.sleep(0.4)
    return {"ok": all(item.get("ok") for item in results), "results": results}


def hosts_scan(args: argparse.Namespace) -> int:
    inst = _installer()
    home = _home(args)
    rows = inst.inspect_hosts(home, allow_real=True)
    return _emit(
        _base_result(
            "hosts.scan",
            True,
            hosts=rows,
            text="确认之前不会写入。",
        )
    )


def hosts_status(args: argparse.Namespace) -> int:
    inst = _installer()
    home = _home(args)
    identity = inst.viewer_identity(home)
    return _emit(
        _base_result(
            "hosts.status",
            True,
            device_id=identity["device_id"],
            device_name=identity["device_name"],
            bundle_rev=inst.installed_bundle_rev(home),
            plugin_version=inst.installed_bundle_rev(home),
            hosts=inst.inspect_hosts(home),
        )
    )


def hosts_connect(args: argparse.Namespace) -> int:
    inst = _installer()
    home = _home(args)
    dry = _dry_run(args)
    programs = list(args.programs) if args.programs else None
    ticket_text = str(getattr(args, "ticket", "") or "").strip()
    grant = str(args.grant or "").strip()

    if ticket_text:
        # One capability instead of two. The ticket carries the same short-lived
        # secret the hub minted; the hub also re-checks the ticket's signature
        # when the client redeems it, so a swapped credential is refused.
        from rogmem import admission

        ticket = admission.decode(ticket_text)
        if ticket is None or ticket.expired():
            return _emit(
                _base_result(
                    "hosts.connect",
                    dry,
                    ok=False,
                    refused="bad-ticket" if ticket is None else "ticket-expired",
                    text="接入票据无效或已过期。回宿主管理再点一次「添加宿主」。",
                )
            )
        if grant and grant != ticket.secret:
            return _emit(
                _base_result("hosts.connect", dry, ok=False, refused="grant-mismatch",
                             text="--grant 与 --ticket 里的凭证不一致。只传其中一个。")
            )
        grant = ticket.secret

    plan = inst.connect_hosts(home, args.base_url, programs, confirm=not dry, allow_real=True)
    payload = _base_result(
        "hosts.connect",
        dry,
        programs=inst.chosen_hosts(home, programs),
        text=plan.get("text") or "",
        refused=plan.get("refused") or "",
        written=plan.get("written") or [],
        wrote=bool(plan.get("written")),
        grant_used=bool(grant),
        ok=bool(plan.get("ok")) if not dry else True,
    )
    if dry:
        payload["ok"] = True
        payload["wrote"] = False
        payload["written"] = []
    elif grant and payload["written"]:
        redeemed = []

        def post(body: dict, _base: str = args.base_url) -> dict:
            if ticket_text:
                body = {**body, "ticket": ticket_text}
            return _post_json(f"{_base.rstrip('/')}/enroll/redeem", body)

        for program in payload["written"]:
            item = inst.redeem_grant(home, args.base_url, grant, program, post, allow_real=True)
            redeemed.append(
                {
                    "program": program,
                    "ok": bool(item.get("ok")),
                    "status": item.get("status") or "",
                    "refused": item.get("refused") or "",
                }
            )
        payload["redeemed"] = redeemed
        if any(not item["ok"] for item in redeemed):
            payload["ok"] = False
    return _emit(payload)


def hosts_update(args: argparse.Namespace) -> int:
    """Rewrite the host-side plugin for every installed host. No key, no config, no data."""
    inst = _installer()
    home = _home(args)
    dry = _dry_run(args)
    programs = list(args.programs) if args.programs else None
    if dry:
        return _emit(
            _base_result(
                "hosts.update",
                True,
                programs=inst.chosen_hosts(home, programs),
                text="确认之前不会更新任何宿主。",
            )
        )
    result = inst.refresh_host_bundle(home, programs, allow_real=True)
    written = list(result.get("rules") or []) + [name for name in (result.get("hooks") or []) if name not in (result.get("rules") or [])]
    return _emit(
        _base_result(
            "hosts.update",
            False,
            ok=bool(result.get("ok")),
            refused=result.get("refused") or "",
            rev=result.get("rev") or 0,
            programs=result.get("programs") or [],
            rules=result.get("rules") or [],
            hooks=result.get("hooks") or [],
            wrote=bool(written),
            text="已把本机已支持宿主的全局规则和 hook 更新到最新。" if result.get("ok") else "没有更新。",
            verify=_hook_report(inst, home, programs) if result.get("ok") else {},
        )
    )


def _hook_report(inst, home: Path, programs: list[str] | None) -> dict:
    """Registered hooks executed for real, the last event each host delivered, and WorkBuddy's trust gate."""
    rows = inst.verify_host_hooks(home, programs)
    for row in rows:
        row["last_event"] = inst.hook_heartbeat(home, str(row.get("program") or ""))
    human = []
    trusted = inst.workbuddy_mcp_trusted(home)
    if trusted is False:
        human.append("WorkBuddy：在连接器管理右上角的自定义连接器里，给 rogmem 点一次「信任」。不点，会话里没有 core / search 工具，只能靠 hook 装入历史包。")
    broken = [row for row in rows if not row.get("ok")]
    return {"hooks": rows, "broken": len(broken), "workbuddy_mcp_trusted": trusted, "human_actions": human}


def hosts_verify(args: argparse.Namespace) -> int:
    inst = _installer()
    home = _home(args)
    programs = list(args.programs) if args.programs else None
    report = _hook_report(inst, home, programs)
    ok = report["broken"] == 0
    text = "每个登记的 hook 都能跑。" if ok else f"{report['broken']} 条登记的 hook 跑不起来。运行 rogmem hosts update --confirm 重写。"
    return _emit(_base_result("hosts.verify", True, ok=ok, text=text, **report))


def _hermes_turns(home: Path) -> list[dict]:
    inst = _installer()
    ledger = inst._read_hermes_ledger(home / ".hermes" / "state.db")
    if not ledger:
        return []
    turns = []
    for turn in ledger["turns"]:
        item = dict(turn)
        item["program"] = "Hermes"
        turns.append(item)
    return turns


def _session_projects(turns: list[dict]) -> list[dict]:
    """One repository per session. The hub fills turns that were stored before the field existed."""
    seen: dict[str, str] = {}
    for turn in turns:
        session_id = str(turn.get("session_id") or "").strip()
        project = str(turn.get("project") or "").strip()
        if session_id and project and session_id not in seen:
            seen[session_id] = project
    return [{"session_id": session_id, "project": project} for session_id, project in seen.items()]


def import_fresh_hermes(home: Path, base: str, chunk_size: int = 15) -> dict:
    """Post Hermes ledger turns that this computer has not already handed to the hub."""
    turns = _hermes_turns(home)
    from rogmem.exchange import chunk_whole_sessions, order_history_newest_first

    fresh = order_history_newest_first(_fresh_turns(home, turns))
    posted = 0
    duplicates = 0
    new = 0
    url = base.rstrip("/") + "/admin/api/history"
    for chunk in chunk_whole_sessions(fresh, chunk_size):
        result = {"ok": False, "refused": "post-failed"}
        for attempt in range(6):
            result = _post_json(url, {"program": "Hermes", "turns": chunk})
            if result.get("ok"):
                break
            delay = 20 if str(result.get("refused") or "").endswith("429") else min(15, 2 * (attempt + 1))
            time.sleep(delay)
        if not result.get("ok"):
            if (home / ".rogmem" / "history.follow").is_file():
                project_upload_gap(home)
            else:
                note_upload_gap(home, fresh[offset:])
            return {
                "ok": False,
                "refused": result.get("refused") or "post-failed",
                "posted": posted,
                "new": new,
                "duplicates": duplicates,
                "remaining": len(fresh) - offset,
            }
        _remember_sent(home, chunk)
        posted += len(chunk)
        duplicates += int(result.get("duplicates") or 0)
        imported = result.get("imported") or []
        if imported:
            new += sum(1 for item in imported if not item.get("duplicate"))
        elif not result.get("duplicates"):
            new += len(chunk)
        time.sleep(1.2)
    if (home / ".rogmem" / "history.follow").is_file():
        project_upload_gap(home)
    elif not _fresh_turns(home, turns):
        clear_upload_gap(home)
    projects = _session_projects(turns)
    bound = 0
    if projects and not _projects_current(home, projects):
        result = _post_json(url, {"program": "Hermes", "projects": projects})
        if not result.get("ok"):
            return {
                "ok": False,
                "refused": result.get("refused") or "post-failed",
                "posted": posted,
                "new": new,
                "duplicates": duplicates,
                "remaining": 0,
                "projects": 0,
            }
        if "projects" not in result:
            bound = 0
        else:
            bound = int(result.get("projects") or 0)
            _remember_projects(home, projects)
    return {
        "ok": True,
        "refused": "",
        "posted": posted,
        "new": new,
        "duplicates": duplicates,
        "remaining": 0,
        "projects": bound,
    }


def history_follow(args: argparse.Namespace) -> int:
    """Keep handing the Hermes ledger to the hub. New turns are picked up on the next pass."""
    home = _home(args)
    base = str(args.base_url or "http://127.0.0.1:18780")
    while True:
        result = import_fresh_hermes(home, base)
        print(json.dumps({"command": "history.follow", **result}, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if result.get("ok") else 1
        time.sleep(8 if result.get("posted") else 20)


def history_preview(args: argparse.Namespace) -> int:
    inst = _installer()
    from rogmem.ingress import envelopes_from_turns

    home = _home(args)
    preview = inst.history_preview(home, allow_real=True)
    turns = inst.collect_history(home, allow_real=True).get("turns") or []
    if args.program:
        turns = [item for item in turns if item.get("program") == args.program]
        preview = {
            "refused": preview.get("refused") or "",
            "programs": [item for item in (preview.get("programs") or []) if item.get("program") == args.program],
            "importable": len(turns),
            "skipped": preview.get("skipped") or 0,
        }
    envelopes = envelopes_from_turns(turns) if args.envelope else []
    lines = ["这些原文会送到记忆中枢。确认之前不会导入。"]
    for item in preview.get("programs") or []:
        lines.append(f"{item['program']}：可导入 {item['importable']} 条，跳过 {item['skipped']} 条。")
    if len(lines) == 1:
        lines.append("没有找到可导入的会话。")
    return _emit(
        _base_result(
            "history.preview",
            True,
            **preview,
            envelopes=envelopes,
            text="\n".join(lines),
        )
    )


def history_upload(args: argparse.Namespace) -> int:
    inst = _installer()
    from rogmem.ingress import accept, envelopes_from_turns

    home = _home(args)
    dry = _dry_run(args)
    collected = inst.collect_history(home, allow_real=True)
    turns = collected.get("turns") or []
    skipped = collected.get("skipped") or 0
    if args.program:
        turns = [item for item in turns if item.get("program") == args.program]
        row = next((item for item in (collected.get("by_program") or []) if item.get("program") == args.program), None)
        if row:
            skipped = int(row.get("skipped") or 0)
    envelopes = envelopes_from_turns(turns)
    refused = collected.get("refused") or ""
    posted = 0
    if dry:
        ingress = accept(envelopes, dry_run=True)
        refused = refused or "unconfirmed"
        ok = True
    else:
        base = str(args.base_url or "").rstrip("/")
        if refused:
            ingress = accept(envelopes, dry_run=True)
            ok = False
        elif not inst._channel_open(base + "/mcp"):
            ingress = accept(envelopes, dry_run=True)
            refused = "channel-closed"
            ok = False
        else:
            def post(items: list[dict], _base: str = base) -> dict:
                return _post_turns(_base, items)

            ingress = accept(envelopes, dry_run=False, post=post)
            refused = ingress.get("refused") or ""
            posted = int(ingress.get("posted") or 0)
            ok = bool(ingress.get("ok")) and not refused
        if ok:
            _remember_sent(home, turns)
        if not (collected.get("refused") or ""):
            _arm_resume(home, base, [args.program] if args.program else [])
            project_upload_gap(home)
    payload = _base_result(
        "history.upload",
        dry,
        ok=ok,
        importable=len(turns),
        skipped=skipped,
        refused=refused,
        ingress=ingress if dry else {"posted": posted, "refused": refused, "distilled": False, "crystal_written": False},
        posted=posted,
        text="确认之前不会导入。" if dry else ("原文已送到记忆中枢。萃取在中枢上进行。" if ok else "没有送完。"),
    )
    if args.envelope and dry:
        payload["envelopes"] = envelopes
    return _emit(payload)


def ingress_accept(args: argparse.Namespace) -> int:
    from rogmem.ingress import accept

    if args.file is None and not args.stdin:
        return _emit(
            _base_result(
                "ingress.accept",
                True,
                ok=False,
                refused="no-envelope",
                errors=[{"index": 0, "errors": ["no-envelope"]}],
                accepted=[],
            )
        )
    if args.stdin:
        from rogmem.host_hook import read_outside

        try:
            raw = read_outside(sys.stdin)
        except UnicodeError:
            raw = ""
    else:
        raw = Path(args.file).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _emit(
            _base_result(
                "ingress.accept",
                True,
                ok=False,
                refused="invalid-json",
                errors=[{"index": 0, "errors": ["invalid-json"]}],
                accepted=[],
            )
        )
    result = accept(payload, dry_run=True)
    out = _base_result("ingress.accept", True)
    out.update(result)
    out["command"] = "ingress.accept"
    out["dry_run"] = True
    return _emit(out)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return serve()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    if command == "channel":
        return channel_main(args)
    if command == "serve":
        return serve()
    if command == "plugin":
        from rogmem import plugin_cli

        home = _home(args)
        if args.plugin_command == "status":
            return _emit(plugin_cli.status(home, verify=not args.no_verify))
        if args.plugin_command == "update":
            return _emit(plugin_cli.update(home, restart_door=not args.keep_door))
        parser.parse_args(["plugin", "--help"])
        return 2
    if command == "hosts":
        action = {
            "scan": hosts_scan,
            "status": hosts_status,
            "connect": hosts_connect,
            "update": hosts_update,
            "verify": hosts_verify,
        }.get(args.hosts_command)
        if action is None:
            parser.parse_args(["hosts", "--help"])
            return 2
        return action(args)
    if command == "history":
        action = {
            "preview": history_preview,
            "upload": history_upload,
            "follow": history_follow,
        }.get(args.history_command)
        if action is None:
            parser.parse_args(["history", "--help"])
            return 2
        return action(args)
    if command == "ingress":
        if args.ingress_command == "accept":
            return ingress_accept(args)
        parser.parse_args(["ingress", "--help"])
        return 2
    parser.print_help()
    return 2
