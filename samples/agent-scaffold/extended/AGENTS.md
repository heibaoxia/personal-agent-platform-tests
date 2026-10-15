---
agentId: "personal-agent-extended"
contractVersion: "1"
purpose: "扩展 Agent 工作空间样例，在最小结构之上按需增加 Context、Behavior、第二个 Skill、Workflow 和 Integration。"
skills:
  - id: "quiet-check"
    path: ".agent/skills/quiet-check.md"
  - id: "external-lookup"
    path: ".agent/skills/external-lookup.md"
workflows:
  - id: "review"
    path: ".agent/workflows/review.md"
integrations:
  - id: "search"
    path: ".agent/integrations/search.md"
---

# Personal Agent（extended）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读：
正文说明与 frontmatter 冲突时，以 frontmatter 为准。`skills`、`workflows`、`integrations`
三个索引与磁盘文件双向对应 —— 索引里声明的文件必须存在，约定目录里的文件必须被登记。

## 结构

```text
extended/
├── AGENTS.md
└── .agent/
    ├── context.yaml
    ├── behaviors.md
    ├── skills/quiet-check.md
    ├── skills/external-lookup.md
    ├── workflows/review.md
    └── integrations/search.md
```

相比最小样例，本样例增加了五类对象，每一类都有实际内容：

- `.agent/context.yaml` —— 默认信息类别、来源、状态和写入策略。
- `.agent/behaviors.md` —— 行为规则。
- `.agent/skills/` —— 第二个 Skill，声明需要外部依赖的能力。
- `.agent/workflows/review.md` —— 多步流程，引用已声明的 Skill。
- `.agent/integrations/search.md` —— 外部依赖的静态声明与降级策略。

## 各类对象的边界

- Context 声明默认信息从哪里来、处于哪种状态、按什么策略写入；它不是行为规则，也不是持久化状态记录。
- Behavior 承载行为规则；它不记录运行时状态，也不是 Skill 元数据。
- Skill 承载能力元数据；它不复制工作空间级索引，也不定义多步编排。
- Workflow 承载多步编排，只能引用已声明的 Skill。
- Integration 只声明外部依赖和降级策略，不包含凭据、端点或启动命令。

## 扩展边界

- 新增对象时必须同时写入索引，且 `id` 为小写 slug、与文件名一致。
- `external-lookup` 声明 `sideEffects: "calls-external"`，因此它必须 `confirmation: "required"`，
  且工作空间必须至少声明一个 Integration —— 本样例声明了 `search`。
- 不为未来需求预留空目录、空文件或占位配置。

## 校验边界

静态校验只检查声明：字段、类型、枚举、slug、路径、引用和副作用策略一致性。
它不连接外部服务、不读取凭据、不执行 Skill、不执行 Behavior、不读写 Context，
也不代表宿主运行时可用或降级策略已经生效。
