---
# source: 42a-skill-spec.md "## SKILL.md"
name: k8s-workload-health
description: >-
  Health checks for Kubernetes workloads (Deployments, StatefulSets, DaemonSets):
  rollout progress, restarts, probe failures and resource pressure. Use when a
  workload is degraded, not ready or crash-looping, or an alert names one.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment, kubernetes/statefulset, kubernetes/daemonset]
  tags: [health, availability]
---
## When to use
## What each task tells you
## How to read the result
## What to try next
