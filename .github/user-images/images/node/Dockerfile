# A plain node server started the ordinary way, no launcher (the benchmark of compute/README.md; compute/runtimes has the
# catalog images with theirs).
FROM node:22.23.4-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914
WORKDIR /app
COPY server.js ./
ENV PORT=8080
CMD ["node", "server.js"]
