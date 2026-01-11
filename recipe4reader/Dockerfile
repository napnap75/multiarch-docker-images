FROM golang:1.24-alpine AS builder

WORKDIR $GOPATH/src/napnap75/recipe4reader/
COPY recipe4reader.go .
RUN apk add --no-cache git gcc musl-dev \
	&& go mod init github.com/napnap75/multiarch-docker-files/recipe4reader \
	&& go get -d -v \
	&& go build -ldflags="-w -s" -o /go/bin/recipe4reader

FROM alpine:latest
COPY --from=builder /go/bin/recipe4reader /usr/bin/
ENTRYPOINT ["/usr/bin/recipe4reader"]
