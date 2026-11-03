#!/usr/bin/env bash
if [[ ${RW_DRY_RUN:-} == 1 ]]; then
  rw_output_set result '{"dry":true}'
else
  rw_output_set result '{"dry":false}'
fi
