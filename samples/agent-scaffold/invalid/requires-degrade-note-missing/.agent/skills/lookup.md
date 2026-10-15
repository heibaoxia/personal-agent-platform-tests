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
  - id: "degrade-note"
    purpose: "Degrade requires a note."
    from: "user"
    ifMissing: "degrade"
---

# Invalid requires fixture
