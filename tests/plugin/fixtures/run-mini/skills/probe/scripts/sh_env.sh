#!/usr/bin/env bash
rw_output_set env "{\"relay\":\"${RELAY_URL-unset}\",\"pool\":\"${POOL_ID-unset}\",\"token\":\"${EXECUTOR_TOKEN_FILE-unset}\",\"contract\":\"$RW_TASK_CONTRACT\",\"home\":\"$HOME\"}"
