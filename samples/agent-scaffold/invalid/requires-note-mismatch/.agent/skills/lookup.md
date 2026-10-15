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
  - id: "ask-note"
    purpose: "Ask cannot carry a note."
    from: "user"
    ifMissing: "ask"
    note: "Not allowed."
  - id: "refuse-note"
    purpose: "Refuse cannot carry a note."
    from: "user"
    ifMissing: "refuse"
    note: "Not allowed."
---

# Invalid requires fixture
