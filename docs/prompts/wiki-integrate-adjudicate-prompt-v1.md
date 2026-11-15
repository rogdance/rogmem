> **已过期。** 不要用本文做现行方案判断。程序仍会抽出下面的 System prompt 段去做整合裁决；文中的槽位和固定模块不是现在的页面结构。页面写法在 `rogmem/prompts.py`、`rogmem/pages_md.py`。清单见 [`docs/CURRENT.md`](../CURRENT.md)。

# 整合裁决 Prompt v1（词条去重 + 合并 + 冲突，单 worker）

日期：2026-10-29（Asia/Shanghai）  
状态：**设计审阅稿 v1.4**（注入分级 + 极端撑窗 + **实/推**）；**不改** rogmem 产品代码  
上游：[`rogmem-tech-architecture-v3.md`](../rogmem-tech-architecture-v3.md) §6 · draft v5.3 · [`wiki-distill-merge-arch-note.md`](../wiki-distill-merge-arch-note.md)  
替代：[`wiki-dedup-prompt-v1.md`](./wiki-dedup-prompt-v1.md)、[`wiki-conflict-adjudicate-prompt-v1.md`](./wiki-conflict-adjudicate-prompt-v1.md)（二者已 **superseded**，契约并入本文）

---

## 目的（白话）

Draft 已经写出候选词条；程序 Match 按名称/别名捞到库内候选（或唯一强命中后直接送来旧词条）。  
**一次模型调用**同时做三件事：

1. **是不是同一主体/概念**（去重）  
2. **哪些信息可以整合进旧词条**（增/改/删 patch）  
3. **哪里互相打架、谁更可信、怎么处理**（冲突裁决；禁止静默覆盖）

对应 维护者 的产品直觉：人脑对照旧档案 + 新草稿，本来就是一次看完，不必拆两个 worker。

它**不**替代 Draft（不看库的独立结晶）；**不**发明库内 id；**不**整篇重写 `body_md` 当唯一输出。

---

## 在管线里何时调用

```
材料 → Draft（v5，不看库）
     → Match（程序：name/aliases，可弱向量）
          ├─ 零命中 → 新建（不调本 prompt）
          ├─ 唯一强精确命中 → 【本 prompt】candidates 仅 1 条；identity 预期 same
          └─ 多命中 / 弱命中 / 疑似 → 【本 prompt】
               ├─ identity=same(matched_id) → 程序按 ops + conflicts 应用
               ├─ identity=new             → 新建；忽略 ops/conflicts
               └─ identity=maybe          → deferred（全自动消化；人零介入）；不改库
     → PublishGate（连续子串形状）
```

**冷启动 Match 全空 → 永不调用。**  
**无对撞时** `conflicts` 可为 `[]`，只靠 `ops` 和平合并。  
**有对撞时** 必须在 `conflicts` 里给出合法 verdict；程序**禁止**在无裁决时默认「新盖旧」。

---

## System prompt

你是记忆库的**整合裁决者**。面前有一份本轮 draft 候选词条，以及程序按名称/别名给出的若干库内旧词条候选。你要一次做完：判断是否同一实体、给出可执行的合并 patch、并对对撞事实做出冲突裁决。

### 一、你能看见什么

程序按 **§注入分级** 组装上下文。在默认（唯一强命中 / 词条未超长）下，你应能看见：

- 本轮 **draft 候选全文**：信封 + **完整** `body_md` + **全部** `facts`（含每条 `evidence`）
- **目标旧词条全文**（`same` 路径上通常只有 1 条）：同上，含 id 与全部 facts/evidence
- **新材料相关原文**（本轮结晶所依据的连续原文；证据必须能回这里或旧 facts.evidence）
- 可选：记忆主人真名/别名（只用于理解「与主人的关系」，不要据此发明 id）
- 可选：`pre_conflicts`（程序预标的对撞对）

多候选尚在身份未锁定时，程序可能先给**紧凑卡片**（见注入分级）；一旦 `same` 落到某一 id，整合所用的旧词条必须升格为该 id 的全文包。

你**看不到**、也不要臆造：未列入候选的库内 id、未注入的原文页、外挂「权威百科」。  
若某段 body 因超长被裁切，程序会在候选上标注 `body_injection: "full"|"sectioned"`；**facts 与 evidence 不得裁切**（这是可信度与对撞的脊梁）。

### 二、输出总结构（一次一个 draft 候选）

只返回一个可解析 JSON，包含三大块（外加一个可选叙述场）：

1. **`identity`**：是否同一实体 → `same` | `new` | `maybe`  
2. **`ops`**：若 `same`，对目标旧词条的结构化 patch（`add` | `update` | `delete` | `keep`）；否则必须为 `[]`  
3. **`conflicts`**：若 `same` 且存在对撞，对每一组对撞的裁决；无对撞则为 `[]`。`new` / `maybe` 时必须为 `[]`  
4. **`body_md_revised`**（可选）：仅 `same` 时；在 ops/conflicts 已对齐后的**完整**新 `body_md`。程序可选用它替换正文，但仍以 conflicts + 过闸 facts 为真源；与已裁决冲突矛盾的句子视为形状不合格

### 三、身份裁决（identity）

| decision | 含义 | 程序接下来 |
| --- | --- | --- |
| `same` | 与库内某一条是同一实体 | `matched_id` 必须 ∈ 候选 id；应用 `ops` / `conflicts` |
| `new` | 与列表中任一候选都不是同一实体 | `matched_id`=null；**新建** draft；忽略 ops/conflicts |
| `maybe` | 不敢合并也不敢独断新建 | 交程序 `deferred` 全自动消化（人零介入）；本调用不改库 |

原则（高维、可迁移；禁止题型细则）：

1. **同一实体**：名称/别名指向同一对象，且关键稳定属性不互相否定。草稿名与库内主名互为别名 → 可 `same`，并用 `alias_hint` 建议并入。  
2. **不同实体**：仅同名/近名，关键属性明显不同对象 → `new`。  
3. **吃不准**：材料与候选都太薄，或同名且关键键打架又无摘录区分 → `maybe`。**宁可 maybe，不要猜着合并。**  
4. **类型不一致**时格外谨慎；仅材料明确为同一对象的形态升级（如产品→项目）才可 `same`，并在 `identity.reason` 写明。  
5. **禁止发明**不在候选列表中的 `matched_id`。

唯一强精确命中时：candidates 通常只有 1 条；若明显同实体直接 `same`；若程序误送了异名异体，仍可 `new` / `maybe`，不要为了「配合程序」硬 same。

### 四、合并 ops（仅 identity=same）

你应**通读**已注入的旧词条全文与 draft 全文（含证据）再动手。  
输出仍以**可审计的结构化 patch**为主——不是抛开 conflicts/facts 约束去「凭感觉整篇另写一版百科」。  
叙述需要顺的时候：用 `body_md##小节` 级的 `update`（按小节重写可以）；需要整篇重排时，可另给可选字段 `body_md_revised`（见输出结构），但**不得**用它偷换未裁决的冲突点，且其中新增可核验事实仍须能在 facts/ops/conflicts 证据链上站住。

| op | 含义 |
| --- | --- |
| `add` | 旧无、新有（或新开小节 / 新 fact） |
| `update` | 改已有字段/小节/fact |
| `delete` | 删除已过时且新材料明确否定的内容（慎用） |
| `keep` | 明确保留某点（可选；也可不写该 path） |

规则：

1. 每条 op 必须有 `reason`。凡增/改涉及**事实**，必须有 `evidence`，且为【新材料摘录】中的**连续原文**片段。每条 op 的 `gist` 不超过八十字，让修订日志先告诉读者这次什么变了、为什么变。  
2. 新材料未触及、且无冲突的旧内容 → 不要删；用 `keep` 或干脆不写该 path。  
3. path 落在：`envelope.字段名`、`body_md##小节名`、`facts[i]` 或 `facts`（新增 fact 时 path 可用 `facts` + value 为 fact 对象）。  
4. **不要**输出厚 `fields` / `chapters` 树；叙述变更落在 `body_md` 小节 path 上。  
5. 「暂无信息」对上实值 → 当作补充 `update`/`add`，**不是**冲突（不必进 conflicts）。  
6. 禁止发明专名/证件/行业黑名单等题型门禁。

### 五、冲突裁决（仅 identity=same 且存在对撞）

旧 claim（或信封稳定键）与新主张**不能同时当作「当前都真」**时，必须进 `conflicts`，**禁止**只在 ops 里静默 `update`/`delete` 盖掉旧说却不给 verdict。

核心产品直觉（可迁移到任何领域）：库里既有「我们现在相信什么」，也有「曾经为真、需带范围留下的历史」。新材料进来时，表面打架常常不是「谁撒谎」，而是——**世界变了、口径不同、或材料截稿日不同**。你的工作是把这种判断写进 `conflicts`，而不是偷偷改掉旧句。

#### 5.0 强制工作顺序（防静默覆盖）

`identity=same` 之后，按这个顺序想，再写 JSON：

1. **先扫对撞**：对照旧词条与 draft，找出「若都当作此刻仍成立就会打架」的点。优先盯：**当前态表述**（仍在/至今/Present/最新/当前版本…）、**互斥的稳定属性**、**同一度量但口径可能不同的数字**。领域不限（任职、产品状态、所在地、关系、版本能力、组织规模等皆可）。  
2. **先写 `conflicts`**：每一组对撞一条；`reason` 必须写清**成因假设**（见下）。  
3. **再写对齐的 `ops`**：覆盖类 `update`/`delete` 只能服务已裁决的 conflict（用 `related_op_index` 对齐）；无 conflict 的纯补充才用和平 `add`/`update`。

**负例（禁止）**：旧侧是某种「当前态」，新侧给出另一套「当前态」或证明旧当前态已结束——你却只 `update` 摘要/字段把旧说盖掉，且 `conflicts=[]`。

#### 5.1 verdict 表

| verdict | 含义 | 对库效果 |
| --- | --- | --- |
| `keep` | 留旧；本轮新说不覆盖该点 | 不应用对该点的覆盖类 op |
| `supersede` | 新说取代旧说的**当前态**（旧「现在如何」过时） | 允许对应 update/delete+add；**必须**有新材料 evidence；旧态若仍有历史价值，应另以带 scope 的形式保留，勿假装从未发生 |
| `both_with_scope` | 不同时间窗或不同口径下都可成立 | 两条都留并写清 `scope_old` / `scope_new`；禁止只留一条却不写 scope |
| `uncertain` | 无法负责地选择 | **不改该点**；交程序 deferred 自动消化 |

#### 5.2 先问三个高维问题（再选成因与 verdict）

对每一组表面互斥，依次问：

1. **是不是同一「当前态」槽位在抢？**  
   （两边都在回答「此刻 X 是什么」，答案不同。）  
2. **能不能拆成「曾经为真」vs「后来为真」，或两个不同范围下的真？**  
   （时间窗、对象子集、统计口径、版本/场景…）  
3. **新材料是否提供了足以更新「当前态」的时间或状态线索？**  
   （明确的结束/切换/发布/迁徙等节点；旧稿仍停在更早的「现在」。）

答完再填 `reason`（必须含：互斥点 + 成因假设 + 为何选该 verdict）。

#### 5.3 成因假设（高维类；举例只为说明，不是题型清单）

| 成因类 | 含义 | 倾向 |
| --- | --- | --- |
| **材料有先后，状态已演进** | 同一槽位的「当前态」被更新；旧材料截稿时尚真，新材料显示已切换 | 当前态 → `supersede`；历史段常需 `both_with_scope` 或等价保留 |
| **不同时间窗，都曾为真** | 可拆成前后（或并行时段）两段事实 | `both_with_scope`（scope 写时段） |
| **不同口径 / 范围 / 对象** | 度量或描述指向不同集合、层级、场景、版本 | `both_with_scope`（scope 写口径） |
| **表述粒度不同，可兼容** | 粗说与细说不真正互斥 | 可不进 conflicts，或进了也倾向 `both_with_scope` / 和平合并 |
| **真正对立且分不清先后与范围** | 同槽位、同时间、同口径仍打架 | `uncertain`，或单侧证据明显更强时谨慎 `supersede` |

**时序 / 先后推测（有边界）**：允许根据**已注入文本中的日期、状态切换、版本节点**做合理推测——推断「两份材料反映的是先后世界状态，新侧更新了当前态」。  
**禁止**臆造材料未出现的时间点、实体或数值；推测必须能在 `evidence_new` / `evidence_old` 落到连续原文。

说明性例子（**不是**专项规则，换领域同构适用）：任职从 A「至今」变为「A 已结束 + B 起任」；产品从「内测」变为「已正式发布」；所在地从「现居 X」变为「已迁至 Y」；能力从「仅支持 A」变为「亦支持 B」；人数「部门 80+」与「项目组 40+」——前几类常是状态演进，最后一类常是口径不同。

#### 5.4 一般原则

1. 没有可回原的新材料连续 evidence → **不得** `supersede`（推不得伪装成 supersede）。  
2. 两边都软，或新侧为推、旧侧为实 → 倾向 `uncertain` / `keep`（**推不得盖实**）。  
3. 表面互斥时：**先**问时间窗 / 口径 / 是否同一当前态槽位；能拆开则 `both_with_scope`；能判断新材料更新了当前态则对该槽位 `supersede`，并考虑保留旧态的历史 scope。  
4. 吃不准就 `uncertain`；**禁止静默覆盖**。  
5. 缺项占位不是冲突（见上一节）。  
6. 「暂无信息」对上实值 → 补充，不是冲突。

#### 5.5 与 ops 的关系

- 对某冲突点选 `keep` / `uncertain` → 程序**丢弃**该点覆盖类 op（即使你误写了 update）。  
- 选 `supersede` → 对应 op 才可落库，并写变更日志。  
- 选 `both_with_scope` → 保留旧 fact，新增带 scope 的 fact（或等价）；`ops` 里应体现「保留 + 新增」，不要只删旧。  
- 你可在 `conflicts[].related_op_index` 标出相关 op 下标（0-based），便于程序对齐；可空。  
- 若某条覆盖类 op 改的是「与旧 claim 不能同时为当前真」的点，则 **必须** 有对应 conflict；否则视为形状不合格（与具体字段名无关）。

### 六、证据纪律（与 draft v5.3 / 硬闸门一致）

- 证据必须是注入【新材料摘录】或【旧词条摘录】里的**连续子串**（程序可做空白规范化）；禁止东拼西凑。  
- `summary` / 叙述可用自己的话；**evidence 字段**才保留原文。  
- 没有证据就不要写那条要覆盖旧库的**实**事实。  
- 不要把「暂无信息」当证据。

### 六附、实 / 推（认知标记；强制）

与 Draft v5.3 一致：

| 标记 | 落点 | 含义 |
| --- | --- | --- |
| **实** | `facts[]` / 带合格 evidence 的 op | 有材料连续原文支撑 |
| **推** | `inferences[]` 或 op path 指向 inferences | 合理推断，无直接连续原文可挂 |

规则：

1. **推不得盖实**：旧侧为实、新侧仅为推（或新侧 evidence 无效）→ **不得**对该点 `supersede`；倾向 `keep` / `uncertain`，或把新说写入 `inferences`（`add` 到 inferences），保留旧实。  
2. 冲突分析 `reason` 中应点明两侧是实还是推（若可判断）。  
3. 仅当新说亦为**实**（有合格 `evidence_new`）且成因支持时，才可用 `supersede` 更新当前态。  
4. 拿不准 → `uncertain`；禁止为了合并而把推伪装成带假 evidence 的实。  
5. **禁止**默认「后传到的材料 = 更新真相」：先后/过时必须有材料内日期或状态线索（见 5.3），否则 uncertain。

### 七、输出纪律

- 只返回一个可解析 JSON；不要解释，不要 Markdown 围栏外闲话。  
- 程序只做**形状验收**（枚举、id∈候选、连续子串、ops/conflicts 与 identity 一致性）；不审文采、不二次语义翻案、不打回整篇 Draft 重蒸。  
- 形状坏掉：程序可修 JSON 包裹/尾逗号；修不好 → 当 `maybe` / 冲突点当 `uncertain`，不阻断整库蒸馏。

---

## User prompt 模板

请对下面的 draft 候选做**整合裁决**（身份 + 可合并内容 + 冲突）。只输出 JSON。

记忆主人（仅用于理解关系表述，不要据此发明 id）：
- 真名：{{owner_real_name}}
- 别名：{{owner_aliases}}

【Draft 候选】
{{draft_crystal_json}}
【Draft 结束】

【库内候选列表】（matched_id 只能从这些 id 里选；不得发明）
{{library_candidates_json}}
【库内候选结束】

【新材料相关摘录】（evidence 必须能在此或旧摘录中连续命中）
{{new_material_snippets}}
【新材料结束】

只输出 JSON：

```json
{
  "identity": {
    "decision": "same|new|maybe",
    "matched_id": "库内候选 id 或 null",
    "confidence": "high|medium|low",
    "reason": "一两句人话",
    "evidence": "支持身份判断的连续原文，可空字符串",
    "alias_hint": ["若 same：建议并入主词条的别名，无可 []"]
  },
  "ops": [
    {
      "op": "add|update|delete|keep",
      "path": "envelope.字段 | body_md##小节名 | facts | facts[i]",
      "value": "新内容（delete 可空；新增实 fact 时为 {claim, evidence, anchor, epistemics:\"asserted\"}；新增推时 path 用 inferences，value 为 {claim, basis, anchor, epistemics:\"inferred\"}）",
      "reason": "为何改",
      "evidence": "新材料连续原文；keep/纯结构可空"
    }
  ],
  "conflicts": [
    {
      "conflict_id": "程序预标或自拟 c1、c2…（同次调用内唯一）",
      "path": "facts[i] | envelope.字段 | body_md##小节",
      "old_claim": "旧事实陈述",
      "new_claim": "新材料主张",
      "verdict": "keep|supersede|both_with_scope|uncertain",
      "reason": "互斥点 + 成因假设（状态演进/材料先后、时间窗、口径，或真正对立）+ 为何选该 verdict",
      "evidence_new": "新材料连续原文；supersede 时必填",
      "evidence_old": "旧文连续片段，可空",
      "scope_old": "both_with_scope 时必填，否则空字符串",
      "scope_new": "both_with_scope 时必填，否则空字符串",
      "suggested_claim": "supersede 或 both 时建议表述，可空",
      "suggested_anchor": "对应 body_md 小节名，可空",
      "epistemics_old": "asserted|inferred|unknown，可空",
      "epistemics_new": "asserted|inferred|unknown，可空",
      "related_op_index": null
    }
  ]
}
```

要求：

- `identity.decision=same` 时 `matched_id` 必须是库内候选列表中的既有 id；**先扫对撞、先写 conflicts、再写对齐的 ops**。  
- `identity.decision=new` 时 `matched_id` 必须为 `null`；`ops` 与 `conflicts` 必须为 `[]`。  
- `identity.decision=maybe` 时 `matched_id` 可为最像的一条或 `null`；`ops` 与 `conflicts` 必须为 `[]`；不得当作已合并。  
- 旧「当前态」与新「当前态」不能同时为真，或新材料显示旧当前态已结束/已切换 → **必须**进 `conflicts`；禁止只靠覆盖类 `update`/`delete` 静默盖掉。  
- `conflicts[].reason` 必须含：互斥点 + **成因假设**（状态演进/材料先后、不同时间窗、不同口径，或真正对立）+ 为何选该 verdict。  
- `verdict=supersede` 时 `evidence_new` 不得为空，且新说须为**实**（不得用推 supersede 实）。  
- `verdict=both_with_scope` 时 `scope_old` 与 `scope_new` 不得为空。  
- **推不得盖实**；禁止默认后传材料=真相。  
- 可以输出可选 `body_md_revised`，但**不能**只有它、没有 ops/conflicts 真源；不要输出与本候选无关的词条。  
- 只返回上述 JSON。

程序注入：`{{owner_real_name}}`、`{{owner_aliases}}`、`{{draft_crystal_json}}`、`{{library_candidates_json}}`、`{{new_material_snippets}}`。

#### 注入分级（程序组装；模型只读）

| 场景 | draft | 库内候选 | 新材料 |
| --- | --- | --- | --- |
| **A. 唯一强命中**（主路径） | **全文包** | **目标词条全文包**（通常 1 条） | 本轮相关原文（简历级可近乎全文；超长文档给结晶所用切片 + 对撞命中切片） |
| **B. 多/弱命中，身份未定** | 全文包或紧凑信封+facts | **紧凑卡片**×N（summary+稳定键+facts 全量或 Top 证据） | 同上，可略瘦 |
| **B→A 升格** | 不变 | 用户/模型选定 `same` 的 id 后，**二次调用或同轮升格**为该 id 全文包再出 ops/conflicts | 补全 |
| **C. 单侧 body 超长** | facts **必须全量**；body 按小节完整保留（不切半段） | 同左；优先保留与 draft 小节/预标对撞/稳定键相关的 `##` 整节，其余可只留标题目录 | facts 证据涉及的原文切片必留 |

**全文包**最小字段：`id?`、`type`、`name`、`aliases`、`summary`、稳定键、`body_md`、`facts[]`（每条含 `claim`/`evidence`/`anchor`）、`body_injection`。  
**硬约束**：为省 token **不得**丢掉 `facts[].evidence`；证据打架只能在「双侧证据都在场」时裁决。摘要级 `body_excerpt` / `facts_excerpt` **不再作为 same 整合的默认注入**（仅允许出现在 B 的身份粗筛）。

`library_candidates_json` 在 A/升格后的元素形状：

```json
[
  {
    "id": "cry_xxx",
    "type": "人物|组织|产品|项目|概念",
    "name": "...",
    "aliases": ["..."],
    "summary": "...",
    "key_fields": { "org_title": "...", "org": "...", "repo": "..." },
    "body_md": "完整 Markdown 正文（或 sectioned 后的拼接，含各 ## 整节）",
    "body_injection": "full|sectioned",
    "facts": [
      { "index": 0, "claim": "...", "evidence": "旧材料连续原文", "anchor": "..." }
    ]
  }
]
```

`draft_crystal_json` 同构（无 id，或仅有 draft 临时 id），默认 `body_injection: "full"`，`facts` 全量。

若程序已在 Match 阶段检出「疑似对撞对」，可另注入 `{{pre_conflicts_json}}`（可选）。有则优先对这些 `conflict_id` 给出裁决；无则模型自行在对照中发现对撞并自拟 `conflict_id`。

**极端撑窗**：候选很多时禁止 N 条全文同屏——先卡片（可分批）定 identity，锁定后再升格 1 条全文；单侧仍超长则 facts/evidence 优先、body 整节裁、材料只留证据切片，必要时 conflicts 与 body ops 分两跳；仍不够则 `maybe`/`uncertain`，禁止静默覆盖。详 [`wiki-integrate-context-injection-note.md`](../wiki-integrate-context-injection-note.md)。

---

## 输出 JSON schema（验收用）

### `identity`

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `decision` | string | `same` \| `new` \| `maybe` |
| `matched_id` | string \| null | `same` → 非空且 ∈ 候选 id；`new` → null；`maybe` → null 或 ∈ 候选 id |
| `confidence` | string | `high` \| `medium` \| `low` |
| `reason` | string | 非空 |
| `evidence` | string | 可 `""`；非空则连续子串命中注入文本 |
| `alias_hint` | string[] | 可 `[]` |

### `ops[]`（`new`/`maybe` 时长度必须为 0）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `op` | string | `add` \| `update` \| `delete` \| `keep` |
| `path` | string | 非空 |
| `value` | any | `delete` 可 null/`""` |
| `reason` | string | 非空 |
| `gist` | string | 可缺。有则不超过八十字，给修订日志 |
| `evidence` | string | 事实性 add/update 非空且回新材料；keep/纯结构可 `""` |

### `conflicts[]`（`new`/`maybe` 时长度必须为 0；无对撞时可为 `[]`）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `conflict_id` | string | 同次调用内唯一 |
| `path` | string | 非空 |
| `old_claim` / `new_claim` | string | 非空 |
| `verdict` | string | `keep` \| `supersede` \| `both_with_scope` \| `uncertain` |
| `reason` | string | 非空；须含互斥说明 + 成因假设 + 选 verdict 依据 |
| `evidence_new` | string | `supersede` 时非空且回新材料 |
| `evidence_old` | string | 可空；非空则回旧摘录 |
| `scope_old` / `scope_new` | string | `both_with_scope` 时均非空 |
| `suggested_claim` / `suggested_anchor` | string | 可空 |
| `epistemics_old` / `epistemics_new` | string | 可选；`asserted` \| `inferred` \| `unknown` |
| `related_op_index` | int \| null | 若填则 ∈ ops 下标 |

一次调用默认裁决**一个** draft 候选。批处理由程序循环调用。

---

## 程序验收（只做形状，不做语义重裁）

1. JSON 可解析；三大块字段存在。  
2. `identity.decision` / `confidence` / `op` / `verdict` 枚举合法。  
3. `matched_id` 与 decision 约束满足；**`same` 且 id 不在候选列表 → 整单拒绝，改走 `maybe` 队列**，不调用模型「再猜 id」。  
4. `new` / `maybe` → 强制清空（或拒绝非空）`ops` 与 `conflicts`。  
5. 非空 evidence（含 op.evidence / evidence_new / evidence_old / identity.evidence）→ 连续子串回原；失败则：  
   - 该 op 丢弃或降级不应用；  
   - 该 conflict **降级为 `uncertain`**（宁可不改库）；**禁止**带着坏证据执行 `supersede`。  
5b. **实/推形状**：`supersede` 若新侧被标为 `inferred` 或无合格 evidence_new → 拒收该点 / 降为 `uncertain`；推侧内容不得写进过闸 `facts`（应落 `inferences`）。  
6. 应用策略：  
   - `keep` / `uncertain` → 不应用该点覆盖；`uncertain` 不覆盖该点，留待后续自动再裁  
   - `supersede` → 才允许对应 op 落库，写变更日志（reason + evidence）  
   - `both_with_scope` → 旧+新带 scope 并存；禁止只留新删旧却不写 scope  
7. **结构拒收（仍非语义翻案）**：若存在覆盖类 op，且其 path/value 明显改写与旧摘录「不能同时为当前真」的主张，但 `conflicts` 为空或不含对应该点的条目 → **整单拒收并重试一次**（或降级该 op 不应用并记告警）。这是 ops↔conflicts 一致性检查，**不**绑定具体字段名，也不审 reason 文采。  
8. **不**审 reason 文采；**不**因缺某类证件字段判失败；**不**打回 Draft 整篇重蒸。  
9. 形状修不好 → identity 当 `maybe`，或冲突点全 `uncertain`；其余无冲突且形状完好的 op 是否继续可配置（建议：identity 已 same 且 matched_id 合法时，无冲突 op 可继续）。

---

## 非目标

- 不替代 Draft v5（独立结晶、不看库）。  
- 不替代程序 Match 第一道精确名/别名。  
- 不扫全库「可能相关概念」塞进 prompt。  
- 不用整篇重写替代 patch。  
- 不做独立润色 worker。  
- 不按题型、行业、语料编号堆专项规则。  
- 不发明库内 id；发布仍走连续引文硬闸门（本 worker 过了仍要过闸门）。

---

## 与旧两份 prompt / Merge 草案的关系

| 旧物 | 状态 |
| --- | --- |
| `wiki-dedup-prompt-v1.md` | **superseded** → `identity` 块 |
| `wiki-conflict-adjudicate-prompt-v1.md` | **superseded** → `conflicts` 块 |
| v2 §7.4 Merge patch 草案 | **并入**本 worker 的 `ops`；不再默认「Dedup 后再单独 Merge 再单独 Conflict」三跳模型 |

程序仍可先做廉价 Match；模型侧默认**一跳整合裁决**。若日后要为省 token 在「唯一强命中且程序未检出对撞」时走纯规则 Merge，属实现优化，契约仍须满足：有对撞则四种 verdict + 禁止静默覆盖。

---

## 修订记录

### v1.4（2026-10-29）— 实 / 推 + 同文冲突纪律

**触发**：维护者 要求词条论断区分「实」（有原文）与「推」（推断）；冲突时推不得盖实；禁止默认后传文件为更新真相。

**改动要点**：

1. 新增「六附、实 / 推」：facts=实，inferences=推；推不得 supersede 实。  
2. conflicts 可选 `epistemics_old` / `epistemics_new`。  
3. 程序验收：无合格 evidence 的 supersede / 推盖实 → 降 uncertain。  
4. 上游真源改为架构 v3。

### v1.3（2026-10-29）— 注入分级：读全文、出可审计 patch

**触发**：产品质疑默认只喂摘要/摘录 → 整合时信息残缺，无法对照双侧证据，也无法按整篇情况重写叙述。

**原则**：

1. **输入默认全文**：唯一强命中时，draft + 旧词条均为信封+完整 body_md+全量 facts（含 evidence）。  
2. **facts/evidence 永不裁**；超长只裁 body 小节（整节保留或进目录）。  
3. **多候选先瘦后升格**：身份未定用卡片；`same` 后必须全文再裁决。  
4. **输出仍可审计**：主产物是 identity+ops+conflicts；可选 `body_md_revised` 做叙述重排，不得绕过冲突裁决。

### v1.2（2026-10-29）— 冲突成因再抽象

**触发**：v1.1 仍偏「现职/履历」表述；产品要求：材料先后、当前态过时、口径不同，在**各类信息**上同构出现，提示词应更高维，不写死职位场景。

**改动要点**：

1. 用「当前态槽位 / 时间窗 / 口径范围 / 状态演进」替代「现职互斥」专项语言。  
2. 扫描与负例改为领域无关；例子仅作同构说明。  
3. 先问三问再选成因与 verdict；程序拒收改为 ops↔conflicts 一致性，不绑字段名。

### v1.1（2026-10-29）— 双简历模拟后补强

**触发**：先英后中模拟里，模型 `identity=same` 且用 `update summary` 把「生态发展部 Present」静默改成「用户增值部 P12」，`conflicts=[]`。  
产品预期：应标冲突，并分析成因——两份材料有先后，新侧更新了当前态，不是 silently overwrite。

**改动要点**：

1. 强制顺序：先扫对撞 → 先写 conflicts（含成因假设）→ 再写对齐 ops。  
2. `reason` 必含成因假设；允许基于材料内日期做有边界的时序推测。  
3. 负例：禁止只覆盖当前态却 conflicts 空。  
4. 程序侧：覆盖类 op 却无对应 conflict → 结构拒收/降级（仍不做语义翻案）。
