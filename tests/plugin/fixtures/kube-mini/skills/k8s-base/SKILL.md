---
name: k8s-base
description: >-
  Discovers Kubernetes resources and runs read-only kubectl commands against a cluster.
  Use to inventory a cluster or to look at a resource the other skills do not cover.
runwhen:
  role: [discovery, cli]
  appliesTo:
    types: [kubernetes/cluster]
---
Run `discover` to sync the cluster's resources to the platform. Run `kubectl` for a
read-only look at anything else.
