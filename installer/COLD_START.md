# 新机器冷启动

把下面整段交给要接入的那台电脑上已有的 Agent。不做打包客户端，不要寻找或制作 Windows / macOS 安装包、`.dmg`、`.msi`、Tauri 或 Electron。

---

请完成本机 rogmem 接入。官方路径是 `python -m rogmem` CLI 子命令加浏览器打开 Web 管理台，不做打包客户端。客户端里的库是 `installer/rogmem_installer.py`，不要再当独立安装器跑。

前置：本机已装 Python 3.12 或更高（先跑 `python3 --version`）。没有就先装官方 Python。

中枢签发的是一张自包含的接入票据：`___ROGMEM_TICKET___`。里面是身份、短时凭证、过期时间和一组有序的候选入口。地址只是候选，身份才是权威；入口换了、带反代子路径、或隔着登录会话，都还能接上。地址不由打开管理台的那台浏览器决定。

这份中枢的身份是 ___ROGMEM_HUB___ 。票据里带着这扇门此刻可能的入口清单，入口会变，身份不变。另一个身份的服务不是这份中枢。
把身份放进环境变量 ROGMEM_HUB，把接入票据放进环境变量 ROGMEM_TICKET，都不要写进文件，也不要写进对话。如果被转到别的站点，把已经打开这个入口的浏览器会话放进环境变量 ROGMEM_HUB_COOKIE，同样不要写进文件或对话。
入口清单（按顺序试，打不开就换下一个）：___ROGMEM_ENTRANCES___
用这份中枢的 /host-bootstrap 把客户端装进这个 Python：从上面能打开的入口下载 /host-bootstrap，再用当前 Python 运行它，把接入票据作为唯一参数传进去：python host_bootstrap.py ___ROGMEM_TICKET___ 。它会按票据里的身份逐个探活入口，只从同一个身份的入口取 /host-client.whl 来装。装的是它此刻给出的客户端。入口都打不开时，本机门口 http://127.0.0.1:18780 的 /healthz 若仍是这个身份，就从门口装。不要克隆 git 仓库，不要登录 GitHub。
然后 python -m rogmem channel --ticket ___ROGMEM_TICKET___ 。ROGMEM_HUB 、 ROGMEM_TICKET 和 ROGMEM_HUB_COOKIE 留在这个进程的环境里。这个命令按身份从票据的入口清单里挑一扇能开的门，把本机门口接到这个身份。门口已经通向它，就用这个门口，不要再开一个转发。命令若说门口被占着且不是这个身份，停掉占用它的进程后再运行一次，不要改用别的端口。命令若退出并给出本机门口，配置就用这个门口；进程保持运行时，配置基址用 http://127.0.0.1:18780 。

`channel` 把这份中枢 forward 到本机 `127.0.0.1:18780`。它从票据里逐个探活入口，只采纳 `/healthz` 回答同一身份的那一扇门；没有身份时，仍用 `find_rogmem()` 在本机找一个 rogmem。中枢在兑换钥匙时还会再校验票据签名，伪造的入口策略或掉包的凭证都过不去。配置写 `http://127.0.0.1:18780/mcp`。

`python -m rogmem hosts scan --dry-run` 列出将要改的文件，然后 `python -m rogmem hosts connect --confirm --ticket ___ROGMEM_TICKET___`。CLI 会从票据里取出准入凭证向中枢兑换，不需要用户再粘贴一次。旧客户端没有 `--ticket` 时，才改用 `--grant` 传提示里的一次性准入凭证。不要把原文贴进对话。确认之前不导入。不要在本机萃取，不要写 crystal。

运维可以在中枢上设置 `ROGMEM_PUBLIC_ORIGIN`，声明新电脑真正应该使用的规范入口；它会排在候选清单第一位。反代子路径会完整保留，不会像浏览器取页面源那样被丢掉。

Windows 上 Cursor 常见在 `%USERPROFILE%\AppData\Roaming\Cursor`，Linux 在 `~/.config/Cursor`，只有配置目录时在 `~/.cursor`。配置仍写用户目录下的 `.cursor/mcp.json`。
