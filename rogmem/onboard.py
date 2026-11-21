"""Prompts shown when adding hosts. The image renders them from the server.

A copied prompt carries an admission ticket, not an address. The ticket is
signed by the hub and holds an ordered entrance policy; the client probes those
entrances and accepts only the one answering with the hub's identity. Nothing
here is derived from the browser that happened to open the page.
"""

from __future__ import annotations

from rogmem.admission import ENTRANCES_MARK, HUB_ENV, TICKET_ENV, TICKET_MARK

GRANT_MARK = "___ROGMEM_GRANT___"
ORIGIN_MARK = "___ROGMEM_ORIGIN___"
HUB_MARK = "___ROGMEM_HUB___"
HUB_COOKIE_ENV = "ROGMEM_HUB_COOKIE"
DOOR = "http://127.0.0.1:18780"


def host_client_instruction() -> str:
    """How this computer reaches the hub, driven by the ticket's identity.

    The ticket, not ``location.origin``, is the authority. Entrances may move,
    be proxied under a sub-path or need a browser session; the identity does not.
    Every command is executable as written: the ticket is passed on argv, so an
    agent never has to decode base64 or guess which environment variable to set.
    """
    return (
        f"这份中枢的身份是 {HUB_MARK} 。票据里带着这扇门此刻可能的入口清单，入口会变，身份不变。另一个身份的服务不是这份中枢。\n"
        f"把身份放进环境变量 {HUB_ENV}，把接入票据放进环境变量 {TICKET_ENV}，都不要写进文件，也不要写进对话。"
        f"如果被转到别的站点，把已经打开这个入口的浏览器会话放进环境变量 {HUB_COOKIE_ENV}，同样不要写进文件或对话。\n"
        f"入口清单（按顺序试，打不开就换下一个）：{ENTRANCES_MARK}\n"
        "用这份中枢的 /host-bootstrap 把客户端装进这个 Python：从上面能打开的入口下载 /host-bootstrap，"
        f"再用当前 Python 运行它，把接入票据作为唯一参数传进去：python host_bootstrap.py {TICKET_MARK} 。"
        "它会按票据里的身份逐个探活入口，只从同一个身份的入口取 /host-client.whl 来装。装的是它此刻给出的客户端。"
        f"入口都打不开时，本机门口 {DOOR} 的 /healthz 若仍是这个身份，就从门口装。不要克隆 git 仓库，不要登录 GitHub。\n"
        f"然后 python -m rogmem channel --ticket {TICKET_MARK} 。"
        f"{HUB_ENV} 、 {TICKET_ENV} 和 {HUB_COOKIE_ENV} 留在这个进程的环境里。"
        "这个命令按身份从票据的入口清单里挑一扇能开的门，把本机门口接到这个身份。"
        "门口已经通向它，就用这个门口，不要再开一个转发。"
        "命令若说门口被占着且不是这个身份，停掉占用它的进程后再运行一次，不要改用别的端口。"
        f"命令若退出并给出本机门口，配置就用这个门口；进程保持运行时，配置基址用 {DOOR} 。\n"
    )


def onboard_prompt(secret: str) -> str:
    """One prompt for any installed host. The ticket enrolls every installed program.

    ``secret`` is the human-readable short-lived capability, kept for the legacy
    ``--grant`` path. The ticket carries the same secret plus the hub identity
    and the entrance policy, so a fresh computer needs exactly one copied blob.
    """
    key = (secret or "").strip()
    return (
        "请把这台电脑接入 ROGMEM 记忆中枢。用户在宿主管理点了「添加宿主」并复制这段话，就是同意接入本机已安装的宿主。\n\n"
        f"一次性准入凭证：{key}\n"
        f"接入票据：{TICKET_MARK}\n"
        "这张凭证大约 20 分钟内有效，只能给这台电脑用。每个程序领各自的钥匙。不要把凭证或钥匙原文写进对话。\n\n"
        "不要做安装包，不要在这台电脑另存一份记忆。先确认 Python 是 3.12 或更高。极空间远程访问要开着，rogmem 听 18080。\n\n"
        "使用 python -m rogmem 子命令完成本机接入，不要再当独立安装器跑。\n"
        + host_client_instruction()
        + f"python -m rogmem hosts scan --dry-run。配置地址写 {DOOR}/mcp。\n\n"
        "扫描 Cursor、Trae、Hermes、Codex、WorkBuddy、CodeBuddy、DSH。把已经安装的列出来，让用户挑选要接入的。\n"
        "用户点了其中几个，hosts connect 就用重复的 --program 只接这几个。用户没有挑选，就不要传 --program，接入全部已发现且受支持的宿主。\n"
        "只处理配置里还没有 rm_ 钥匙的程序。把上面这段接入票据原样用 --ticket 传给 "
        f"python -m rogmem hosts connect --confirm --ticket {TICKET_MARK} 。"
        "CLI 会从票据里取出准入凭证向中枢兑换，中枢还会校验票据签名；底层仍是 connect_hosts 与 redeem_grant，allow_real=True。"
        "不要向用户再要 token，不要让用户输入、粘贴或确认这把凭证，不要停下来等用户再输入一次。\n"
        "先 hosts connect --dry-run 看将要改的文件，接着立刻 --confirm --ticket。"
        "旧客户端没有 --ticket 时，才改用 --grant 传上面的一次性准入凭证，不要同时传两个。\n"
        "状态已经是启用、配置里已经有钥匙的，跳过。不改配置，不重领钥匙，也不要为此向用户要 token。"
        "返回已停用或已拒绝的保持不动，告诉用户去宿主列表处理。\n"
        "新领到的钥匙立刻写入该程序的配置。设备名用主机名，不要写成「本机」。\n"
        "同一次接入给选中的宿主写入全局规则。Cursor、Trae、Hermes、Codex、WorkBuddy、CodeBuddy 安装同一份 hook。DSH 安装 rogmem-dsh 插件。没选中的宿主不要写规则，也不要装 hook 或插件。"
        "规则在各宿主的全局位置，不写进某一个仓库。hook 失败不能挡住宿主继续工作。绝对禁止改写 Hermes 的 MEMORY.md 和 USER.md。\n\n"
        "接入并启用之后，立刻在后台运行 python -m rogmem history follow，持续把 Hermes 账本里还没送过的回合送进中枢。"
        "之后新出现的回合也继续送。看宿主列表的「待导入会话数」，这个数会随着导入变少。不要再问要不要导入，不要停下来等第二次确认。"
        "不要把会话原文贴进对话。\n"
        "Hermes 领到钥匙后，记忆提供方是 rogmem。新的一轮对话由这个提供方用同一条回合身份送进去。已经在跑的 Hermes 要重新打开才换上。\n\n"
        "不要删除 NAS 上的 rogmem 数据。"
    )


def host_update_prompt() -> str:
    """Refresh client and host-side plugin. Not a new admission: no ticket, no key.

    This computer already has a door and a remembered identity, so it does not
    need an entrance policy. Keeping it out of this prompt avoids a ticket mark
    that no server step would fill.
    """
    return (
        "请把本机的宿主端插件更新到最新：先刷新本机客户端，再给本机所有已支持的宿主重写全局规则和 hook。\n"
        "这不是重新接入。不要新领钥匙，不要改已有钥匙或配置，不要动记忆数据，不要改端口或卷。\n\n"
        f"本机门口 {DOOR} 已经记住这份中枢的身份 {HUB_MARK}。用它的 /host-bootstrap 刷新客户端，"
        "不要克隆 git 仓库，不要登录 GitHub，也不要把浏览器会话写进文件或对话。\n"
        "然后运行 python -m rogmem hosts update --confirm，把本机所有已发现且受支持的宿主的全局规则和 hook 更新到最新。\n"
        "看不到要更新的宿主时，先运行 python -m rogmem hosts scan --dry-run 列一遍再更新。\n"
        "规则写在宿主的全局位置，不写进某一个仓库；hook 覆盖同名旧文件；DSH 更新插件。已停用或已拒绝的宿主保持不动，告诉用户去宿主列表处理。\n"
        "更新完成后回报每个宿主的结果，不要把任何钥匙或原文贴进对话。\n"
        "不要删除 NAS 上的 rogmem 数据。"
    )
