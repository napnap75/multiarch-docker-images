FROM alpine:latest

RUN apk add --no-cache snapcast-server su-exec

COPY start.sh /usr/local/bin

ENTRYPOINT [ "/usr/local/bin/start.sh" ]
