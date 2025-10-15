FROM alpine:latest AS builder

ARG TARGETPLATFORM

RUN DOWNLOAD_ARCH=$(echo ${TARGETPLATFORM} | cut -d"/" -f 2) \
	&& apk add --no-cache curl \
	&& DOWNLOAD_URL=$(curl -s https://api.github.com/repos/bdd/runitor/releases/latest | grep "browser_download_url" | grep "linux-"${DOWNLOAD_ARCH} | cut -d\" -f4) \
	&& curl --retry 3 -L -s -o runitor ${DOWNLOAD_URL} \
	&& chmod +x runitor

FROM alpine:latest

COPY --from=builder runitor /usr/bin/

ADD dnsupdater.sh docker-command.sh /usr/bin/

RUN apk add --no-cache bash curl jq bind-tools \
	&& chmod +x /usr/bin/dnsupdater.sh

CMD ["/usr/bin/docker-command.sh"]
