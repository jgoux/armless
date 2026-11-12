#!/bin/sh
# Process-heavy work, then busybox httpd on 8080
set -eu
work=/tmp/work
mkdir -p "$work"
t0=$(date +%s)

# 300 short fork/execs of busybox tools (60 pipelines of 5)
i=0
while [ "$i" -lt 60 ]; do
  echo "$i" | md5sum | cut -c1-8 | tr a-f A-F | wc -c > /dev/null
  i=$((i + 1))
done

t1=$(date +%s)

# a tar and gzip of /usr
tar -cf - /usr 2> /dev/null | gzip -1 | wc -c > "$work/usr.tgz.size" || true

t2=$(date +%s)

# sort and awk over a generated file
seq 1 100000 | awk '{ print ($1 * 7919) % 100003, $1 }' > "$work/nums"
sort -n "$work/nums" | awk '{ s += $1 } END { print s }' > "$work/sum"
t3=$(date +%s)
echo "shell app: forks $((t1 - t0))s, tar $((t2 - t1))s, sort and awk $((t3 - t2))s, usr.tgz $(cat "$work/usr.tgz.size") bytes, sum $(cat "$work/sum"), serving"
rm -rf "$work"

exec httpd -f -p 8080 -h /www
