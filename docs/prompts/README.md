> **已过期。** 不要用本目录的说明做现行方案判断。蒸馏提示在 `rogmem/prompts.py`。清单见 [`docs/CURRENT.md`](../CURRENT.md)。

# rogmem Wiki 蒸馏相关 Prompt 索引

日期：2026-10-29（Asia/Shanghai）  
状态：**维护者已审的设计真源（已入库）**；**尚未接入** runtime（不 import、不翻转 `DISTILLER_STORE_WRITE_ENABLED`，写库开关保持 `False`）  
主架构：`docs/rogmem-tech-architecture-v3.md` §6（已与 integrate v1.4 / draft v5.3 同步；本分支未收录该架构文）

## 管线位置（draft → match → **integrate-adjudicate** → gate）

```
Draft（独立结晶，v5.3：叙述语种解耦；仅 evidence 原语种）
  → Match（程序：name/aliases）
  → Integrate-Adjudicate（单 worker，v1.4）
        注入：唯一强命中 = 双侧全文 + 全量 facts 证据（五类词条通用）
        identity / conflicts / ops / 可选 body_md_revised
  → PublishGate（连续子串；只验 evidence，不按叙述语种设门）
```

零命中 → 新建，不调整合裁决。  
`new` → 升活跃；`maybe` → `deferred` 全自动消化（人零介入），不改旧库直至和解或到期按 new。  
**不按简历特例**；人物/组织/产品/项目/概念同一套。

## 文件

| 文件 | 职责 | 状态 |
| --- | --- | --- |
| [`wiki-distill-draft-prompt-v5.md`](./wiki-distill-draft-prompt-v5.md) | **Draft v5.3**（维护者已审）：瘦信封 + body_md；叙述语种解耦；**仅** evidence 原语种 | 主契约 |
| [`wiki-integrate-adjudicate-prompt-v1.md`](./wiki-integrate-adjudicate-prompt-v1.md) | **整合裁决**：身份 + patch + 冲突；全文注入分级 | **现行真源 v1.4** |
| `../wiki-integrate-context-injection-note.md` | 注入分级说明 | 已定策略（本分支未收录） |
| [`../sim-dual-resume-report.md`](../sim-dual-resume-report.md) | 双简历模拟报告 | 已跑；驱动 v1.1–v1.3 |
| `wiki-dedup-prompt-v1.md` | 词条去重（旧） | **superseded**（本分支未收录原稿，契约并入整合裁决） |
| `wiki-conflict-adjudicate-prompt-v1.md` | 冲突裁决（旧） | **superseded**（本分支未收录原稿，契约并入整合裁决） |
| v2 §7.4-hist | 旧 Merge-only | **superseded** → 整合裁决 `ops` |

## 共同纪律

1. **高维可迁移**：五类实体；当前态/时间窗/口径冲突模型；不堆题型细则。  
2. **程序只做形状门**：JSON / 枚举 / id∈候选 / 连续子串 / ops↔conflicts 一致性；不审文采。  
3. **evidence 原语种原文**；叙述语种与材料语种解耦；缺项「暂无信息」。  
4. **读全文、出可审计 patch**；可选 `body_md_revised` 不得绕过 conflicts。  
5. **失败不重蒸整篇**；无独立润色 worker。  
6. **禁止静默覆盖**。

## 仍开问题（产品）

1. 唯一强命中且无对撞时，是否允许跳过模型走廉价规则合并？  
2. `both_with_scope` 落双 facts（建议）还是 body 并列？`pre_conflicts` 本轮是否做？  
3. `body_md_revised` 默认采用策略；超长 body 的具体阈值。
