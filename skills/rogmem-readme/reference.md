# README 主张从哪来

改 README 时对照这张表。左列是允许写的话，右列是现行出处。出处消失，这句话就从 README 删掉。

| README 里的话 | 出处 |
| --- | --- |
| 首段产品定义，原句 | `rogmem/ui.py` 的 `about_page` |
| GitHub。不写个人邮箱和电话 | 同一函数里的关于页链接 |
| 人物速写、工程速写、最近摘录的顺序 | `rogmem/app.py` 的 `guide` |
| 默认 15000 字 | `rogmem/engine.py` 的 `CORE_BUDGET_DEFAULT`，基础设置页同一数字 |
| 五类页 | `rogmem/pages_md.py` 的 `PAGE_TYPES` |
| 管线阶段：解析、草稿、并页、速写 | `rogmem/progress.py` 的 `STAGES` |
| 工人，以及模型沿用关系 | `rogmem/ui.py` 的 `pipeline_page` 列出六个工人；沿用关系在 `rogmem/engine.py` 的 `llm_target` 与 `_judge_model` |
| 说法对不上在并页里裁决 | `rogmem/prompts.py` 的 `INTEGRATE_PAGE_SYSTEM` |
| 并页交回的正文直接落地 | `rogmem/engine.py` 并页落地后调用 `_write_sketch` |
| 萃取前替换密钥模样的字符串 | `rogmem/engine.py` 的 `_llm_extract` |
| 知识文件类型 | `rogmem/ui.py` 的 `FILE_TYPES` |
| 相同内容只留一条 | `rogmem/engine.py` 的 `hash_dedup_enabled` |
| 宿主名单 | `rogmem/engine.py` 的 `HOST_PROGRAMS` |
| DSH 用插件，其余用同一份 hook | `rogmem/ui.py` 宿主页说明，`rogmem/onboard.py` |
| 镜像监听 18080，数据在 `/data`，扫描在 `/scan` | `Dockerfile` |
| 管理台随这个端口打开，MCP 仍要钥匙 | `rogmem/app.py` 的 `_admin_ok` |
| 本机 `channel` 转到 18780 | `rogmem/cli.py` |
| 部署这台电脑可以克隆。中枢已经在跑之后，其他电脑用 `/host-bootstrap`，不克隆。历史要先 preview，用户明确同意后才 upload | `rogmem/onboard.py` 的 `host_client_instruction`，`installer/COLD_START.md`，`docs/install.md` |
| 本机 CLI 不另存记忆 | `rogmem/cli.py` 模块说明 |
| Python 3.12 或更高 | `installer/rogmem_installer.py` |
| MCP 工具表 | `rogmem/app.py` 里每个 `@mcp.tool()` |
| 版本号形状 | `rogmem/version.py` |
| 图谱只画尚未结束的关系 | `rogmem/ui.py` 的 `graph_page` |

## 禁止出现

- 电话、住址、雇主、会话原文、密钥、令牌、内网地址、远程访问域名。
- 生产库怎么清空、卷怎么删、端口改成别的。这些在 `AGENTS.md` 和发版技能里。
- 预发布端口、打包时间的具体某一版、没在仓库里的评测分数。
- 许可证名称。仓库里没有 `LICENSE` 时，README 不宣布一种许可证。
- 徽章。没有公开的构建状态或许可证，就不贴。
- 提示词正文、栏目清单、为某一个错例写的例子。
- 第二只吉祥物、第二份英文 README、把管理台每个字段抄一遍。

## 术语

界面和代码用哪个词，README 就用哪个。记忆主人、速写、结晶、宿主、情景会话、知识文件、历史包，不改写成同义词。

## 章节顺序

1. 首屏：吉祥物、名称、英文一句、导航、产品定义原句、一句场景、三行表
2. 它解决什么
3. 一次会话怎么用记忆
4. 记忆长什么样
5. 从原文到结晶
6. 已经接上的宿主
7. 在自己的机器上跑起来
8. 智能体怎么调用
9. 版本与联系
