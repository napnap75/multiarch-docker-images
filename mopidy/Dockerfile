FROM alpine:edge AS builder

ARG TARGETPLATFORM

RUN apk add --no-cache curl \
	&& DOWNLOAD_ARCH=$(echo ${TARGETPLATFORM} | sed "s#linux/arm/v6#arm#" | sed "s#linux/arm/v7#armhf#" | sed "s#linux/arm64#aarch64#" | sed "s#linux/amd64#amd64#") \
	&& echo "DOWNLOAD_ARCH=${DOWNLOAD_ARCH}" \
	&& S6_DOWNLOAD_URL=$(curl -s https://api.github.com/repos/just-containers/s6-overlay/releases/latest | grep "browser_download_url" | grep "s6-overlay-${DOWNLOAD_ARCH}.tar.gz\"" | cut -d\" -f4) \
	&& curl --retry 3 -L -s -o /tmp/s6-overlay.tar.gz $S6_DOWNLOAD_URL \
	&& mkdir /tmp/s6-overlay \
	&& tar xzf /tmp/s6-overlay.tar.gz -C /tmp/s6-overlay

FROM alpine:edge

RUN apk add --no-cache py3-pip mopidy \
	&& echo "30 7 * * * mopidy local scan" | crontab -

COPY --from=builder /tmp/s6-overlay /
COPY etc /etc/

ENTRYPOINT ["/init"]
