#!/usr/bin/env bash
for i in $(seq 1 "$RW_INPUT_COUNT"); do
  rw_output_append probe "{\"namespace\":\"$RW_RESOURCE_NAMESPACE\",\"name\":\"$RW_RESOURCE_NAME\",\"threshold\":$RW_SETTING_LATENCY_THRESHOLD_MS,\"note\":\"$RW_INPUT_NOTE\"}"
done
echo '{"type":"'"$RW_RESOURCE_TYPE"'","path":"'"$RW_RESOURCE_PATH"'"}' > "$RW_OUT/events.json"
