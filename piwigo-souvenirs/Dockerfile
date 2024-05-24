FROM golang:1.21-alpine AS builder

WORKDIR $GOPATH/src/napnap75/piwigo-souvenirs/
COPY piwigo-souvenirs.go .
RUN apk add --no-cache git gcc musl-dev \
	&& go mod init github.com/napnap75/rpi-docker-files/piwigo-souvenirs \
	&& go get -d -v \
	&& go build -ldflags="-w -s" -o /go/bin/piwigo-souvenirs

FROM alpine:latest
COPY --from=builder /go/bin/piwigo-souvenirs /usr/bin/
VOLUME /tmp/messages
ENTRYPOINT ["/usr/bin/piwigo-souvenirs"]
