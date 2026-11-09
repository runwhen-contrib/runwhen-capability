#!/usr/bin/env bash
if bash -c 'false; true'; then rw_output_set nested '{"ok":true}'; else rw_output_set nested '{"ok":false}'; fi
