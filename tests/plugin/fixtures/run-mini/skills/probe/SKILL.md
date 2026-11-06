---
name: probe
description: >-
  Tasks that exercise every way a task can finish: ok, partial, failed, skipped. Use only in
  tests of the SDK.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment]
---
Test skill.
