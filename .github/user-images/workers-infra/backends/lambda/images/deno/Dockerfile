# Shared base image for the Deno catalog runtime — carries a thin shim over
# native Deno.serve as its entrypoint. A service image is built `FROM` this +
# `COPY` user code into /app; run bare it idles ready for a session. Build
# context is the repo root.
FROM denoland/deno:alpine-2.9.0 AS runtime

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
ENV SANDBOX_RUNTIME=deno APP_DIR=/app
WORKDIR /app
# Tenant code verifies TLS against this file rather than the webpki-roots
# compiled into the deno binary: that one is only as current as the deno
# release. A tenant that sets DENO_TLS_CA_STORE replaces the choice.
COPY --from=hostproxy /out/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENV DENO_TLS_CA_STORE=system
COPY backends/lambda/shims/serve-deno.ts /opt/workers/serve-deno.ts
# The host-restoring proxy the launcher puts on the serving port; the
# variable is what flips the launcher into proxy mode.
COPY --from=hostproxy /out/hostproxy /opt/workers/hostproxy
ENV WORKERS_HOSTPROXY=/opt/workers/hostproxy
ENTRYPOINT ["deno", "run", "-A", "/opt/workers/serve-deno.ts"]
