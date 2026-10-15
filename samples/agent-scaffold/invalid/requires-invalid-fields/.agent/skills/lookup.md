---
id: "lookup"
purpose: "Exercise requires field validation."
triggers:
  - "test"
excludes:
  - "test"
inputs:
  - "question"
outputs:
  - "answer"
sideEffects: "none"
confirmation: "not-required"
implementation: "Fixture only."
requires:
  - id: "BAD_ID"
    purpose: ""
    from: true
    ifMissing: 7
    note: false
    extra: "not declared"
  - purpose: "Missing id, from, and ifMissing."
  - id: "bad-source"
    purpose: "Unknown source enum."
    from: "database"
    ifMissing: "ask"
  - id: "bad-context-category"
    purpose: "Context 类别必须是字符串。"
    from: "context"
    contextCategory: 1
    ifMissing: "ask"
  - id: "bad-integration-id"
    purpose: "Integration 标识必须是字符串。"
    from: "integration"
    integration: false
    ifMissing: "ask"
---

# Invalid fields
