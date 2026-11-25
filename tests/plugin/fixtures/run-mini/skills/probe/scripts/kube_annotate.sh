#!/usr/bin/env bash
kubectl -n "$RW_RESOURCE_NAMESPACE" annotate "deployment/$RW_RESOURCE_NAME" "restarted-at=$RW_NOW" --overwrite
rw_output_set result "{\"stamped\":\"$RW_NOW\"}"
