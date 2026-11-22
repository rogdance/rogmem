"""打包标识必须是发版者显式声明、贯穿全链路的单一真值。

回归背景：旧 Dockerfile 声明了 ``ARG PACK_NONCE`` 却没在写 BUILD_STAMP 的
RUN 里引用它，版本号取的是构建容器墙上时间。BuildKit 只对「RUN 里引用过的
ARG」做缓存失效，于是同一份代码重发会命中缓存里的旧 BUILD_STAMP；预检从镜像
读回旧打包时间，烟测拿旧时间去比对旧页面，全流程「通过」而版本号永远不变。

这些测试不依赖 Docker daemon：把构建时的决策抽成可测的领域函数，再用文本契约
锁住 Dockerfile / smoke.py / SKILL.md 三处的同一份标识。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
ZSPACE_SKILL = ROOT / "skills" / "zspace-release" / "SKILL.md"
SMOKE = ROOT / "skills" / "zspace-release" / "smoke.py"
SHIP_SKILL = ROOT / "skills" / "rogmem-ship" / "SKILL.md"

from rogmem import version  # noqa: E402


# ---------- 构建时的决策：可测的领域函数 ----------

def test_explicit_pack_stamp_wins():
    assert version.resolve_pack_stamp("2611220830") == "2611220830"


def test_pack_stamp_can_come_from_the_build_environment(monkeypatch):
    monkeypatch.setenv("PACK_STAMP", "2611220831")
    assert version.resolve_pack_stamp() == "2611220831"
    assert version.resolve_pack_stamp("2611220832") == "2611220832"  # 显式优先


@pytest.mark.parametrize("bad", ["", "261122083", "26112208301", "abcdefghij", " 2611220830x "])
def test_malformed_pack_stamp_falls_back_to_pack_time(monkeypatch, bad):
    monkeypatch.delenv("PACK_STAMP", raising=False)
    moment = datetime(2026, 11, 22, 3, 4, tzinfo=timezone.utc)
    assert version.resolve_pack_stamp(bad, moment=moment) == version.shanghai_stamp(moment)


def test_fallback_is_the_shanghai_clock_not_the_local_one():
    moment = datetime(2026, 11, 21, 16, 29, tzinfo=timezone.utc)  # 上海 00:29 次日
    assert version.shanghai_stamp(moment) == "2611220029"


def test_a_declared_stamp_produces_a_new_product_version(monkeypatch):
    monkeypatch.setenv("ROGMEM_BUILD_STAMP", "2611220830")
    monkeypatch.setenv("ROGMEM_RELEASE", "DEV")
    assert version.product_version() == "S-V1-2611220830-DEV"
    assert version.product_version() != "S-V1-2611211429-DEV"  # 昨天下午那一版


# ---------- Dockerfile 契约 ----------

def test_dockerfile_declares_and_actually_uses_pack_stamp():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG PACK_STAMP=" in text, "构建参数必须显式声明"
    stamp_run = next(
        (line for line in text.splitlines()
         if line.startswith("RUN ") and "BUILD_STAMP" in line),
        "",
    )
    assert stamp_run, "找不到写 BUILD_STAMP 的 RUN"
    assert "${PACK_STAMP}" in stamp_run, "PACK_STAMP 必须在 RUN 文本里被引用，否则不进缓存键"
    assert "${PACK_NONCE}" in stamp_run, "PACK_NONCE 仍应参与缓存键"
    assert "resolve_pack_stamp" in stamp_run, "写入值必须由领域函数决定"
    assert "shanghai_stamp()" not in stamp_run, "旧的内联取时钟写法已废止"


def test_the_dockerfile_run_really_writes_the_declared_stamp(monkeypatch, tmp_path):
    """不启 Docker、也不起子进程：在当前进程执行那条 RUN 的等价代码。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    line = next(l for l in text.splitlines()
                if l.startswith("RUN ") and "BUILD_STAMP" in l)
    code = line.split('python -c "', 1)[1].rstrip('"')
    target = tmp_path / "BUILD_STAMP"
    code = code.replace("'/app/rogmem/BUILD_STAMP'", repr(str(target)))
    monkeypatch.setenv("PACK_STAMP", "2611220830")
    monkeypatch.setenv("PACK_NONCE", "261122083099")
    monkeypatch.delenv("ROGMEM_BUILD_STAMP", raising=False)
    exec(compile(code, "<Dockerfile RUN>", "exec"), {})  # noqa: S102 - 已在仓库代码内
    assert target.read_text(encoding="utf-8").strip() == "2611220830"


# ---------- 预检必须拒绝旧镜像 ----------

@pytest.fixture
def smoke_module():
    if not SMOKE.is_file():
        pytest.skip("公开仓不含极空间发版技能")
    spec = importlib.util.spec_from_file_location("zspace_release_smoke_under_test", SMOKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_docker_run(monkeypatch, stamp: str):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=f"STAMP {stamp}\n100 /opt/fastembed/model.onnx\n",
            stderr="",
        ),
    )


def test_preflight_rejects_an_image_from_a_previous_pack(smoke_module, monkeypatch):
    _fake_docker_run(monkeypatch, "2611211429")
    monkeypatch.setattr(smoke_module, "check_model_files", lambda entries: [])
    monkeypatch.setattr(smoke_module, "version_for",
                        lambda stamp, lane="": f"S-V1-{stamp}-DEV")
    errors = smoke_module.preflight_image("rogmem:local", "2611220830")
    assert any("不一致" in item and "缓存" in item for item in errors), errors


def test_preflight_accepts_the_image_that_matches_this_pack(smoke_module, monkeypatch):
    _fake_docker_run(monkeypatch, "2611220830")
    monkeypatch.setattr(smoke_module, "check_model_files", lambda entries: [])
    monkeypatch.setattr(smoke_module, "version_for",
                        lambda stamp, lane="": f"S-V1-{stamp}-DEV")
    assert smoke_module.preflight_image("rogmem:local", "2611220830") == []


def test_smoke_cli_exposes_expect_stamp(smoke_module):
    text = SMOKE.read_text(encoding="utf-8")
    assert '"--expect-stamp"' in text
    assert "preflight_image(args.preflight_image, args.expect_stamp" in text


# ---------- 文档契约：全链路共用同一个 STAMP ----------

@pytest.mark.skipif(not ZSPACE_SKILL.is_file(), reason="公开仓不含极空间发版技能")
def test_release_skill_uses_one_declared_stamp_everywhere():
    text = ZSPACE_SKILL.read_text(encoding="utf-8")
    assert "--build-arg PACK_STAMP=\"$STAMP\"" in text
    assert "--expect-stamp \"$STAMP\"" in text
    assert '--rebuild --stamp "$STAMP"' in text
    assert text.count("shanghai_stamp()") >= 2, "生产与 Windows 章节都要自己生成 STAMP"
    assert "<STAMP> 用预检打印的 10 位" not in text, "旧文案会让人抄回旧打包时间"


@pytest.mark.skipif(not SHIP_SKILL.is_file(), reason="公开仓不含合版发布技能")
def test_publish_skill_uses_the_same_stamp():
    text = SHIP_SKILL.read_text(encoding="utf-8")
    assert 'announce.py --stamp "$STAMP"' in text
    assert "不要用从镜像里读回来的旧打包时间" in text
