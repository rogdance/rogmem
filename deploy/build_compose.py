"""Emit image-referencing Compose files. Do not embed business source.

Build on the user's computer, then import the image on 极空间:

    docker buildx build --load --platform linux/amd64 -t rogmem:local .
    docker save -o rogmem-local.tar rogmem:local

Runtime deps (libgomp, venv) live in the Dockerfile, not in a compose `sh -c`.
CPU, memory, budget, and HF_ENDPOINT are install parameters. The committed
example uses a small default, not the 6-core / 12G ceiling of one 16GB machine.
See docs/install.md.

This file is the open-source example. Do not paste it over the existing
rogmem-dev production project. That project stays at 6 cores, 12G, budget 0.75,
and its release suffix is DEV.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "rogmem:local"
OUTPUTS = (
    ROOT / "compose.yml",
    Path(__file__).with_name("极空间-rogmem-compose.yml"),
)
SIZE_LIMIT = 20_000


def render(
    *,
    cpus: str = "2.0",
    mem_limit: str = "4g",
    budget: str = "0.5",
    hf_endpoint: str = "https://hf-mirror.com",
    release: str = "PUB",
    image: str = IMAGE,
) -> str:
    if release not in {"DEV", "PUB", "PRE"}:
        raise SystemExit("ROGMEM_RELEASE 只认 DEV、PUB 或 PRE")
    for label, value in (
        ("cpus", cpus),
        ("mem_limit", mem_limit),
        ("budget", budget),
        ("hf_endpoint", hf_endpoint),
        ("image", image),
    ):
        if not str(value).strip() or any(char in str(value) for char in "\n\r"):
            raise SystemExit(f"compose 参数 {label} 不能为空或换行")
    text = f"""# 粘贴到极空间：Docker，Compose，新建项目，名称填 rogmem。
# 已有同名项目时不要重建，不要删除 rogmem-data 或 rogmem-scan。
# 镜像在你自己的电脑上构建并导入，不要在 NAS 上编译，也不要把业务源码打进这份文件。
#   docker buildx build --load --platform linux/amd64 -t {image} .
# 示例资源是保守默认（2 核、4G、预算 0.5）。6 核、12G、预算 0.75 只属于一台 8 核 16GB 的机器，不要抄到更小的机器上。
# 这是开源示例。不要用它覆盖 rogmem-dev 上已经在跑的生产项目。
# 向量模型在构建镜像时放进 /opt/fastembed。构建机必须能下载 BAAI/bge-small-zh-v1.5。HF_ENDPOINT 是构建参数。
# 启动后用你自己打开的入口看 /healthz。端口只有 18080:18080。
# 首次启动在数据卷写入 admin.password。用这一次明文登录。不要设置 ROGMEM_NO_PASSWORD。
services:
  rogmem:
    image: {image}
    container_name: rogmem
    restart: unless-stopped
    cpus: "{cpus}"
    mem_limit: {mem_limit}
    ports:
      - "18080:18080"
    environment:
      PORT: "18080"
      ROGMEM_RELEASE: "{release}"
      ROGMEM_BUDGET: "{budget}"
      ROGMEM_DATA: /data
      ROGMEM_SCAN: /scan
      HF_ENDPOINT: {hf_endpoint}
    volumes:
      - rogmem-data:/data
      - rogmem-scan:/scan
volumes:
  rogmem-data:
  rogmem-scan:
"""
    if "H4sI" in text or "rogmem-pack" in text:
        raise SystemExit("compose payload must not embed gzip source")
    if len(text) >= SIZE_LIMIT:
        raise SystemExit(f"compose is {len(text)} bytes; purity limit is {SIZE_LIMIT}")
    return text


def main() -> None:
    yml = render()
    for out in OUTPUTS:
        out.write_text(yml, encoding="utf-8")
        print(f"{out} {out.stat().st_size}")


if __name__ == "__main__":
    main()
