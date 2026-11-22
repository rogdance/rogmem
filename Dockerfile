FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
# HF_ENDPOINT 是构建参数，默认镜像站。构建机必须能下载 BAAI/bge-small-zh-v1.5。
ARG HF_ENDPOINT=https://hf-mirror.com
ENV PATH="/opt/venv/bin:$PATH" \
    HF_ENDPOINT=${HF_ENDPOINT} \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    ROGMEM_EMBED_BAKED=/opt/fastembed
RUN mkdir -p /opt/fastembed \
    && python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-zh-v1.5', cache_dir='/opt/fastembed')"
# 运行时要读的仓库文件见 rogmem/runtime_files.py。新增一条就要在这里 COPY。
COPY docs/prompts ./docs/prompts
COPY rogmem ./rogmem
# 打包标识必须由发版者显式声明，并在下面这条 RUN 里被引用，才能进入镜像层缓存键。
# 旧版只声明了 PACK_NONCE 却没用它：同一份代码重发时命中的是缓存里的旧 BUILD_STAMP，
# 预检读回旧打包时间，烟测拿旧时间比对旧页面，全流程「通过」而版本号永远不变。
# PACK_STAMP 为空/非法时回退到打包时刻的北京时间，兼容没有改命令的旧发版机。
ARG PACK_NONCE=
ARG PACK_STAMP=
RUN PACK_NONCE="${PACK_NONCE}" PACK_STAMP="${PACK_STAMP}" python -c "from pathlib import Path; from rogmem.version import resolve_pack_stamp; Path('/app/rogmem/BUILD_STAMP').write_text(resolve_pack_stamp(), encoding='utf-8')"
# 新电脑从 /host-client.whl 安装本机命令。打好轮子后删掉源目录，容器里无参数的 python -m rogmem 仍然只提供 serve。
COPY installer ./installer
COPY host ./host
RUN python -c "from rogmem.host_client import bake_bundle; bake_bundle()" \
    && rm -rf installer host
ENV ROGMEM_DATA=/data ROGMEM_SCAN=/scan PORT=18080
EXPOSE 18080
CMD ["python", "-m", "rogmem"]
