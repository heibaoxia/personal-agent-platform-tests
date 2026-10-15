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
  - id: "same"
    purpose: "First declaration."
    from: "user"
    ifMissing: "ask"
  - id: "same"
    purpose: "Duplicate declaration."
    from: "user"
    ifMissing: "ask"
---

# Invalid requires fixture
