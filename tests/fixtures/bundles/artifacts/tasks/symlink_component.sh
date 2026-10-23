source "$RW_SDK/rw.sh"
mkdir real
printf 'behind a link\n' > real/file
ln -s real linked
rw_artifact dump linked/file
