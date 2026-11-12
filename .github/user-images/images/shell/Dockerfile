# alpine whose entrypoint is a shell script that does process-heavy work before it serves with busybox httpd on 8080
# (the benchmark of compute/README.md): the kernel's fork/exec/mmap/page faults and many cold binaries.
FROM alpine:3.24.3@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
COPY entrypoint.sh /entrypoint.sh
# alpine's busybox has no httpd applet: it is in busybox-extras
RUN apk add --no-cache busybox-extras \
 && chmod 755 /entrypoint.sh && mkdir /www && echo 'bench-shell ok' > /www/index.html
ENTRYPOINT ["/entrypoint.sh"]
