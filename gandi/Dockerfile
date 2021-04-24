FROM alpine:latest

ADD updatedns.sh /usr/bin/updatedns.sh

RUN apk add --no-cache curl jq \
	&& chmod +x /usr/bin/updatedns.sh

CMD /usr/bin/updatedns.sh
