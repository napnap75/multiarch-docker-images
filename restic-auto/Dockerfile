FROM alpine:latest AS builder

ARG TARGETPLATFORM

RUN DOWNLOAD_ARCH=$(echo ${TARGETPLATFORM} | cut -d"/" -f 2) \
	&& apk add --no-cache curl \
	&& DOWNLOAD_URL=$(curl -s https://api.github.com/repos/restic/restic/releases/latest | grep "browser_download_url" | grep "linux_"${DOWNLOAD_ARCH}"\." | cut -d\" -f4) \
	&& curl --retry 3 -L -s -o restic.bz2 ${DOWNLOAD_URL} \
	&& bunzip2 restic.bz2 \
	&& chmod +x restic

FROM alpine:latest

COPY --from=builder restic /usr/bin/

RUN apk add --no-cache bash curl jq openssh-client dcron tzdata

COPY restic-auto docker-entrypoint.sh docker-command.sh /usr/local/bin/

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

CMD ["/usr/local/bin/docker-command.sh"]
