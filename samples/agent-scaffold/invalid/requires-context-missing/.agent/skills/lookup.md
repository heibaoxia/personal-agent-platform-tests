---
id: "lookup"
purpose: "Fixture Skill for requires validation."
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
  - id: "scope"
    purpose: "Need an undeclared Context category."
    from: "context"
    contextCategory: "missing-category"
    ifMissing: "ask"
---

# Invalid requires fixture
