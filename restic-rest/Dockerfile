FROM golang:alpine AS builder

RUN apk add --no-cache git \
	&& mkdir -p /go/src/github.com/restic \
	&& cd /go/src/github.com/restic \
	&& git clone https://github.com/restic/rest-server \
	&& cd rest-server \
	&& go run build.go

FROM alpine:latest

COPY --from=builder /go/src/github.com/restic/rest-server/rest-server /go/src/github.com/restic/rest-server/docker/* /usr/bin/

RUN apk add --no-cache apache2-utils \
	&& adduser -DH rest-server

USER rest-server

VOLUME /data

ENV DATA_DIRECTORY=/data PASSWORD_FILE=/data/.htpasswd

ENTRYPOINT ["/usr/bin/entrypoint.sh"]
