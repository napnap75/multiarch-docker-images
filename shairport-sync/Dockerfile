FROM alpine:3.16 AS builder

RUN apk update \
	&& apk add git build-base autoconf automake libtool alsa-lib-dev libdaemon-dev popt-dev libressl-dev soxr-dev avahi-dev libconfig-dev curl asio-dev flac-dev libvorbis-dev mosquitto-dev \
	&& mkdir /root/shairport-sync \
	&& cd /root/shairport-sync \
	&& while [ "$DOWNLOAD_URL" == "" ] ; do DOWNLOAD_URL=$(curl -s https://api.github.com/repos/mikebrady/shairport-sync/releases/latest | grep "tarball_url" | cut -d\" -f4) ; done \
	&& curl --retry 3 -L -s -o /tmp/shairport-sync.tar.gz $DOWNLOAD_URL \
	&& tar xzf /tmp/shairport-sync.tar.gz --strip-components=1 \
	&& autoreconf -i -f \
	&& ./configure --with-pipe --with-avahi --with-soxr --with-metadata --with-mqtt-client --with-ssl=openssl \
	&& make

RUN cd /root/shairport-sync \
	&& make install

FROM alpine:3.16

RUN apk add --no-cache avahi-libs dbus libdaemon popt libressl soxr avahi libconfig mosquitto-libs su-exec \
	&& addgroup shairport-sync \
	&& adduser -D shairport-sync -G shairport-sync

COPY --from=builder /root/shairport-sync/shairport-sync /usr/local/bin/
COPY --from=builder /usr/local/etc/shairport-sync.conf /etc/shairport-sync.conf
COPY start.sh /usr/local/bin

ENTRYPOINT [ "/usr/local/bin/start.sh" ]
CMD [ "-c", "/etc/shairport-sync.conf", "-u" ]
