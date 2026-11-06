---
name: one
description: Payments Deployments restart nightly
appliesTo:
  types: [kubernetes/deployment]
tags: [restarts]
---
Payments Deployments are restarted by a nightly job at 02:00 UTC; a restart then is expected.
