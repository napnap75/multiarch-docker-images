FROM alpine:latest AS builder

ARG TARGETPLATFORM

RUN DOWNLOAD_ARCH=$(echo ${TARGETPLATFORM} | cut -d"/" -f 2) \
	&& if [[ $DOWNLOAD_ARCH == arm* ]] ; then DOWNLOAD_ARCH=arm ; fi \
	&& apk add --no-cache curl jq \
	&& DOWNLOAD_URL=$(curl -s https://api.github.com/repos/mmatczuk/go-http-tunnel/releases/latest | jq -r '.assets | map(select(.name == "tunnel_linux_'${DOWNLOAD_ARCH}'.tar.gz"))[0].browser_download_url') \
	&& curl --retry 3 -L -s -o tunnel_linux_arm.tar.gz ${DOWNLOAD_URL} \
	&& tar zxvf tunnel_linux_arm.tar.gz

FROM alpine:latest

COPY --from=builder tunnel tunneld /usr/bin/

RUN adduser -DH tunnel

USER tunnel
