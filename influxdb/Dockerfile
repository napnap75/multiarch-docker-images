FROM alpine:latest AS builder

ARG TARGETPLATFORM

RUN apk add --no-cache curl jq \
	&& VERSION=$(curl -s https://api.github.com/repos/influxdata/influxdb/releases/latest | jq '.name' | sed -E "s/.*([0-9]+\.[0-9]+\.[0-9]+).*/\1/") \
	&& curl -o entrypoint.sh https://raw.githubusercontent.com/influxdata/influxdb/master/docker/influxd/entrypoint.sh \
	&& DOWNLOAD_ARCH=$(echo ${TARGETPLATFORM} | sed "s#/#_#") \
	&& curl -o influxdb.tgz https://dl.influxdata.com/influxdb/releases/influxdb2-${VERSION}_${DOWNLOAD_ARCH}.tar.gz \
	&& tar -z -x -f influxdb.tgz --strip-components=1

FROM debian:stable-slim

COPY --from=builder influx influxd /usr/bin/

EXPOSE 8086

ENV DEBIAN_FRONTEND noninteractive

COPY --from=builder entrypoint.sh /entrypoint.sh

RUN apt-get update \
	&& apt-get install -y \
	ca-certificates \
	tzdata \
	&& apt-get clean autoclean \
	&& apt-get autoremove --yes \
	&& rm -rf /var/lib/{apt,dpkg,cache,log}

ENTRYPOINT ["/entrypoint.sh"]
CMD ["influxd"]
