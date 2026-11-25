---
name: k8s-workload-health
description: >-
  Health of one Kubernetes workload: rollout state and a rolling restart. Use when a
  Deployment is not progressing or pods keep restarting.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment]
  tags: [rollout]
---
`rollout-status` first. Only run `restart` when the rollout is stuck and the user agrees.
