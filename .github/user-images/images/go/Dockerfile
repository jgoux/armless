# Cross-compiled on the build platform, so that the arm64 image needs no emulation to build
FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS build
ARG TARGETARCH
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=$TARGETARCH go build -trimpath -ldflags='-s -w' -o /app .

FROM gcr.io/distroless/static
COPY --from=build /app /app
# host.internal is the one name that the host forwards to its 127.0.0.1 (armless's DNS answers it
# with a private address, and the connection is the host's to open)
# LOAD_MB is for the snapshot test's image alone (main.go), built with --build-arg LOAD_MB=64
ARG LOAD_MB=
ENV PORT=8080 \
    DATABASE_URL=postgres://postgres:postgres@host.internal:5432/postgres?sslmode=disable \
    LOAD_MB=$LOAD_MB
ENTRYPOINT ["/app"]
