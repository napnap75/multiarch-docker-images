FROM alpine:latest AS builder

ARG TARGETPLATFORM

RUN apk add --no-cache curl jq \
	&& VERSION=$(curl -s https://api.github.com/repos/influxdata/telegraf/releases/latest | jq '.name' | sed -E "s/.*([0-9]+\.[0-9]+\.[0-9]+).*/\1/") \
	&& curl -s -o entrypoint.sh https://raw.githubusercontent.com/influxdata/influxdata-docker/master/telegraf/1.17/alpine/entrypoint.sh \
	&& DOWNLOAD_ARCH=$(echo ${TARGETPLATFORM} | sed "s#arm/v6#armel#" | sed "s#arm/v7#armhf#" | sed "s#/#_#") \
	&& curl -s -o telegraf.tgz https://dl.influxdata.com/telegraf/releases/telegraf-${VERSION}_${DOWNLOAD_ARCH}.tar.gz \
	&& tar -z -x -f telegraf.tgz

FROM alpine:3.12

COPY --from=builder telegraf*/usr/bin/telegraf /usr/bin/
COPY --from=builder entrypoint.sh /entrypoint.sh

RUN echo 'hosts: files dns' >> /etc/nsswitch.conf \
	&& apk add --no-cache iputils ca-certificates net-snmp-tools procps lm_sensors tzdata \
	&& update-ca-certificates \
	&& chmod +x /usr/bin/telegraf /entrypoint.sh

EXPOSE 8125/udp 8092/udp 8094

ENTRYPOINT ["/entrypoint.sh"]
CMD ["telegraf"]
