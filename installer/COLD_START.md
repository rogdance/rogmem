# 新机器冷启动

把下面整段交给要接入的那台电脑上已有的 Agent。不要寻找或制作 Windows / macOS 安装包、`.dmg`、`.msi`、Tauri 或 Electron。

部署这份中枢的那台电脑可以克隆公开仓库。中枢已经在跑之后，其他电脑走 `/host-bootstrap`，不要克隆。

---

请完成本机 rogmem 接入。官方路径是 `python -m rogmem` CLI 子命令加浏览器打开 Web 管理台，不做打包客户端。客户端里的库是 `installer/rogmem_installer.py`，不要再当独立安装器跑。

前置：本机已装 Python 3.12 或更高（先跑 `python3 --version`）。没有就先装官方 Python。

这份中枢的身份是 ___ROGMEM_HUB___ 。复制这段话时的入口是 ___ROGMEM_ORIGIN___ 。入口会变，身份不变。另一个身份的服务不是这份中枢。
把身份放进环境变量 ROGMEM_HUB，不要写进文件，也不要写进对话。如果被转到别的站点，把已经打开这个入口的浏览器会话放进环境变量 ROGMEM_HUB_COOKIE，同样不要写进文件或对话。
部署这份中枢的那台电脑可以克隆公开仓库。中枢已经在跑之后，其他电脑用这份中枢的 /host-bootstrap 把客户端装进这个 Python，不要克隆 git 仓库。装的是它此刻给出的客户端。入口打不开时，本机门口 http://127.0.0.1:18780 的 /healthz 若仍是这个身份，就从门口装。
然后 python -m rogmem channel --origin ___ROGMEM_ORIGIN___ 。ROGMEM_HUB 和 ROGMEM_HUB_COOKIE 留在这个进程的环境里。这个命令把本机门口接到这个身份。门口已经通向它，就用这个门口，不要再开一个转发。命令若说门口被占着且不是这个身份，停掉占用它的进程后再运行一次，不要改用别的端口。命令若退出并给出本机门口，配置就用这个门口；进程保持运行时，配置基址用 http://127.0.0.1:18780 。

`channel` 把这份中枢 forward 到本机 `127.0.0.1:18780`。入口必须是用户刚打开的那个地址。没有身份时不要改去扫描别的端口；`find_rogmem()` 不是安装时的默认发现。有身份时，它只接身份相同的那一扇门。配置写 `http://127.0.0.1:18780/mcp`。

`python -m rogmem hosts connect --dry-run` 列出将要改的文件，确认后再加 `--confirm --grant`。历史先 `history preview` 看条数，用户明确同意后再 `history upload --confirm`。没有这句话就停在 preview。不要运行 `history follow`。不要把原文贴进对话。确认之前不导入。不要在本机蒸馏，不要写 crystal。

Windows 上 Cursor 常见在 `%USERPROFILE%\AppData\Roaming\Cursor`，Linux 在 `~/.config/Cursor`，只有配置目录时在 `~/.cursor`。配置仍写用户目录下的 `.cursor/mcp.json`。
