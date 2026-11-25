#!/usr/bin/env bash
if [[ ${RW_DRY_RUN:-} == 1 ]]; then
  kubectl -n "$RW_RESOURCE_NAMESPACE" rollout restart "deployment/$RW_RESOURCE_NAME" --dry-run=server
  rw_output_set result '{"dry":true}'
else
  kubectl -n "$RW_RESOURCE_NAMESPACE" rollout restart "deployment/$RW_RESOURCE_NAME"
  rw_output_set result '{"dry":false}'
fi
