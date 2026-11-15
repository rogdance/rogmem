"""Publishing registers the version already running in production."""

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


announce = _load("rogmem_ship_announce", _ROOT / "skills" / "rogmem-ship" / "announce.py")
plan = _load("rogmem_ship_plan", _ROOT / "skills" / "rogmem-ship" / "plan.py")


class _Done:
    def __init__(self, code: int, stderr: str = ""):
        self.code = code
        self.stderr = stderr


def test_release_tag_is_the_production_version():
    assert announce.release_tag("2611081019") == "V1-2611081019-DEV"


def test_bad_stamp_is_refused():
    try:
        announce.release_tag("yesterday")
    except ValueError as error:
        assert "10" in str(error)
    else:
        raise AssertionError("bad stamp was accepted")


def test_create_args_carry_no_image_and_no_prerelease():
    args = announce.create_args("V1-2611081019-DEV")
    assert args[:4] == ["gh", "release", "create", "V1-2611081019-DEV"]
    assert "--repo" in args and "rogdance/rogmem" in args
    assert "--latest" in args
    assert "--prerelease" not in args
    assert not any(part.endswith(".tar") for part in args)


def test_health_mismatch_does_not_create_a_release():
    calls = []

    def run(args):
        calls.append(args)
        return _Done(0)

    try:
        announce.register("2611081019", run, lambda: {"version": "V1-2611081019-PRE", "service": "rogmem"}, lambda: None)
    except SystemExit as error:
        assert "V1-2611081019-DEV" in str(error)
    else:
        raise AssertionError("pre-release health was published")
    assert calls == []


def test_existing_release_is_left_in_place():
    calls = []

    def run(args):
        calls.append(args)
        return _Done(0)

    tag = announce.register(
        "2611081019",
        run,
        lambda: {"version": "V1-2611081019-DEV", "service": "rogmem"},
        lambda: None,
    )
    assert tag == "V1-2611081019-DEV"
    assert calls == [["gh", "release", "view", tag, "--repo", "rogdance/rogmem"]]


def test_default_register_does_not_invoke_the_manifest_generator(monkeypatch):
    monkeypatch.delenv("ROGMEM_RELEASES_MANIFEST_OUT", raising=False)
    calls = []

    def run(args):
        calls.append(args)
        return _Done(0)

    announce.register(
        "2611081019",
        run,
        lambda: {"version": "V1-2611081019-DEV", "service": "rogmem"},
        lambda: None,
    )
    assert all("generate_releases_manifest" not in " ".join(str(part) for part in args) for args in calls)


def test_register_with_env_knob_calls_generator_once_without_pushing(monkeypatch, tmp_path):
    out = tmp_path / "releases.json"
    monkeypatch.setenv("ROGMEM_RELEASES_MANIFEST_OUT", str(out))
    calls = []

    def run(args):
        calls.append(args)
        if "generate_releases_manifest" in " ".join(str(p) for p in args):
            out.write_text('{"schema":"rogmem-releases/1"}', encoding="utf-8")
            return _Done(0)
        return _Done(0)

    announce.register(
        "2611081019",
        run,
        lambda: {"version": "V1-2611081019-DEV", "service": "rogmem"},
        lambda: None,
    )
    assert out.exists()
    generator_calls = [args for args in calls if "generate_releases_manifest" in " ".join(str(p) for p in args)]
    assert len(generator_calls) == 1
    pushes = [args for args in calls if args[:2] == ["git", "push"]]
    assert pushes == []


def test_missing_release_is_created_and_a_ping_failure_still_counts():
    calls = []

    def run(args):
        calls.append(args)
        if args[1] == "release" and args[2] == "view":
            return _Done(1, "not found")
        return _Done(0)

    def ping():
        raise RuntimeError("关于页没接上")

    tag = announce.register(
        "2611081019",
        run,
        lambda: {"version": "V1-2611081019-DEV", "service": "rogmem"},
        ping,
    )
    assert tag == "V1-2611081019-DEV"
    assert calls[1] == announce.create_args(tag)


def test_a_single_word_selects_one_stage():
    assert plan.plan("合版") == {"stages": ["合版"], "target": ""}
    assert plan.plan("部署") == {"stages": ["部署"], "target": ""}
    assert plan.plan("发布") == {"stages": ["发布"], "target": ""}
    assert plan.plan("发版") == {"stages": ["部署"], "target": ""}


def test_pre_release_is_a_deploy_target_and_not_a_publish():
    assert plan.plan("部署到预发布") == {"stages": ["部署"], "target": "pre"}
    assert plan.plan("部署到正式环境") == {"stages": ["部署"], "target": "prod"}


def test_a_combined_sentence_includes_only_the_named_stages():
    assert plan.plan("合版并部署到预发布") == {"stages": ["合版", "部署"], "target": "pre"}
    assert plan.plan("部署然后发布") == {"stages": ["部署", "发布"], "target": "prod"}
    assert plan.plan("合版、部署、发布") == {"stages": ["合版", "部署", "发布"], "target": "prod"}


def test_yitiaolong_runs_all_three_against_production():
    assert plan.plan("一条龙") == {"stages": ["合版", "部署", "发布"], "target": "prod"}
    assert plan.plan("简版一条龙") == {"stages": ["合版", "部署", "发布"], "target": "prod"}


def test_reset_is_a_later_stage_only_when_named():
    assert plan.plan("重置") == {"stages": ["重置"], "target": ""}
    assert plan.plan("一条龙，重置") == {"stages": ["合版", "部署", "发布", "重置"], "target": "prod"}
    assert plan.plan("部署到正式环境然后重置") == {"stages": ["部署", "重置"], "target": "prod"}
    assert "重置" not in plan.plan("一条龙")["stages"]
    assert plan.plan("不要重置，一条龙") == {"stages": ["合版", "部署", "发布"], "target": "prod"}
    assert plan.plan("别重置") == {"stages": [], "target": ""}
    assert plan.plan("无需重置") == {"stages": [], "target": ""}
    assert plan.plan("不用重置") == {"stages": [], "target": ""}


def test_both_destinations_ask_instead_of_guessing():
    chosen = plan.plan("部署到预发布和生产")
    assert chosen["stages"] == ["部署"]
    assert chosen["target"] == "ask"


def test_publishing_and_a_pre_release_deploy_do_not_compose():
    assert plan.plan("部署到预发布并发布")["target"] == "ask"
    assert plan.plan("部署到预发布并发布")["stages"] == ["部署", "发布"]
    assert plan.plan("一条龙到预发布") == {"stages": ["合版", "部署", "发布"], "target": "ask"}
