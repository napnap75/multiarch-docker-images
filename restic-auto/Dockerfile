FROM alpine:latest AS builder

RUN apk add --no-cache curl \
	&& while [ "$DOWNLOAD_URL" == "" ] ; do DOWNLOAD_URL=$(curl -s https://api.github.com/repos/restic/restic/releases/latest | grep "browser_download_url" | grep "linux_amd64\." | cut -d\" -f4) ; done \
	&& curl --retry 3 -L -s -o restic.bz2 ${DOWNLOAD_URL} \
	&& bunzip2 restic.bz2 \
	&& chmod +x restic

FROM alpine:latest

COPY --from=builder restic /usr/bin/

RUN apk add --no-cache bash curl jq openssh-client dcron tzdata

COPY restic-auto docker-entrypoint.sh docker-command.sh /usr/local/bin/

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

CMD ["/usr/local/bin/docker-command.sh"]
