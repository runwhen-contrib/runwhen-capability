source "$RW_SDK/rw.sh"
mkdir -p out
printf 'registered in place\n' > out/report.txt
rw_artifact report out/report.txt
