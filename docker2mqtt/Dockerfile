FROM golang:alpine AS builder

WORKDIR $GOPATH/src/napnap75/docker2mqtt/

COPY docker2mqtt.go .

RUN apk add --no-cache git gcc musl-dev \
	&& go mod init github.com/napnap75/multiarch-docker-images/docker2mqtt \
	&& go get -d -v \
	&& go build -ldflags="-w -s" -o /go/bin/docker2mqtt

FROM alpine:latest

COPY --from=builder /go/bin/docker2mqtt /usr/bin/
ENTRYPOINT ["/usr/bin/docker2mqtt"]
