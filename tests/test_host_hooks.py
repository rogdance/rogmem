"""Selected hosts get their hook. No selection means every detected supported host."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from installer.rogmem_installer import chosen_hosts, write_configs
from rogmem.host_hook import _flush, handle


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "Library" / "Application Support" / "Cursor").mkdir(parents=True)
    (home / ".hermes").mkdir()
    (home / ".codex").mkdir()
    return home


def test_no_selection_means_every_detected_host(tmp_path):
    home = _home(tmp_path)
    assert chosen_hosts(home, None) == ["Cursor", "Hermes", "Codex"]
    assert chosen_hosts(home, []) == []
    assert chosen_hosts(home, ["Hermes", "Nope"]) == ["Hermes"]


def test_a_named_host_installs_only_its_hook(tmp_path):
    home = _home(tmp_path)
    assert write_configs(home, "http://127.0.0.1:18780", programs=["Cursor"]) == ["Cursor"]
    script = home / ".cursor" / "hooks" / "rogmem.py"
    assert script.is_file() and script.stat().st_mode & 0o111
    saved = json.loads((home / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert saved["hooks"]["sessionStart"][0]["command"] == str(script)
    assert saved["hooks"]["sessionStart"][0]["timeout"] == 3
    assert (home / ".cursor" / "rules" / "rogmem.mdc").is_file()
    assert not (home / ".hermes" / "agent-hooks" / "rogmem.py").exists()
    assert not (home / ".hermes" / "rogmem-rule.md").exists()
    assert not (home / ".codex" / "hooks" / "rogmem.py").exists()
    write_configs(home, "http://127.0.0.1:18780", programs=["Cursor"])
    again = json.loads((home / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert len(again["hooks"]["sessionStart"]) == 1


def test_no_selection_installs_cursor_and_hermes_hooks(tmp_path):
    home = _home(tmp_path)
    (home / ".hermes" / "config.yaml").write_text("memory:\n  provider: onemem\n", encoding="utf-8")
    assert write_configs(home, "http://127.0.0.1:18780") == ["Cursor", "Hermes", "Codex"]
    hermes = home / ".hermes" / "agent-hooks" / "rogmem.py"
    assert hermes.is_file()
    text = (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert "provider: onemem" in text
    assert "pre_llm_call:" in text and str(hermes) in text
    assert "post_llm_call:" in text
    write_configs(home, "http://127.0.0.1:18780")
    assert text.count(str(hermes)) == (home / ".hermes" / "config.yaml").read_text(encoding="utf-8").count(str(hermes))
    codex = home / ".codex" / "hooks" / "rogmem.py"
    assert codex.is_file()
    registered = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert registered["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == str(codex)
    assert registered["hooks"]["Stop"][0]["hooks"][0]["command"] == str(codex)


def test_hermes_hook_reads_its_own_block_and_joins_existing_events(tmp_path):
    from rogmem.host_hook import _endpoint
    from installer.rogmem_installer import _merge_hermes_hooks

    home = tmp_path / "home"
    config = home / ".hermes" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "other:\n  url: http://127.0.0.1:1/mcp\n  bearer_token: rm_other\n"
        "rogmem:\n  url: http://127.0.0.1:18780/mcp\n  bearer_token: rm_real\n"
        "hooks:\n  pre_llm_call:\n    - command: /bin/echo\n      timeout: 1\n",
        encoding="utf-8",
    )
    assert _endpoint(home, "Hermes") == ("http://127.0.0.1:18780/mcp", "rm_real", "")
    script = home / ".hermes" / "agent-hooks" / "rogmem.py"
    _merge_hermes_hooks(config, str(script))
    text = config.read_text(encoding="utf-8")
    assert text.count("pre_llm_call:") == 1
    assert str(script) in text
    assert "post_llm_call:" in text
    assert "/bin/echo" in text


def test_a_turn_keeps_the_workspace_remote_for_the_rest_of_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "rogmem.host_hook._remote_from_dir",
        lambda directory: "https://github.com/example/memory" if directory == "/work/rogmind" else "",
    )
    seen = []

    def flush(home, program, rows, timeout=30):
        seen.extend(rows)
        return []

    monkeypatch.setattr("rogmem.host_hook._flush", flush)
    handle(
        {
            "hook_event_name": "beforeSubmitPrompt",
            "session_id": "s",
            "prompt": "合成句：这一句属于这个仓库。",
            "workspace_roots": ["/work/rogmind"],
        },
        home=tmp_path,
        program="Cursor",
    )
    handle(
        {
            "hook_event_name": "afterAgentResponse",
            "session_id": "s",
            "text": "合成句：回复也属于这个仓库。",
        },
        home=tmp_path,
        program="Cursor",
    )
    assert [item["project"] for item in seen] == [
        "https://github.com/example/memory",
        "https://github.com/example/memory",
    ]


def test_turns_use_the_same_mcp_door_as_core(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "rogmem.host_hook._endpoint",
        lambda home, program: ("http://127.0.0.1:18780/mcp", "rm_test", "dev"),
    )

    def mcp(url, token, device, name, arguments, timeout=None):
        seen.append((url, name, arguments["role"], token))
        return {"result": {"content": [{"type": "text", "text": "{\"ok\": true}"}]}}

    monkeypatch.setattr("rogmem.host_hook._mcp", mcp)
    left = _flush(
        tmp_path,
        "Cursor",
        [{"session_id": "s", "turn_id": "s:1", "seq": 1, "role": "user", "text": "合成句", "event_at": "t"}],
    )
    assert left == []
    assert seen == [("http://127.0.0.1:18780/mcp", "submit_turn", "user", "rm_test")]


def test_opening_fails_open_and_does_not_retry_for_ten_minutes(tmp_path, monkeypatch):
    calls = []

    def core(home, program, project=""):
        calls.append(program)
        return ""

    monkeypatch.setattr("rogmem.host_hook._core_text", core)
    first = handle({"hook_event_name": "sessionStart", "session_id": "s"}, home=tmp_path, program="Cursor")
    second = handle({"hook_event_name": "sessionStart", "session_id": "s"}, home=tmp_path, program="Cursor")
    assert first == {} and second == {}
    assert calls == ["Cursor"]
    later = handle(
        {"hook_event_name": "pre_llm_call", "session_id": "s", "extra": {"is_first_turn": False}},
        home=tmp_path,
        program="Hermes",
    )
    assert later == {}
    assert calls == ["Cursor"]


def test_the_local_door_knows_this_computer(tmp_path, monkeypatch):
    import argparse

    import rogmem.cli as cli

    seen = {}

    class Inst:
        FIXED_FORWARD_PORT = 18780

        def find_rogmem(self):
            return 1

        def probe_origin(self, origin, cookie=None, timeout=5):
            return {"ok": True, "hub": ""}

        def forward(self, listen, target, home=None, hub=""):
            seen["home"] = home
            seen["listen"] = listen
            seen["hub"] = hub

            class Server:
                def shutdown(self):
                    return None

            return Server()

    def stop(home):
        seen["drained"] = home
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_installer", lambda: Inst())
    monkeypatch.setattr(cli, "_home", lambda args: tmp_path)
    monkeypatch.setattr("rogmem.host_hook.drain_outbox", stop)
    try:
        cli.channel_main(argparse.Namespace())
    except KeyboardInterrupt:
        pass
    assert seen["home"] == tmp_path
    assert seen["listen"] == 18780
    assert seen["drained"] == tmp_path


def test_the_channel_retries_turns_the_hub_has_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "rogmem.host_hook._endpoint",
        lambda home, program: ("http://127.0.0.1:18780/mcp", "rm_test", "dev"),
    )
    sent = []
    hold = {"down": True}

    def mcp(url, token, device, name, arguments, timeout=None):
        sent.append(arguments["text"])
        if hold["down"]:
            return {}
        return {"result": {"content": [{"type": "text", "text": "{\"ok\": true}"}]}}

    monkeypatch.setattr("rogmem.host_hook._mcp", mcp)
    from rogmem.host_hook import drain_outbox

    handle(
        {"hook_event_name": "beforeSubmitPrompt", "session_id": "s", "prompt": "合成句：还没送出。"},
        home=tmp_path,
        program="Cursor",
    )
    handle(
        {"hook_event_name": "beforeSubmitPrompt", "session_id": "s", "prompt": "合成句：这次通了。"},
        home=tmp_path,
        program="Cursor",
    )
    stored = (tmp_path / ".rogmem" / "outbox.jsonl").read_text(encoding="utf-8")
    assert "合成句：还没送出。" in stored and "合成句：这次通了。" in stored
    hold["down"] = False
    assert drain_outbox(tmp_path) == 2
    assert not (tmp_path / ".rogmem" / "outbox.jsonl").exists()
    assert "合成句：还没送出。" in sent and "合成句：这次通了。" in sent


def test_the_inline_upload_keeps_the_host_deadline_and_the_resident_drain_waits(tmp_path, monkeypatch):
    """A turn upload embeds on the hub and takes seconds; the host hook budget is 3s.

    The hook's own drain must stay short so it never overruns the host; the
    resident channel has no host deadline and waits for the hub to accept.
    """
    import rogmem.host_hook as hook

    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": "")
    seen: list[float] = []

    def drain(home, timeout=30):
        seen.append(timeout)
        return 0

    monkeypatch.setattr(hook, "drain_outbox", drain)
    handle({"hook_event_name": "beforeSubmitPrompt", "session_id": "s", "prompt": "合成句。"}, home=tmp_path, program="Cursor")
    assert seen == [hook._INLINE_UPLOAD_TIMEOUT]
    assert hook._INLINE_UPLOAD_TIMEOUT <= 3
    assert hook._DRAIN_UPLOAD_TIMEOUT > hook._INLINE_UPLOAD_TIMEOUT


def test_a_packet_is_injected_once_and_a_failed_post_stays_local(tmp_path, monkeypatch):
    import rogmem.host_hook as hook

    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": "历史包" * 1000)
    drains = []
    real_drain = hook.drain_outbox
    monkeypatch.setattr(hook, "drain_outbox", lambda home, timeout=30: drains.append(home) or 0)
    opened = handle({"hook_event_name": "sessionStart", "session_id": "s"}, home=tmp_path, program="Cursor")
    packet = "【rogmem 历史包】\n" + ("历史包" * 1000)
    assert opened == {"additional_context": packet}
    assert drains == []
    monkeypatch.setattr(hook, "drain_outbox", real_drain)
    again = handle({"hook_event_name": "sessionStart", "session_id": "s"}, home=tmp_path, program="Cursor")
    assert again == {}

    monkeypatch.setattr("rogmem.host_hook._flush", lambda home, program, rows, timeout=30: rows)
    captured = handle(
        {"hook_event_name": "beforeSubmitPrompt", "session_id": "s", "prompt": "合成句：记得这一句。"},
        home=tmp_path,
        program="Cursor",
    )
    assert captured == {}
    stored = (tmp_path / ".rogmem" / "outbox.jsonl").read_text(encoding="utf-8")
    assert "合成句：记得这一句。" in stored

    monkeypatch.setattr("rogmem.host_hook._flush", lambda home, program, rows, timeout=30: [])
    handle(
        {"hook_event_name": "afterAgentResponse", "session_id": "s", "text": "合成句：已经送出。"},
        home=tmp_path,
        program="Cursor",
    )
    assert not (tmp_path / ".rogmem" / "outbox.jsonl").exists()


def test_codebuddy_uses_the_same_hook_and_dsh_gets_a_plugin(tmp_path):
    home = tmp_path / "home"
    (home / ".codebuddy").mkdir(parents=True)
    (home / ".codebuddy" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"pdf": True}}),
        encoding="utf-8",
    )
    (home / ".dsh" / "profiles" / "desktop").mkdir(parents=True)
    (home / ".dsh" / "profiles" / "desktop.backup").mkdir()
    (home / ".dsh" / "profiles" / "desktop" / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"other": "1.0.0"},
                "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
            }
        ),
        encoding="utf-8",
    )
    (home / ".dsh" / "profiles" / "desktop.backup" / "package.json").write_text("{}\n", encoding="utf-8")
    (home / ".dsh" / "cordis.patch.yml").write_text(
        "- insert:\n    - id: keep-me\n      name: other\n",
        encoding="utf-8",
    )
    assert write_configs(home, "http://127.0.0.1:18780", "rm_test", ["CodeBuddy", "DSH"]) == ["CodeBuddy", "DSH"]
    settings = json.loads((home / ".codebuddy" / "settings.json").read_text(encoding="utf-8"))
    assert settings["enabledPlugins"] == {"pdf": True}
    command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert command == str(home / ".codebuddy" / "hooks" / "rogmem.py")
    saved = json.loads((home / ".rogmem" / "hosts" / "DSH.json").read_text(encoding="utf-8"))
    assert saved["token"] == "rm_test"
    assert saved["url"].endswith("/mcp")
    patch = (home / ".dsh" / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "id: keep-me" in patch and "id: rogmem-mcp" in patch
    manifest = json.loads((home / ".dsh" / "profiles" / "desktop" / "package.json").read_text(encoding="utf-8"))
    assert manifest["dependencies"]["other"] == "1.0.0"
    assert manifest["dependencies"]["rogmem-dsh"].startswith("link:")
    assert manifest["dsh"]["profile"]["bundles"] == ["@deepseek-ai/dsh-base", "rogmem-dsh"]
    assert json.loads((home / ".dsh" / "profiles" / "desktop.backup" / "package.json").read_text(encoding="utf-8")) == {}
    assert "alwaysApply: true" in (home / ".codebuddy" / "rules" / "rogmem.mdc").read_text(encoding="utf-8")
    assert "会话压缩" in (home / ".dsh" / "skills" / "rogmem" / "SKILL.md").read_text(encoding="utf-8")
    write_configs(home, "http://127.0.0.1:18780", "rm_test", ["CodeBuddy", "DSH"])
    again = (home / ".dsh" / "cordis.patch.yml").read_text(encoding="utf-8")
    assert again.count("id: rogmem-mcp") == 1
    assert again.count("id: keep-me") == 1
    bundles = json.loads((home / ".dsh" / "profiles" / "desktop" / "package.json").read_text(encoding="utf-8"))
    assert bundles["dsh"]["profile"]["bundles"].count("rogmem-dsh") == 1
    hook = (home / ".rogmem" / "plugins" / "rogmem-dsh" / "rogmem.py").read_text(encoding="utf-8")
    assert "def handle(" in hook


def test_rewriting_where_a_host_points_keeps_its_key(tmp_path):
    home = tmp_path / "home"
    for name in (".cursor", ".hermes", ".codex", ".dsh"):
        (home / name).mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text("memory:\n  provider: onemem\n", encoding="utf-8")
    selected = ["Cursor", "Hermes", "Codex", "DSH"]
    assert write_configs(home, "http://127.0.0.1:18780", "rm_kept", selected) == selected
    assert write_configs(home, "http://127.0.0.1:18780", "", selected) == selected
    assert "Bearer rm_kept" in (home / ".cursor" / "mcp.json").read_text(encoding="utf-8")
    assert "bearer_token: rm_kept" in (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert 'bearer_token = "rm_kept"' in (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    saved = (home / ".rogmem" / "hosts" / "DSH.json").read_text(encoding="utf-8")
    assert '"token": "rm_kept"' in saved
    assert "Bearer rm_kept" in (home / ".dsh" / "cordis.patch.yml").read_text(encoding="utf-8")
    from installer.rogmem_installer import redeem_grant

    def _should_not_ask(body):
        raise AssertionError(body)

    again = redeem_grant(home, "http://127.0.0.1:18780", "rg_test", "DSH", _should_not_ask)
    assert again["ok"] is True and again["status"] == "已接入"
    write_configs(home, "http://127.0.0.1:18780", "rm_next", ["DSH"])
    replaced = (home / ".rogmem" / "hosts" / "DSH.json").read_text(encoding="utf-8")
    assert "rm_next" in replaced
    assert "rm_kept" not in replaced


def test_the_hub_saying_already_does_not_mean_the_key_is_here(tmp_path):
    from installer.rogmem_installer import redeem_grant

    home = tmp_path / "home"
    (home / ".dsh").mkdir(parents=True)
    write_configs(home, "http://127.0.0.1:18780", programs=["DSH"])
    result = redeem_grant(
        home,
        "http://127.0.0.1:18780",
        "rg_test",
        "DSH",
        lambda body: {"ok": True, "already": True},
    )
    assert result["ok"] is False
    assert result["wrote"] is False
    assert "rm_" not in (home / ".rogmem" / "hosts" / "DSH.json").read_text(encoding="utf-8")


def test_one_list_names_every_supported_host():
    from rogmem.engine import HOST_PROGRAMS
    from rogmem.host_hook import _PROGRAMS
    from rogmem.host_rules import _FORBIDDEN

    from installer.rogmem_installer import SUPPORTED

    assert set(SUPPORTED) == set(HOST_PROGRAMS) == _PROGRAMS == set(_FORBIDDEN)


def test_a_plugin_can_name_the_host_without_living_in_its_directory(tmp_path):
    from rogmem.host_hook import invocation

    home, program = invocation(tmp_path / "rogmem.py", ["--program", "DSH", "--home", str(tmp_path)])
    assert program == "DSH"
    assert home == tmp_path
    found_home, found_program = invocation(tmp_path / ".cursor" / "hooks" / "rogmem.py", [])
    assert found_program == "Cursor"
    assert found_home == tmp_path


def test_codex_and_workbuddy_keep_their_own_hooks_and_share_the_script(tmp_path):
    from installer.rogmem_installer import install_host_hooks

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".workbuddy").mkdir()
    (home / ".codex" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"matcher": "", "hooks": [{"type": "command", "command": "/bin/echo keep", "timeout": 10}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (home / ".workbuddy" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"pdf": True}}),
        encoding="utf-8",
    )
    assert install_host_hooks(home, ["Codex", "WorkBuddy"]) == ["Codex", "WorkBuddy"]
    codex = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        item.get("command")
        for group in codex["hooks"]["UserPromptSubmit"]
        for item in group["hooks"]
    ]
    assert "/bin/echo keep" in commands
    assert str(home / ".codex" / "hooks" / "rogmem.py") in commands
    assert "Stop" in codex["hooks"]
    work = json.loads((home / ".workbuddy" / "settings.json").read_text(encoding="utf-8"))
    assert work["enabledPlugins"] == {"pdf": True}
    assert work["hooks"]["SessionStart"][0]["hooks"][0]["command"] == str(home / ".workbuddy" / "hooks" / "rogmem.py")
    install_host_hooks(home, ["Codex", "WorkBuddy"])
    again = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert len(again["hooks"]["UserPromptSubmit"]) == 2


def test_codex_reads_its_toml_and_the_shared_device_file(tmp_path):
    from rogmem.host_hook import _endpoint

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".rogmem").mkdir()
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.other]\nurl = "http://127.0.0.1:1/mcp"\n\n'
        '[mcp_servers.rogmem]\nurl = "http://127.0.0.1:18780/mcp"\nbearer_token = "rm_codex"\n',
        encoding="utf-8",
    )
    (home / ".rogmem" / "device_id").write_text("dev-codex\n", encoding="utf-8")
    assert _endpoint(home, "Codex") == ("http://127.0.0.1:18780/mcp", "rm_codex", "dev-codex")


def test_trae_reads_mcp_from_its_user_dir_not_the_dot_trae_dir(tmp_path):
    from rogmem.host_hook import _endpoint, _trae_mcp_path

    home = tmp_path / "home"
    (home / ".trae").mkdir(parents=True)
    target = _trae_mcp_path(home)
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "rogmem": {
                        "url": "http://127.0.0.1:18780/mcp",
                        "headers": {"Authorization": "Bearer rm_trae"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (home / ".rogmem").mkdir()
    (home / ".rogmem" / "device_id").write_text("dev-trae\n", encoding="utf-8")
    assert _endpoint(home, "Trae") == ("http://127.0.0.1:18780/mcp", "rm_trae", "dev-trae")


def test_trae_hooks_keep_the_version_and_the_shared_script(tmp_path):
    from installer.rogmem_installer import install_host_hooks

    home = tmp_path / "home"
    (home / ".trae").mkdir(parents=True)
    assert install_host_hooks(home, ["Trae"]) == ["Trae"]
    data = json.loads((home / ".trae" / "hooks.json").read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert set(data["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop"}
    commands = [
        item["command"] for group in data["hooks"]["UserPromptSubmit"] for item in group["hooks"]
    ]
    assert str(home / ".trae" / "hooks" / "rogmem.py") in commands
    assert (home / ".trae" / "hooks" / "rogmem.py").is_file()


def test_a_packet_survives_a_state_dir_the_host_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr("rogmem.host_hook._core_text", lambda home, program, project="": "历史包")

    home = tmp_path / "home"
    (home / ".trae").mkdir(parents=True)
    (home / ".rogmem").write_text("not a directory\n", encoding="utf-8")
    result = handle(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "合成句：问一句。"},
        home=home,
        program="Trae",
    )
    assert result == {"hookSpecificOutput": {"additionalContext": "【rogmem 历史包】\n历史包"}}


def test_a_prompt_without_a_session_start_still_loads_once_and_a_stop_reads_the_transcript(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "rogmem.host_hook._core_text",
        lambda home, program, project="": calls.append(program) or "历史包",
    )
    seen = []
    monkeypatch.setattr("rogmem.host_hook._flush", lambda home, program, rows, timeout=30: seen.extend(rows) or [])
    monkeypatch.setattr("rogmem.host_hook._remote_from_dir", lambda directory: "git@github.com:example/memory.git")
    first = handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "cwd": "/work/rogmind",
            "prompt": "合成句：这一问要进记忆。",
        },
        home=tmp_path,
        program="Codex",
    )
    assert first["hookSpecificOutput"]["additionalContext"].startswith("【rogmem 历史包】")
    second = handle(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "合成句：第二问不再取历史包。"},
        home=tmp_path,
        program="Codex",
    )
    assert second == {}
    assert calls == ["Codex"]
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "合成句：过程里的一句。"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "合成句：最后交出去的回复。"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    handle(
        {"hook_event_name": "Stop", "session_id": "s", "transcript_path": str(transcript)},
        home=tmp_path,
        program="Codex",
    )
    assert [item["role"] + ":" + item["text"] for item in seen] == [
        "user:合成句：这一问要进记忆。",
        "user:合成句：第二问不再取历史包。",
        "assistant:合成句：最后交出去的回复。",
    ]
    assert {item["project"] for item in seen} == {"git@github.com:example/memory.git"}


def _gbk_pipe(monkeypatch, raw: bytes) -> io.BytesIO:
    """A pipe whose text wrapper uses a legacy code page, the way a Chinese Windows pipe does."""
    outgoing = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="gbk"))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(outgoing, encoding="gbk"))
    return outgoing


def _run_hook(monkeypatch, home: Path, raw: bytes, reply: str) -> tuple[bytes, dict]:
    from rogmem.host_hook import main

    outgoing = _gbk_pipe(monkeypatch, raw)
    monkeypatch.setattr(sys, "argv", ["rogmem.py", "--program", "WorkBuddy", "--home", str(home)])
    monkeypatch.setattr("rogmem.host_hook._core_text", lambda home, program, project="": reply)
    assert main() == 0
    sys.stdout.flush()
    outbox = home / ".rogmem" / "outbox.jsonl"
    stored = json.loads(outbox.read_text(encoding="utf-8")) if outbox.is_file() else {}
    return outgoing.getvalue(), stored


def test_utf8_hook_json_survives_a_legacy_stdio_code_page(tmp_path, monkeypatch):
    # These UTF-8 bytes are also legal GBK, so a locale decode stores different characters.
    prompt = "青石记下这句原话"
    reply = "回给宿主的一段中文"
    raw = b"\xef\xbb\xbf" + json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": "sess-utf8", "prompt": prompt},
        ensure_ascii=False,
    ).encode("utf-8")
    body, stored = _run_hook(monkeypatch, tmp_path, raw, reply)
    assert stored["text"] == prompt
    assert stored["role"] == "user"
    response = json.loads(body.decode("utf-8"))
    assert reply in response["hookSpecificOutput"]["additionalContext"]


def test_hook_pipe_keeps_text_the_legacy_code_page_rejects(tmp_path, monkeypatch):
    prompt = "青石记下这句原话𠀀"
    reply = "回给宿主的一段中文"
    raw = json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": "sess-utf8", "prompt": prompt},
        ensure_ascii=False,
    ).encode("utf-8")
    body, stored = _run_hook(monkeypatch, tmp_path, raw, reply)
    assert stored["text"] == prompt
    response = json.loads(body.decode("utf-8"))
    assert reply in response["hookSpecificOutput"]["additionalContext"]


def test_hook_pipe_drops_a_payload_that_is_not_utf8(tmp_path, monkeypatch):
    body, stored = _run_hook(monkeypatch, tmp_path, b"\xff\xfe", "")
    assert stored == {}
    assert json.loads(body.decode("utf-8")) == {}


def test_an_already_unicode_stream_is_not_passed_through_a_code_page():
    from rogmem.host_hook import read_outside, write_outside

    assert read_outside(io.StringIO("青石记下这句原话")) == "青石记下这句原话"
    out = io.StringIO()
    write_outside(out, "回给宿主的一段中文")
    assert out.getvalue() == "回给宿主的一段中文"


def test_child_process_bytes_stay_utf8(tmp_path, monkeypatch):
    url = "https://example.test/青石.git"
    seen = {}

    def run(args, **kwargs):
        seen.update(kwargs)

        class Proc:
            stdout = url.encode("utf-8")
            returncode = 0

        return Proc()

    monkeypatch.setattr("rogmem.host_hook.subprocess.run", run)
    from rogmem.host_hook import _remote_from_dir

    assert _remote_from_dir(str(tmp_path)) == url
    assert seen.get("text") is not True
    assert "encoding" not in seen


def test_an_undecodable_child_pipe_does_not_drop_the_turn(tmp_path, monkeypatch):
    def run(args, **kwargs):
        class Proc:
            stdout = b"\xff"
            returncode = 1

        return Proc()

    monkeypatch.setattr("rogmem.host_hook.subprocess.run", run)
    handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "cwd": str(tmp_path),
            "prompt": "青石记下这句原话",
        },
        home=tmp_path,
        program="WorkBuddy",
    )
    stored = (tmp_path / ".rogmem" / "outbox.jsonl").read_text(encoding="utf-8")
    assert "青石记下这句原话" in stored


def test_a_later_user_turn_repeats_the_standing_order(tmp_path, monkeypatch):
    """The packet is a one-time bootstrap. Later turns carry one short read-when-needed line."""
    import rogmem.host_hook as hook

    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": "历史包")
    monkeypatch.setattr(hook, "_endpoint", lambda home, program: ("http://127.0.0.1:18780/mcp", "rm_test", "dev"))
    monkeypatch.setattr(hook, "_flush", lambda home, program, rows, timeout=30: [])

    opened = handle(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "合成句：第一问。"},
        home=tmp_path,
        program="Trae",
    )
    assert opened["hookSpecificOutput"]["additionalContext"].startswith("【rogmem 历史包】")

    later = handle(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "合成句：第二问。"},
        home=tmp_path,
        program="Trae",
    )
    reminder = later["hookSpecificOutput"]["additionalContext"]
    assert "search" in reminder and "宁查勿猜" in reminder
    assert "历史包" not in reminder


def test_an_unenrolled_host_repeats_nothing(tmp_path, monkeypatch):
    import rogmem.host_hook as hook

    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": "历史包")
    monkeypatch.setattr(hook, "_flush", lambda home, program, rows, timeout=30: [])
    markers = tmp_path / ".rogmem" / "hook-core"
    markers.mkdir(parents=True)
    (markers / "Trae-s.ok").write_text("ok\n", encoding="utf-8")
    assert hook._enrolled(tmp_path, "Trae") is False
    later = handle(
        {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "合成句：第二问。"},
        home=tmp_path,
        program="Trae",
    )
    assert later == {}
