source "$RW_SDK/rw.sh"
# seq -f '%.0f': BSD seq prints 1000000 and up as 1e+06
seq -f '%.0f' 1 2000000 | rw_artifact dump -
