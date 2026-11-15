"""Host client published by the hub the computer can already reach.

A new computer does not clone a git remote. It downloads this archive from
the same origin that answered /healthz, puts that directory on PYTHONPATH,
and runs python -m rogmem. The archive is the host role only: imports the
host commands actually reach, plus files those commands address from the
checkout root. The server entry is not part of it.
"""

from __future__ import annotations

import ast
import hashlib
import io
import zipfile
from pathlib import Path

BAKED_NAME = "host_client.whl"
WHEEL_NAME = "rogmem_host-1.0.0-py3-none-any.whl"
_DIST_INFO = "rogmem_host-1.0.0.dist-info"
SERVER_ENTRY = "rogmem.app"
_LOCAL_ROOTS = {"rogmem", "installer"}
_SKIP_DIRS = {"__pycache__", "node_modules"}
_SEEDS = (
    "rogmem/__init__.py",
    "rogmem/__main__.py",
    "rogmem/cli.py",
    "installer/rogmem_installer.py",
)


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def baked_path() -> Path:
    return Path(__file__).resolve().parent / BAKED_NAME


def bundle_filename() -> str:
    return WHEEL_NAME


def bundle_bytes(root: Path | None = None) -> bytes | None:
    """Wheel bytes, or None when this process has no client to give."""
    if root is not None:
        if _source_ready(root):
            return _wheel(client_files(root))
        return None
    base = package_root()
    if _source_ready(base):
        return _wheel(client_files(base))
    baked = baked_path()
    if baked.is_file():
        return baked.read_bytes()
    return None


def bake_bundle(root: Path | None = None, dest: Path | None = None) -> Path:
    """Write the wheel. The image does this, then removes the checkout copies."""
    base = root or package_root()
    target = dest or (base / "rogmem" / BAKED_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_wheel(client_files(base)))
    return target


def client_files(root: Path | None = None) -> dict[str, bytes]:
    base = root or package_root()
    files: dict[str, bytes] = {}
    queue = [base / rel for rel in _SEEDS]
    seen: set[Path] = set()
    while queue:
        path = queue.pop()
        if not path.is_file():
            continue
        path = path.resolve()
        if path in seen or not _inside(base, path):
            continue
        seen.add(path)
        rel = path.relative_to(base.resolve()).as_posix()
        module = _module_name(rel)
        if _is_server(module):
            continue
        raw = path.read_bytes()
        files[rel] = raw
        for imported in _imports(rel, raw.decode("utf-8")):
            if _is_server(imported) or imported.split(".", 1)[0] not in _LOCAL_ROOTS:
                continue
            found = _module_file(base, imported)
            if found is not None:
                queue.append(found)
        if rel == "installer/rogmem_installer.py":
            for payload in _checkout_payloads(raw.decode("utf-8")):
                _add_payload(base, payload, files)
    if "rogmem/__main__.py" not in files or "installer/rogmem_installer.py" not in files:
        raise FileNotFoundError("宿主客户端不完整，无法打包")
    return files


def _source_ready(root: Path) -> bool:
    return (root / "installer" / "rogmem_installer.py").is_file() and (root / "rogmem" / "cli.py").is_file()


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _is_server(module: str) -> bool:
    return module == SERVER_ENTRY or module.startswith(SERVER_ENTRY + ".")


def _module_name(rel: str) -> str:
    parts = rel.split("/")
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1])
    stem = parts[-1][:-3] if parts[-1].endswith(".py") else parts[-1]
    return ".".join([*parts[:-1], stem])


def _module_file(root: Path, module: str) -> Path | None:
    parts = module.split(".")
    direct = root.joinpath(*parts).with_suffix(".py")
    if direct.is_file():
        return direct
    init = root.joinpath(*parts, "__init__.py")
    if init.is_file():
        return init
    return None


def _imports(rel: str, source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            if not node.module:
                continue
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
            continue
        base = _relative_base(rel, node.level)
        if node.module:
            found.add(f"{base}.{node.module}" if base else node.module)
            continue
        for alias in node.names:
            found.add(f"{base}.{alias.name}" if base else alias.name)
    return found


def _relative_base(rel: str, level: int) -> str:
    parts = rel.split("/")[:-1]
    if level > 1:
        parts = parts[: -(level - 1)]
    return ".".join(parts)


def _checkout_payloads(source: str) -> list[str]:
    """Paths the installer addresses from the checkout root, not via import.

    A shorter join is only the left-hand side of a longer one, so it is not
    its own payload. `parents[1] / "rogmem" / "host_hook.py"` contributes the
    file, not the whole rogmem tree.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        parts = _div_chain(node)
        if parts:
            found.add("/".join(parts))
    return [
        rel
        for rel in sorted(found)
        if not any(other.startswith(rel + "/") for other in found)
    ]


def _div_chain(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _div_chain(node.left)
        right = node.right
        if left is not None and isinstance(right, ast.Constant) and isinstance(right.value, str):
            return [*left, right.value]
        return None
    if _is_checkout_root(node):
        return []
    return None


def _is_checkout_root(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    index = node.slice
    if isinstance(index, ast.Constant):
        if index.value != 1:
            return False
    else:
        return False
    parents = node.value
    if not isinstance(parents, ast.Attribute) or parents.attr != "parents":
        return False
    resolved = parents.value
    if not isinstance(resolved, ast.Call):
        return False
    func = resolved.func
    if not isinstance(func, ast.Attribute) or func.attr != "resolve":
        return False
    path_call = func.value
    if not isinstance(path_call, ast.Call):
        return False
    name = path_call.func
    if not isinstance(name, ast.Name) or name.id != "Path":
        return False
    return any(isinstance(arg, ast.Name) and arg.id == "__file__" for arg in path_call.args)


def _add_payload(root: Path, rel: str, files: dict[str, bytes]) -> None:
    path = (root / rel).resolve()
    if not _inside(root, path):
        return
    if path.is_file():
        files[rel] = path.read_bytes()
        return
    if not path.is_dir():
        return
    for child in path.rglob("*"):
        if not child.is_file() or _SKIP_DIRS.intersection(child.parts):
            continue
        name = child.relative_to(root.resolve()).as_posix()
        if name.startswith("/") or ".." in Path(name).parts:
            continue
        files[name] = child.read_bytes()


def _wheel(files: dict[str, bytes]) -> bytes:
    payload = dict(files)
    if "installer/__init__.py" not in payload:
        payload["installer/__init__.py"] = b"# package\n"
    payload[f"{_DIST_INFO}/METADATA"] = (
        "Metadata-Version: 2.1\n"
        "Name: rogmem-host\n"
        "Version: 1.0.0\n"
        "Summary: rogmem host client\n"
        "Requires-Python: >=3.12\n"
        "Requires-Dist: cryptography\n"
    ).encode()
    payload[f"{_DIST_INFO}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: rogmem\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    ).encode()
    record_name = f"{_DIST_INFO}/RECORD"
    lines = []
    for name in sorted(payload):
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(name)
        digest = hashlib.sha256(payload[name]).hexdigest()
        lines.append(f"{name},sha256={digest},{len(payload[name])}")
    payload[record_name] = ("\n".join([*lines, f"{record_name},,"]) + "\n").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(payload):
            info = zipfile.ZipInfo(name)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload[name])
    return buffer.getvalue()
