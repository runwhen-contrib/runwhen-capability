#!/usr/bin/env bash
host_only=0
for name in RELAY_URL POOL_ID EXECUTOR_TOKEN_FILE RW_EXECUTOR_TOKEN_FILE; do
  if [ -n "${!name+x}" ]; then host_only=$((host_only + 1)); fi
done
if [ -z "${RW_SYNC_CREDENTIAL-}" ]; then
  rw_output_set sync "{\"path\":null,\"hostOnly\":$host_only}"
  exit 0
fi
read_field() { python3 -c 'import json,os,sys; print(json.load(open(os.environ["RW_SYNC_CREDENTIAL"]))[sys.argv[1]])' "$1"; }
api=$(read_field apiBaseUrl)
workspace=$(read_field workspace)
token=$(read_field token)
mode=$(python3 -c 'import os,stat; print(oct(stat.S_IMODE(os.stat(os.environ["RW_SYNC_CREDENTIAL"]).st_mode)))')
rw_output_set sync "{\"path\":\"$RW_SYNC_CREDENTIAL\",\"mode\":\"$mode\",\"apiBaseUrl\":\"$api\",\"workspace\":\"$workspace\",\"token\":\"$token\",\"hostOnly\":$host_only}"
