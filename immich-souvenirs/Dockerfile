FROM golang:1.23-alpine AS builder

WORKDIR $GOPATH/src/napnap75/immich-souvenirs/
COPY immich-souvenirs.go .
RUN apk add --no-cache git gcc musl-dev \
	&& go mod init github.com/napnap75/multiarch-docker-files/immich-souvenirs \
	&& go get -d -v \
	&& go build -ldflags="-w -s" -o /go/bin/immich-souvenirs

FROM alpine:latest
RUN apk add --no-cache tzdata
ENV TIME-TO-RUN="7:00"
COPY --from=builder /go/bin/immich-souvenirs /usr/bin/
VOLUME /tmp/messages
ENTRYPOINT ["/usr/bin/immich-souvenirs"]
