FROM alpine:latest AS builder

ARG TARGETPLATFORM

RUN apk add --no-cache curl jq \
	&& DOWNLOAD_ARCH=$(echo ${TARGETPLATFORM} | sed "s#linux/arm/v6#arm#" | sed "s#linux/arm/v7#armhf#" | sed "s#linux/arm64#aarch64#" | sed "s#linux/amd64#x86_64#") \
	&& mkdir /tmp/s6-overlay \
	&& S6_DOWNLOAD_URL=$(curl -s https://api.github.com/repos/just-containers/s6-overlay/releases/latest | jq -r '.assets | map(select(.name == "s6-overlay-'${DOWNLOAD_ARCH}'.tar.xz"))[0].browser_download_url') \
	&& curl --retry 3 -L -s -o /tmp/s6-overlay.tar.xz $S6_DOWNLOAD_URL \
	&& tar -xf /tmp/s6-overlay.tar.xz -C /tmp/s6-overlay \
	&& S6_DOWNLOAD_URL=$(curl -s https://api.github.com/repos/just-containers/s6-overlay/releases/latest | jq -r '.assets | map(select(.name == "s6-overlay-noarch.tar.xz"))[0].browser_download_url') \
	&& curl --retry 3 -L -s -o /tmp/s6-overlay.tar.xz $S6_DOWNLOAD_URL \
	&& tar -xf /tmp/s6-overlay.tar.xz -C /tmp/s6-overlay

FROM alpine:latest

RUN apk add --no-cache bash curl openssh-server rsync rrsync borgbackup
COPY --from=builder /tmp/s6-overlay /
ADD etc /etc/

ENTRYPOINT ["/init"]
CMD /usr/sbin/sshd -D -e
