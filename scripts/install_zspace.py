"""First install of ROGMEM onto the user's own 极空间.

The success path checks the tree, builds an amd64 image, writes Compose,
uploads the tar, prints 请到极空间确认, and stops. A person creates the
project in the 极空间 Docker page. This script does not read a browser
session, does not delete volumes, and does not rebuild an existing project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.build_compose import render as render_compose
from scripts.public_tree import blocked_text

IMAGE = "rogmem:local"
PROJECT_ID = "rogmem"
MODEL_MIN_BYTES = 80_000_000
INSTALL_SCAN = (
    "docs/install.md",
    "Dockerfile",
    "deploy/build_compose.py",
    "compose.yml",
    "scripts/install_zspace.py",
    "README.md",
)

PREFLIGHT = r"""
import os
stamp=open('/app/rogmem/BUILD_STAMP',encoding='utf-8').read().strip()
print('STAMP '+stamp)
for root, dirs, files in os.walk('/opt/fastembed'):
    for name in files:
        path=os.path.join(root, name)
        try:
            size=os.path.getsize(path)
        except OSError:
            continue
        print(f'{size} {path}')
"""


class Stop(Exception):
    def __init__(self, step: str, message: str):
        self.step = step
        super().__init__(message)


def repo_ready(root: Path) -> None:
    missing = [
        name
        for name in ("docs/install.md", "Dockerfile", "deploy/build_compose.py")
        if not (root / name).is_file()
    ]
    if missing:
        raise Stop("仓库", "缺文件：" + "、".join(missing) + "。停止，仓库还没完成抽离。")
    dirty = []
    for name in INSTALL_SCAN:
        path = root / name
        if path.is_file() and blocked_text(path.read_text(encoding="utf-8", errors="replace")):
            dirty.append(name)
    if dirty:
        raise Stop("仓库", "安装文件里仍有写死的入口或个人联系方式：" + "、".join(dirty) + "。停止，仓库还没完成抽离。")


def personal_spaces(payload: dict) -> list[str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw = data.get("personal_spaces") or []
    spaces = []
    for item in raw:
        if isinstance(item, str) and item.startswith("/"):
            spaces.append(item.rstrip("/"))
        elif isinstance(item, dict):
            for key in ("path", "dir"):
                value = item.get(key)
                if isinstance(value, str) and value.startswith("/"):
                    spaces.append(value.rstrip("/"))
                    break
    if spaces:
        return list(dict.fromkeys(spaces))
    listing = data.get("device_dirs")
    if isinstance(listing, dict):
        listing = listing.get("list") or []
    if isinstance(listing, list):
        marker = "/my/data"
        for item in listing:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str):
                continue
            index = path.find(marker)
            if index == -1:
                continue
            spaces.append(path[: index + len(marker)].rstrip("/"))
    return list(dict.fromkeys(spaces))


def destination_candidates(payload: dict) -> list[str]:
    return [f"{space}/Dockers/{PROJECT_ID}" for space in personal_spaces(payload) if space.endswith("/my/data")]


def pick_destination(payload: dict) -> str:
    found = destination_candidates(payload)
    if len(found) != 1:
        listed = "\n".join(found) if found else "（没有）"
        raise Stop(
            "放置目录",
            "个人空间里能对应这次 Docker 项目的目录不是一个。候选：\n"
            + listed
            + "\n停止。不要猜盘符，不要换项目名。",
        )
    return found[0]


def compose_text(*, cpus: str, mem_limit: str, budget: str, hf_endpoint: str) -> str:
    return render_compose(cpus=cpus, mem_limit=mem_limit, budget=budget, hf_endpoint=hf_endpoint, release="PUB")


def compose_ok(text: str) -> None:
    mappings = re.findall(r'"(\d+):(\d+)"', text)
    if mappings != [("18080", "18080")]:
        raise Stop("端口", "生成的 Compose 不是只有 18080:18080。停止。不要改成别的端口。")
    if 'ROGMEM_RELEASE: "PUB"' not in text:
        raise Stop("端口", "ROGMEM_RELEASE 不是 PUB。停止。")
    if "ROGMEM_NO_PASSWORD:" in text or "ROGMEM_NO_PASSWORD=" in text:
        raise Stop("端口", "Compose 设置了 ROGMEM_NO_PASSWORD。停止。不要设置这个变量。")


def parse_preflight(stdout: str) -> tuple[str, list[tuple[str, int]]]:
    stamp = ""
    entries = []
    for line in stdout.splitlines():
        if line.startswith("STAMP "):
            stamp = line.split(" ", 1)[1].strip()
            continue
        size, _sep, path = line.partition(" ")
        if size.isdigit() and path:
            entries.append((path, int(size)))
    return stamp, entries


def preflight_ok(stdout: str) -> str:
    stamp, entries = parse_preflight(stdout)
    if not re.fullmatch(r"\d{10}", stamp):
        raise Stop("镜像预检", f"构建后的打包时间不是 10 位数字（{stamp!r}）。停止。")
    weights = [size for path, size in entries if "fastembed" in path and size >= MODEL_MIN_BYTES]
    if not weights:
        raise Stop(
            "镜像预检",
            f"镜像里没有不少于 {MODEL_MIN_BYTES} 字节的 /opt/fastembed 权重。停止。不要改用一个没嵌模型的镜像。",
        )
    return stamp


def resolve_cli(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = (env.get("ZSPACE_CLI") or "").strip()
    candidates = [Path(override)] if override else []
    candidates.append(Path.home() / ".cursor/skills/zspace-nas-agent-toolkit/scripts/zspace-cli")
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise Stop("极空间登录", "没有找到官方 zspace-cli。先安装极空间官方 Agent Skill。不要改去读取浏览器会话。")


def _run(command: list[str], runner) -> subprocess.CompletedProcess[str]:
    if runner is None:
        return subprocess.run(command, check=False, capture_output=True, text=True)
    return runner(command)


def docker_ready(runner) -> None:
    result = _run(["docker", "buildx", "version"], runner)
    if result.returncode != 0:
        raise Stop("本机可以构建", "没有 docker buildx。停止。不要改到 NAS 上编译。")


def build_image(root: Path, hf_endpoint: str, runner) -> None:
    command = [
        "docker",
        "buildx",
        "build",
        "--load",
        "--platform",
        "linux/amd64",
        "--build-arg",
        f"HF_ENDPOINT={hf_endpoint}",
        "-t",
        IMAGE,
        str(root),
    ]
    result = _run(command, runner)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise Stop("本机可以构建", "镜像构建失败。停止。不要改到 NAS 上编译。\n" + detail)


def assert_amd64(runner) -> None:
    result = _run(["docker", "image", "inspect", "--format", "{{.Architecture}}", IMAGE], runner)
    arch = (result.stdout or "").strip()
    if result.returncode != 0 or arch != "amd64":
        raise Stop("本机可以构建", f"构建结果不是 linux/amd64（{arch or '未知'}）。停止。不要改到 NAS 上编译。")


def read_image(runner) -> str:
    result = _run(
        ["docker", "run", "--rm", "--platform", "linux/amd64", "--entrypoint", "python", IMAGE, "-c", PREFLIGHT],
        runner,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise Stop("镜像预检", "打不开构建好的镜像。停止。\n" + detail)
    return preflight_ok(result.stdout or "")


def save_tar(directory: Path, stamp: str, runner) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tar = directory / f"rogmem-local-{stamp}.tar"
    result = _run(["docker", "save", "-o", str(tar), IMAGE], runner)
    if result.returncode != 0 or not tar.is_file():
        raise Stop("上传", "没有导出镜像 tar。停止。")
    return tar


def cli_json(cli_path: Path, profile: str, args: list[str], runner, *, yes: bool = False, local_root: str | None = None) -> dict:
    command = [str(cli_path), "--profile", profile, "--timeout", "180"]
    if local_root:
        command.extend(["--local-root", local_root])
    command.extend(args)
    if yes:
        command.append("--yes")
    result = _run(command, runner)
    raw = ((result.stdout or "") or (result.stderr or "")).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise Stop("极空间登录", raw or "zspace-cli 没有返回 JSON") from None
    if result.returncode != 0 or payload.get("ok") is not True:
        error = payload.get("error") or {}
        raise Stop("极空间登录", f"zspace-cli 失败：{error.get('code')} {error.get('message')}")
    return payload


def require_login(cli_path: Path, profile: str, runner) -> None:
    if not profile:
        raise Stop("极空间登录", "没有 profile。用 --profile 或 ZSPACE_PROFILE 传入你自己的名字。")
    status = cli_json(cli_path, profile, ["status"], runner)
    if (status.get("data") or {}).get("logged_in") is not True:
        raise Stop(
            "极空间登录",
            "未登录。请用户在真实终端执行 "
            f"{cli_path} --profile {profile} login --no-open ，"
            "授权码由用户自己贴进那个终端。不要索取、不要代填账号、密码或授权码。",
        )


def remote_size(payload: dict) -> int | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("size", "file_size", "filesize", "fsize", "bytes"):
        value = data.get(key)
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _file_info(cli_path: Path, profile: str, path: str, runner) -> dict | None:
    try:
        return cli_json(cli_path, profile, ["file", "info", "--params", json.dumps({"path": path})], runner)
    except Stop:
        return None


def ensure_directory(cli_path: Path, profile: str, directory: str, runner) -> None:
    """Create /<pool>/my/data/Dockers/rogmem when it is not there yet. Never delete."""
    if _file_info(cli_path, profile, directory, runner) is not None:
        return
    pool_data, _sep, rest = directory.rpartition("/Dockers/")
    if not pool_data or rest != PROJECT_ID:
        raise Stop("放置目录", f"目录不是这次项目的路径：{directory}")
    for parent, name in ((pool_data, "Dockers"), (f"{pool_data}/Dockers", PROJECT_ID)):
        target = f"{parent}/{name}"
        if _file_info(cli_path, profile, target, runner) is not None:
            continue
        cli_json(
            cli_path,
            profile,
            ["file", "mkdir", "--params", json.dumps({"parent": parent, "name": name})],
            runner,
            yes=True,
        )


def upload_tar(cli_path: Path, profile: str, tar: Path, directory: str, runner) -> str:
    dest = f"{directory.rstrip('/')}/{tar.name}"
    params = {"local_path": tar.name, "dest_path": dest}
    cli_json(
        cli_path,
        profile,
        ["file", "upload", "--params", json.dumps(params, ensure_ascii=False)],
        runner,
        yes=True,
        local_root=str(tar.parent),
    )
    info = cli_json(cli_path, profile, ["file", "info", "--params", json.dumps({"path": dest})], runner)
    size = remote_size(info)
    local = tar.stat().st_size
    if size != local:
        raise Stop("上传", f"上传后的大小是 {size!r}，本地 tar 是 {local} 字节。停止。")
    return dest


def confirm_message(remote_tar: str, compose_path: Path) -> str:
    return (
        "请到极空间确认\n"
        f"镜像 tar：{remote_tar}\n"
        f"Compose：{compose_path}\n"
        "在极空间 Docker 界面新建项目，名称填 rogmem，粘贴这份 Compose，只映射 18080:18080，"
        "使用新卷 rogmem-data 和 rogmem-scan，然后启动。\n"
        "不要删除已有数据卷。不要重建已经存在的同名项目。不要设置 ROGMEM_NO_PASSWORD。\n"
        "启动后把你打开管理台的那个地址发回来，再核对 /healthz。"
    )


def project_stop() -> Stop:
    return Stop(
        "项目",
        "先到极空间 Docker 界面确认还没有 id 为 rogmem 的项目。"
        "没有，才带 --confirm-absent 再跑。已有同名项目就停在这里："
        "不要重建，不要删除 rogmem-data 或 rogmem-scan，先问这是不是要升级的那一份。",
    )


def install(args: argparse.Namespace, *, runner=None, cli_path: Path | None = None) -> str:
    root = Path(args.root).resolve()
    repo_ready(root)
    docker_ready(runner)
    profile = (args.profile or os.environ.get("ZSPACE_PROFILE") or "").strip()
    cli = cli_path or resolve_cli()
    require_login(cli, profile, runner)
    space = cli_json(cli, profile, ["file", "space", "--params", "{}"], runner)
    directory = pick_destination(space)
    text = compose_text(
        cpus=args.cpus,
        mem_limit=args.memory,
        budget=args.budget,
        hf_endpoint=args.hf_endpoint,
    )
    compose_ok(text)
    out = root / "build" / "install"
    out.mkdir(parents=True, exist_ok=True)
    compose_path = out / "compose.yml"
    compose_path.write_text(text, encoding="utf-8")
    if not args.confirm_absent:
        raise project_stop()
    build_image(root, args.hf_endpoint, runner)
    assert_amd64(runner)
    stamp = read_image(runner)
    tar = save_tar(out, stamp, runner)
    ensure_directory(cli, profile, directory, runner)
    remote = upload_tar(cli, profile, tar, directory, runner)
    return confirm_message(remote, compose_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 ROGMEM 第一次装到用户自己的极空间，然后停下来等人确认。")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--profile", default="")
    parser.add_argument("--cpus", default="2.0")
    parser.add_argument("--memory", default="4g")
    parser.add_argument("--budget", default="0.5")
    parser.add_argument("--hf-endpoint", default="https://hf-mirror.com")
    parser.add_argument(
        "--confirm-absent",
        action="store_true",
        help="用户已经在极空间确认没有 id 为 rogmem 的项目",
    )
    return parser


def main(argv: list[str] | None = None, *, runner=None, cli_path: Path | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        message = install(args, runner=runner, cli_path=cli_path)
    except Stop as exc:
        print(f"停止于{exc.step}：{exc}", file=sys.stderr)
        return 2
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
