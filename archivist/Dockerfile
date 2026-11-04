# archivist: multi-stage build. The builder compiles a static binary;
# the runtime is scratch with only that binary and the files it needs.

# --- build stage ------------------------------------------------------------
FROM golang:1.27-alpine AS build
WORKDIR /src

# ca-certificates and tzdata are copied into the runtime image below.
RUN apk add --no-cache ca-certificates tzdata git

# TARGETARCH is set by buildx for multi-platform builds; it is unset for a
# plain docker build, where go builds for the host architecture.
ARG TARGETARCH

# go.sum is not committed yet: go mod tidy resolves and pins the exact
# versions at build time. Once go.sum is committed, replace the tidy line
# with `go mod download` for fully reproducible builds.
COPY go.mod main.go ./
COPY internal/ ./internal/
RUN go mod tidy
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -trimpath -ldflags="-s -w" -o /archivist .

# Minimal runtime tree assembled next to the binary:
# - CA bundle: the S3 endpoint is HTTPS by default
# - timezone database: the cron and date rendering honor TZ
# - /app/pb_data (SQLite database) and /tmp, the only writable directories,
#   owned by the unprivileged runtime user 65532
RUN mkdir -p /out/etc/ssl/certs /out/usr/share /out/app/pb_data /out/tmp \
    && cp /etc/ssl/certs/ca-certificates.crt /out/etc/ssl/certs/ \
    && cp -r /usr/share/zoneinfo /out/usr/share/zoneinfo \
    && chown -R 65532:65532 /out/app /out/tmp

# --- runtime stage -----------------------------------------------------------
FROM scratch
COPY --from=build /out/ /
COPY --from=build /archivist /app/archivist

WORKDIR /app
ENV TMPDIR=/tmp
USER 65532:65532
VOLUME /app/pb_data
EXPOSE 8090
ENTRYPOINT ["/app/archivist"]
CMD ["serve"]
