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
ARG PACK_NONCE=
RUN python -c "from pathlib import Path; from rogmem.version import shanghai_stamp; Path('/app/rogmem/BUILD_STAMP').write_text(shanghai_stamp(), encoding='utf-8')"
# 新电脑从 /host-client.whl 安装本机命令。打好轮子后删掉源目录，容器里无参数的 python -m rogmem 仍然只提供 serve。
COPY installer ./installer
COPY host ./host
RUN python -c "from rogmem.host_client import bake_bundle; bake_bundle()" \
    && rm -rf installer host
ENV ROGMEM_DATA=/data ROGMEM_SCAN=/scan PORT=18080
EXPOSE 18080
CMD ["python", "-m", "rogmem"]
