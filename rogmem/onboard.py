"""Prompt shown when adding hosts. It lives with the server so the image can render it."""

from __future__ import annotations

GRANT_MARK = "___ROGMEM_GRANT___"
ORIGIN_MARK = "___ROGMEM_ORIGIN___"
HUB_MARK = "___ROGMEM_HUB___"
HUB_ENV = "ROGMEM_HUB"
HUB_COOKIE_ENV = "ROGMEM_HUB_COOKIE"


def host_client_instruction() -> str:
    """How this computer reaches the hub and gets python -m rogmem.

    The copied entrance is how the page was opened. The hub id is which memory
    that page belongs to. 18780 is only the local door.
    """
    return (
        f"这份中枢的身份是 {HUB_MARK} 。复制这段话时的入口是 {ORIGIN_MARK} 。"
        "入口会变，身份不变。另一个身份的服务不是这份中枢。\n"
        f"把身份放进环境变量 {HUB_ENV}，不要写进文件，也不要写进对话。"
        f"如果被转到别的站点，把已经打开这个入口的浏览器会话放进环境变量 {HUB_COOKIE_ENV}，同样不要写进文件或对话。\n"
        "用这份中枢的 /host-bootstrap 把客户端装进这个 Python。装的是它此刻给出的客户端。"
        "入口打不开时，本机门口 http://127.0.0.1:18780 的 /healthz 若仍是这个身份，就从门口装。"
        "不要克隆 git 仓库，不要登录 GitHub。\n"
        f"然后 python -m rogmem channel --origin {ORIGIN_MARK} 。"
        f"{HUB_ENV} 和 {HUB_COOKIE_ENV} 留在这个进程的环境里。"
        "这个命令把本机门口接到这个身份。门口已经通向它，就用这个门口，不要再开一个转发。"
        "命令若说门口被占着且不是这个身份，停掉占用它的进程后再运行一次，不要改用别的端口。"
        "命令若退出并给出本机门口，配置就用这个门口；进程保持运行时，配置基址用 http://127.0.0.1:18780 。\n"
    )


def onboard_prompt(secret: str) -> str:
    """One prompt for any installed host. The secret enrolls every installed program on this computer."""
    key = (secret or "").strip()
    return (
        "请把这台电脑接入 ROGMEM 记忆中枢。用户在宿主管理点了「添加宿主」并复制这段话，就是同意接入本机已安装的宿主。\n\n"
        f"一次性准入凭证：{key}\n"
        "这张凭证大约 20 分钟内有效，只能给这台电脑用。每个程序领各自的钥匙。不要把凭证或钥匙原文写进对话。\n\n"
        "不要做安装包，不要在这台电脑另存一份记忆。先确认 Python 是 3.12 或更高。极空间远程访问要开着，rogmem 听 18080。\n\n"
        "使用 python -m rogmem 子命令完成本机接入，不要再当独立安装器跑。\n"
        + host_client_instruction()
        + "python -m rogmem hosts scan --dry-run。配置地址写 http://127.0.0.1:18780/mcp。\n\n"
        "扫描 Cursor、Trae、Hermes、Codex、WorkBuddy、CodeBuddy、DSH。把已经安装的列出来，让用户挑选要接入的。\n"
        "用户点了其中几个，hosts connect 就用重复的 --program 只接这几个。用户没有挑选，就不要传 --program，接入全部已发现且受支持的宿主。\n"
        "只处理配置里还没有 rm_ 钥匙的程序。上面的准入凭证已经在这段提示里。你自己取出来，用 --grant 传给 "
        "python -m rogmem hosts connect --confirm。不要向用户再要 token，不要让用户输入、粘贴或确认这把凭证。\n"
        "先 hosts connect --dry-run 看将要改的文件，接着立刻 --confirm --grant。"
        "底层仍是 connect_hosts 与 redeem_grant，allow_real=True。不要停下来等用户再输入一次。\n"
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
