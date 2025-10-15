FROM alpine:3.21

RUN apk add --no-cache openssh

COPY entrypoint.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

CMD ["/usr/sbin/sshd", "-D", "-e"]
