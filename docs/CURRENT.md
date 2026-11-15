# 现行与过期

日期：2026-11-08

做方案时只看现行。下面列出的稿子是当时的设计或实验记录，文首已标「已过期」。和代码不一致时，以代码为准。

## 现行

| 做什么 | 看哪里 |
| --- | --- |
| 蒸馏、并页、速写、专名、身份的提示 | `rogmem/prompts.py` |
| 页面类型和正文怎么写 | `rogmem/pages_md.py` |
| 给外部代理的工具 | `rogmem/app.py` |
| 合版、部署、发布 | `skills/rogmem-ship/SKILL.md`、`skills/zspace-release/SKILL.md` |
| 对外介绍 | `README.md`。改它之前读 `skills/rogmem-readme/SKILL.md` |

整合裁决这一步，程序仍会从 `docs/prompts/wiki-integrate-adjudicate-prompt-v1.md` 抽出 System prompt 段去调用。抽出来的是运行中的提示，不是一份还可以拿来改页面结构的设计说明。其中的槽位、固定模块已经过期。

## 已过期

这些文件描述的是旧方案：固定章节、槽位、默认补「暂无来源」、D5 演变链。不要用它们判断现在的页面、检索或工具。

- `docs/prompts/wiki-distill-draft-prompt-v5.md`（文末 §D，含 D5）。模拟程序仍会读取其中 A/B 段，线上蒸馏不读这份。
- `docs/prompts/wiki-integrate-adjudicate-prompt-v1.md`（见上：只剩抽出的提示还在跑）
- `docs/prompts/README.md`
- `docs/rogmem-tech-architecture-v3.md`
- `docs/rogmem-方案审阅-v1.md`
- `docs/rogmem-架构CTO评审-v1.md`
- `docs/wiki-distill-merge-arch-note.md`
- `docs/wiki-integrate-context-injection-note.md`
- `docs/wiki-crystal-lifecycle-note.md`
- `docs/superpowers/specs/` 下 2026-10 的产品和技术方案
- `docs/superpowers/loop/` 与 `docs/reports/` 是当时的轮次记录，不是现行规格
