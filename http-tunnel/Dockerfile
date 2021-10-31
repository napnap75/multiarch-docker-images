FROM alpine:latest AS builder

ARG TARGETPLATFORM

RUN DOWNLOAD_ARCH=$(echo ${TARGETPLATFORM} | cut -d"/" -f 2) \
	&& apk add --no-cache curl \
	&& DOWNLOAD_URL=$(curl -s https://api.github.com/repos/mmatczuk/go-http-tunnel/releases/latest | grep "browser_download_url" | grep "tunnel_linux_"${DOWNLOAD_ARCH}"\." | cut -d\" -f4) \
	&& curl --retry 3 -L -s -o tunnel_linux_arm.tar.gz ${DOWNLOAD_URL} \
	&& tar zxvf tunnel_linux_arm.tar.gz

FROM alpine:latest

COPY --from=builder tunnel tunneld /usr/bin/

RUN adduser -DH tunnel

USER tunnel
