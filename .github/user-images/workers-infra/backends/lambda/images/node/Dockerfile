# Shared base image for the Node.js catalog runtime — carries the fetch shim
# as its entrypoint. A worker image is built `FROM` this with user code baked
# under /app (the synthesized Dockerfile in internal/backend/microvm/artifact.go);
# run bare it idles ready. Build context is the repo root; published by
# `x publish-runtime-images` at the tag internal/backend/lambda/lambda.go pins.
FROM node:24-alpine AS runtime

FROM --platform=$BUILDPLATFORM golang:1.26 AS hostproxy
WORKDIR /src
COPY go.mod go.sum ./
COPY backends/lambda/hostproxy backends/lambda/hostproxy
COPY deploy/cabundle deploy/cabundle
# Cross-built for the image's platform: arm64 for Lambda MicroVM guests, amd64
# for a computate cell's (supabase-workers-computate-runtime-*).
ARG TARGETARCH
RUN CGO_ENABLED=0 GOOS=linux GOARCH=$TARGETARCH go build -o /out/hostproxy ./backends/lambda/hostproxy
# The runtime's own trust store minus the roots deploy/cabundle distrusts.
COPY --from=runtime /etc/ssl/certs/ca-certificates.crt /out/ca-certificates.crt
RUN go run ./deploy/cabundle -in /out/ca-certificates.crt

FROM runtime
ENV SANDBOX_RUNTIME=node APP_DIR=/app
WORKDIR /app
# Tenant code verifies TLS against this file rather than the store compiled
# into the node binary: that one is only as current as the node release, and
# OpenSSL's default store on alpine is /etc/ssl/cert.pem -> this bundle. A
# tenant that sets NODE_OPTIONS replaces the flag along with it.
COPY --from=hostproxy /out/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENV NODE_OPTIONS=--use-openssl-ca

# The package managers a boot might need, resolved here instead of there. An
# app whose dependencies are not in its bundle has the shim install them
# (backends/lambda/shims/serve.mjs), and a manager corepack fetches at that
# point spends the guest's liveness budget on the installer before the install
# has started. Pinned so this file is the one place a version changes.
ARG NPM_VERSION=11.19.0
ARG PNPM_VERSION=12.4.2
ARG YARN_VERSION=4.18.0
# Yarn 1 and Yarn 2+ share a name and are not the same package manager: Berry
# reads a v1 lockfile only by rewriting it, which an immutable install then
# refuses. Both are here so a project's own yarn.lock picks the one that
# understands it — the shim reads the lockfile's format to choose (serve.mjs,
# yarnFlavour), since the file says which yarn wrote it while `packageManager`
# only says what the author happened to have installed.
ARG YARN_CLASSIC_VERSION=1.22.22
# Under /opt rather than $HOME: the cache is then a layer every instance
# already has, not a write each boot repeats.
ENV COREPACK_HOME=/opt/corepack
# The builds the shim may run, newest first within a name. A project's
# `packageManager` version is never one of them: fetching it would put a
# download for the installer inside the boot it is installing in.
ENV WORKERS_PACKAGE_MANAGERS=npm@${NPM_VERSION},pnpm@${PNPM_VERSION},yarn@${YARN_VERSION},yarn@${YARN_CLASSIC_VERSION}
RUN corepack install --global npm@${NPM_VERSION} pnpm@${PNPM_VERSION} yarn@${YARN_VERSION} \
    # `install --global` holds one version per manager, and plainly running the
    # odd one out is what puts it in the cache beside the others.
    && corepack yarn@${YARN_CLASSIC_VERSION} --version > /dev/null \
    # pnpm's package is a launcher that fetches a platform binary on first run,
    # which the corepack cache does not carry. This is that first run, so the
    # binary lands in the image rather than in the first boot that needs pnpm.
    && corepack pnpm@${PNPM_VERSION} --version > /dev/null

COPY backends/lambda/shims/serve.mjs /opt/workers/serve.mjs
# The host-restoring proxy the launcher puts on the serving port; the
# variable is what flips the launcher into proxy mode.
COPY --from=hostproxy /out/hostproxy /opt/workers/hostproxy
ENV WORKERS_HOSTPROXY=/opt/workers/hostproxy
ENTRYPOINT ["node", "/opt/workers/serve.mjs"]
