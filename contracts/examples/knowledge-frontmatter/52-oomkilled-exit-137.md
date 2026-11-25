---
# source: 52-rules-and-knowledge.md "## Knowledge (D61)"
name: oomkilled-exit-137
description: Exit code 137 means the container was OOMKilled
appliesTo: { types: [kubernetes/pod, kubernetes/deployment] }
tags: [memory, exit-codes]
---
A container that exits with 137 was killed by the kernel OOM killer, not by the application. Compare `resources.limits.memory` with recent usage before reading application logs.
