source "$RW_SDK/rw.sh"
blob=$(head -c 300000 /dev/zero | tr '\0' x)
rw_set blob "\"$blob\""
exit 1
