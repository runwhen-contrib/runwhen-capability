#!/usr/bin/env bash
set -euo pipefail
kubectl rollout restart "deployment/${RW_RESOURCE_NAME}"
