---
name: rogmem-readme
description: 维护仓库根目录 README.md 的对外介绍。在用户要改宣传 README、项目介绍、吉祥物、关于页口径，或改了宿主、MCP 工具、结晶类型、开场历史包、管线阶段这些 README 已经写过的行为时使用。
---

# 维护 ROGMEM 的 README

README 是给打开仓库的人看的产品介绍。它跟着现在跑着的产品走，不跟着过期设计稿走。

正文只放在 `skills/rogmem-readme/`。`.cursor/skills/rogmem-readme` 是指向这里的符号链接，不要另写一份。

## 先核对

1. 读 `docs/CURRENT.md` 的「现行」。过期稿里的栏目、槽位、固定章节，不写进 README。
2. 读 `rogmem/ui.py` 里 `about_page` 的那句产品定义。README 首段必须原样保留这一句。
3. 要改的每一句事实，先在现行代码里找到它。找不到就不写。

主张对照表在 [reference.md](reference.md)。

## 改哪一节

章节顺序固定。产品多了一块用户能看见的能力，加在最接近的那一节里。只有整块能力在现有章节里无处安放，才新增一级标题，并同步 `tests/test_readme_contract.py` 的标题列表。

| 用户能看见的变化 | 改这里 |
| --- | --- |
| 关于页那句定义、GitHub 地址 | 首段，以及「版本与联系」。个人邮箱和电话不写进这两处 |
| 开场装入什么、字数默认值 | 「一次会话怎么用记忆」和首屏那张表 |
| 页的类型 | 「记忆长什么样」 |
| 管线阶段、工人、裁决词 | 「从原文到结晶」 |
| 宿主名单或接入方式 | 「已经接上的宿主」 |
| 镜像端口、卷、本机命令 | 「在自己的机器上跑起来」 |
| 新增或改名 MCP 工具 | 「智能体怎么调用」 |
| 界面栏目改称呼（如「情景会话」→「原始会话」） | 见下面「改称呼时」 |

## 口径

- 中文。英文只留标题下那一句。
- 术语与界面一致：记忆主人、速写、结晶、宿主、原始会话、文件资料、历史包。
- 句子写已经发生的行为。愿望写在「现状」里，并标明还没有做成功能。
- 命令必须是仓库里真有的。端口、路径、默认字数从代码抄，不靠记忆。
- 首屏三行表只留最能区分这个产品的三件事。新特性进后面的章节。

## 头图

文件是 `docs/brand/rogmem-lockup.svg`：图标加字标的 ROGMEM 锁定版式，字标颜色用 `prefers-color-scheme` 在日间黑、暗夜白之间切换，图标渐变 `#FF3763 → #FF7038` 两种底色不变。README 里只出现这一次，宽度 440 居中，替代文本写清是 ROGMEM 的标识，不要再描述浣熊。换图时仍把 SVG 放到这个路径，保持同一套 `@media (prefers-color-scheme)` 字标色和渐变色号。字标已经读作 ROGMEM，这张图下面不再叠一个纯文字 ROGMEM 的一级标题，保留副标题。旧的吉祥物图片 `docs/brand/rogmem-mascot.jpeg` 留在仓库里不删，没有别的地方引用它。

## 改称呼时

一个称呼会在好几个地方同时出现，漏改一处，界面或测试里就会留下旧词。按这张表一起改：

| 地方 | 改什么 |
| --- | --- |
| `rogmem/ui.py` | `CONSOLE` 顶栏、`sources_page` 的 title 和空栏文案、`source_detail` 的 title、相关注释 |
| `README.md` | 两样东西的条目、mermaid 的来源框、「现状」和管理台栏目那两句 |
| `skills/rogmem-readme/` | 口径里的术语表、`reference.md` 的主张对照表行名 |
| `skills/rogbench/SKILL.md` | description 和正文里的旧词 |
| `tests/` | 锁 UI 文案的断言（`test_source_nav.py`、`test_scene_kind_labels.py` 等）和 `tests/live_check.py` 的验收检查 |
| `scripts/` | 样例文案，例如 `verify_raw_text_wrap.py` 的示例正文标题 |

不改：`tab=session` / `tab=file` 这类路由参数、`sources.kind` 底层值、测试文件名，以及 `docs/superpowers/specs` 和 `plans` 里带日期的历史设计稿、计划（那是当时的记录，不是现行口径）。

改完核三件事：① 跑 README 契约测试和锁文案的测试，和 `main` 基线比失败集，不能有新增失败；② 直接生成两页 HTML，确认新词出现、旧词消失；③ `git diff main --stat` 只该有文案行。

分支照仓库规矩走任务分支 + 独立 worktree；合入前先 `git merge main --no-edit`，并行任务可能已经推进了 main。是否合并、发版等用户一句话，不自己上生产。

## 改完

跑：

```bash
.venv/bin/python -m pytest tests/test_readme_contract.py -q
```

测试失败就改 README 或测试里的标题契约，直到通过。没有 `.venv` 时，用仓库在用的 Python 3.12 跑同一条。

不因为改了 README 就部署，也不改生产端口和卷。用户没说写入 GitHub，就停在本地提交之前。
