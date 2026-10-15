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
  - id: "bad-policy"
    purpose: "Policy enum is restricted."
    from: "user"
    ifMissing: "skip"
---

# Invalid requires fixture
