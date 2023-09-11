FROM alpine:latest

ADD dnsupdater.sh /usr/bin/dnsupdater.sh

RUN apk add --no-cache bash curl jq bind-tools \
	&& chmod +x /usr/bin/dnsupdater.sh

CMD /usr/bin/dnsupdater.sh
