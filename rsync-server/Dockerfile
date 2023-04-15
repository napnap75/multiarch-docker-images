FROM alpine:latest

ADD rsync-server.sh /usr/bin/rsync-server.sh

RUN apk add --no-cache bash rsync \
	&& chmod +x /usr/bin/rsync-server.sh

CMD /usr/bin/rsync-server.sh
