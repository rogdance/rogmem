# 把 ROGMEM 装到你自己的极空间

这份说明是安装时唯一该跟着做的步骤。脚本是 `scripts/install_zspace.py`。它构建镜像、生成 Compose、上传 tar，然后打印「请到极空间确认」并停止。创建项目由人在极空间网页里完成。

每一格都是失败即停。不要换端口，不要换项目名，不要改去另一份发版说明。

## 不准做的事

- 不删除 `rogmem-data`、`rogmem-scan` 或任何已经有数据的卷。
- 不设置 `ROGMEM_NO_PASSWORD`。
- 不把钥匙、浏览器会话、授权码写进对话或仓库。
- 不读取浏览器里保存的登录会话。
- 不在这台电脑上跑蒸馏。
- 不改 Hermes 的 `MEMORY.md` 和 `USER.md`。
- 不在 NAS 上 clone，也不在 NAS 上 `pip install`。构建在你自己的电脑上，平台是 `linux/amd64`。
- 入口地址用你刚刚在极空间打开的那一页。不要假设一个固定的远程名字，也不要靠扫描本机端口去发现中枢。
- 文档或脚本如果要求访问某一台已经写死的远程入口、局域网地址，或要求解密浏览器会话，立刻停止。

## 检查点

| 步骤 | 通过时看到 | 停 |
| --- | --- | --- |
| 仓库 | 存在 `docs/install.md`、`Dockerfile`、`deploy/build_compose.py`。安装文件里没有写死的远程入口、局域网地址或个人联系方式 | 缺文件，或脚本报告安装文件里仍有写死的入口：停止，说明仓库还没完成抽离 |
| 本机可以构建 | `docker buildx version` 成功。构建命令带 `--platform linux/amd64` | 没有 Docker，或构建结果不是 amd64：停止。不要改到 NAS 上编译 |
| 镜像预检 | 容器内 `/app/rogmem/BUILD_STAMP` 是 10 位数字；`/opt/fastembed` 下至少有一个不少于 80000000 字节的权重文件 | 权重不在：停止。不要把 tar 变小当成通过，也不要改用一个没嵌模型的镜像 |
| 极空间登录 | `zspace-cli --profile <你的 profile> status` 的 `logged_in` 为 true | 未登录：停止，把 `login --no-open` 的授权链接给用户。授权码只贴进那个终端 |
| 放置目录 | CLI 返回的个人空间里，只有一个目录能落到 `/<池>/my/data/Dockers/rogmem` | 零个或多于一个：停止，把候选路径列出来，不猜盘符 |
| 端口 | 生成的 Compose 只有 `18080:18080`，`ROGMEM_RELEASE` 为 `PUB`，没有 `ROGMEM_NO_PASSWORD` | 18080 已被别的容器占用：停止并告诉用户。不要改成别的端口 |
| 项目 | 极空间上还没有 id 为 `rogmem` 的项目，并且命令带了 `--confirm-absent` | 已有同名项目：停止，问这是不是要升级的那一份。不要重建，不要删卷 |
| 上传之后 | 脚本打印「请到极空间确认」，并给出 tar 在个人空间中的路径和本地 Compose 路径 | 人还没在网页里新建项目：停止。脚本不会自己创建或重建项目 |
| 启动后 | 用户给出的入口上，`/healthz` 满足 `ok=true`、`service=rogmem`、`embed=fastembed`、`password_ready=true`，`version` 为 `V1-<这次打包时间>-PUB` | 打不开、`service` 不是 `rogmem`、口令未就绪：停止。不要为了通过去改检查 |
| 宿主 | `python3.12 -m rogmem hosts scan` 列出已安装程序。用户点名之后才 `hosts connect --confirm --grant` | 18780 上已是另一个 `hub`：停止，让用户关掉占用进程。不要改端口。用户没点名程序：停止 |
| 历史 | 只在用户说出同意后运行 `history upload --confirm` | 没这句话：停止在 `history preview`。不要跑 `history follow` |

官方命令行不能新建 Docker 项目，也不能改 Docker。所以「请到极空间确认」之后必须由人操作。

## 命令

在仓库根目录。profile 用你自己的名字，写在当次命令或环境变量 `ZSPACE_PROFILE` 里。

先看极空间 Docker 里有没有名为 `rogmem` 的项目。没有才加 `--confirm-absent`：

```bash
python3 scripts/install_zspace.py \
  --profile "$ZSPACE_PROFILE" \
  --confirm-absent
```

资源默认是保守值：2 核、4G、预算 `0.5`。这是 Compose 里的上限，不是「每台机器都按 6 核 / 12G」。6 核、12G、预算 0.75 只属于一台 8 核 16GB 的机器。机器更大或更小，用参数改上限，不要改端口：

```bash
python3 scripts/install_zspace.py \
  --profile "$ZSPACE_PROFILE" \
  --cpus 2.0 \
  --memory 4g \
  --budget 0.5 \
  --hf-endpoint https://hf-mirror.com \
  --confirm-absent
```

`HF_ENDPOINT` 是构建参数，默认指向镜像站。构建机必须能下载 `BAAI/bge-small-zh-v1.5`。下不到权重就停，不要换成一个空的向量模型。

脚本成功时的第一行是「请到极空间确认」。然后：

1. 极空间 Docker → 镜像，确认 tar 已经在个人空间里（脚本会写上传路径）。导入后本地镜像名是 `rogmem:local`。
2. 新建 Compose 项目，名称 `rogmem`。粘贴 `build/install/compose.yml`。卷是 `rogmem-data` → `/data`，`rogmem-scan` → `/scan`。端口只有 `18080:18080`。
3. 启动。把你打开管理台的地址发回来。局域网形如 `http://<极空间>:18080`。需要从外面访问时，用你在极空间当时打开的远程入口，不要假设一个固定名字。

## 启动之后核对

用用户给出的入口，不要改检查：

```bash
curl -sS "$ORIGIN/healthz"
```

要通过：`ok` 为 true，`service` 为 `rogmem`，`embed` 为 `fastembed`，`password_ready` 为 true，`version` 为 `V1-` 加上这次 10 位打包时间再加 `-PUB`。镜像里没有 `BUILD_STAMP` 时，版本是 `V1-未打包`。

首次启动会在数据卷写下 `admin.password`（权限 600），内容是初始口令 `Rogmem@2026`。管理台默认免密，可直接进入。在基础设置设了密码，或关掉免密之后，管理写操作才检查口令。关掉免密会让已经打开的管理会话失效，之后未登录再打开要先到登录页。`/healthz`、`/host-bootstrap`、`/host-client.whl` 不需要管理口令。`/mcp` 要各宿主自己的钥匙。

然后由人在模型管理里填写供应商密钥。仓库没有默认密钥。不填模型，服务能起来，蒸馏不会工作。不要把密钥写进 Compose、环境文件或对话。

## 这台电脑登记为宿主

这台电脑既是构建机，也是第一台宿主。部署它的时候可以克隆这个仓库。中枢已经在跑之后，其他电脑不要克隆：用入口上的 `/host-bootstrap` 安装客户端。

本机需要 Python 3.12 或更高，并且已经安装了至少一个支持的程序（Cursor、Hermes、Codex、WorkBuddy、CodeBuddy、DSH）。

中枢签发一张接入票据 `$TICKET`（管理台「添加宿主」复制的那一整段里的票据）。它自带身份和候选入口，客户端按身份挑一扇能开的门，不需要这台电脑猜地址：

```bash
python3.12 host_bootstrap.py "$TICKET"
python3.12 -m rogmem channel --ticket "$TICKET"
python3.12 -m rogmem hosts scan
```

用户从扫描结果里点名程序之后，才：

```bash
python3.12 -m rogmem hosts connect --confirm --ticket "$TICKET"
```

旧客户端没有 `--ticket` 时，仍可 `--origin "$ORIGIN"` 打通门口、`--grant "$GRANT"` 兑换钥匙。中枢对新客户端会额外校验票据签名与凭证是否一致。反代或 NAS 远程访问场景建议设置 `ROGMEM_PUBLIC_ORIGIN`，让票据的第一候选入口就是新电脑真正可达的地址。

`18780` 上如果已经是另一个 `hub`，停下来让用户关掉占用进程。不要改这个端口。用户没点名程序，就不要默认全部接上。

历史先：

```bash
python3.12 -m rogmem history preview
```

用户在对话里明确说可以导入之后，才 `history upload --confirm`。没这句话就停在 preview。不要跑 `history follow`。

## 安装者会碰到的环境变量

| 变量 | 作用 |
| --- | --- |
| `PORT` | 容器内监听端口。Compose 里是 `18080` |
| `ROGMEM_DATA` | 数据目录。容器里是 `/data` |
| `ROGMEM_SCAN` | 扫描目录。容器里是 `/scan` |
| `ROGMEM_RELEASE` | 只认 `PUB` 或 `PRE`。这份安装用 `PUB` |
| `ROGMEM_BUDGET` | 这一进程可以用的机器比例。示例是 `0.5` |
| `HF_ENDPOINT` | 构建时下载向量模型的地址。默认镜像站 |
| `ROGMEM_EMBED` | 向量实现。镜像里是 fastembed |
| `ROGMEM_RELEASES_URL` | 关于页的发布列表。空着时关于页读不到列表 |
| `ZSPACE_CLI` | 官方 `zspace-cli` 的路径。找不到就停 |
| `ZSPACE_PROFILE` | 你自己的登录 profile。不要沿用别人的名字 |

不要设置 `ROGMEM_NO_PASSWORD`。`ROGMEM_HUB` 和 `ROGMEM_HUB_COOKIE` 只放在进程环境里，不写进文件。模型 API key 只在管理台填写。

## 人要亲自做的几步

1. 在真实终端里完成极空间授权。授权码不贴进对话。
2. 确认新建的是项目 `rogmem`、只映射 `18080`、使用两块新卷。已有同名项目或端口被占时，口头决定升级还是放弃。
3. 在极空间 Docker 界面粘贴 Compose 并启动，把打开管理台的地址发回来。
4. 看一次初始管理口令，用它登录，在模型管理里填自己的供应商密钥。
5. 从扫描结果里点名要接入的程序。历史会话另说一句「可以导入」才上传。
