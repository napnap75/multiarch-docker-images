FROM golang:1.16.7-buster

RUN apt-get update && apt-get install -y libssl-dev ca-certificates fuse 

ENV IPFS_VERSION v0.10.0
RUN set -eux; \
    dpkgArch="$(dpkg --print-architecture)"; \
    case "${dpkgArch##*-}" in \
        "armhf" | "armel") dpkgArch="arm" ;;\
    esac; \
  cd /tmp \
  && curl -L -s -o go-ipfs.tgz https://github.com/ipfs/go-ipfs/releases/download/$IPFS_VERSION/go-ipfs_${IPFS_VERSION}_linux-${dpkgArch}.tar.gz \
  && tar xzf go-ipfs.tgz \
  && curl -L -s -o container_daemon https://raw.githubusercontent.com/ipfs/go-ipfs/master/bin/container_daemon

ENV SUEXEC_VERSION v0.2
ENV TINI_VERSION v0.19.0
RUN set -eux; \
    dpkgArch="$(dpkg --print-architecture)"; \
    case "${dpkgArch##*-}" in \
        "amd64" | "armhf" | "armel" | "arm64") tiniArch="tini-static-$dpkgArch" ;;\
        *) echo >&2 "unsupported architecture: ${dpkgArch}"; exit 1 ;; \
    esac \
  && cd /tmp \
  && git clone https://github.com/ncopa/su-exec.git \
  && cd su-exec \
  && git checkout -q $SUEXEC_VERSION \
  && make su-exec-static \
  && cd /tmp \
  && wget -q -O tini https://github.com/krallin/tini/releases/download/$TINI_VERSION/$tiniArch \
  && chmod +x tini

# Now comes the actual target image, which aims to be as small as possible.
FROM busybox:1.31.1-glibc

COPY --from=0 /tmp/go-ipfs/ipfs /usr/local/bin/ipfs
COPY --from=0 /tmp/container_daemon /usr/local/bin/start_ipfs
COPY --from=0 /tmp/su-exec/su-exec-static /sbin/su-exec
COPY --from=0 /tmp/tini /sbin/tini
COPY --from=0 /bin/fusermount /usr/local/bin/fusermount
COPY --from=0 /etc/ssl/certs /etc/ssl/certs

RUN chmod 4755 /usr/local/bin/fusermount \
  && chmod 0755 /usr/local/bin/start_ipfs

COPY --from=0 /lib/*-linux-gnu*/libdl.so.2 /lib/
COPY --from=0 /usr/lib/*-linux-gnu*/libssl.so* /usr/lib/
COPY --from=0 /usr/lib/*-linux-gnu*/libcrypto.so* /usr/lib/

ENV IPFS_PATH /data/ipfs
RUN mkdir -p $IPFS_PATH \
  && adduser -D -h $IPFS_PATH -u 1000 -G users ipfs \
  && chown ipfs:users $IPFS_PATH \
  && mkdir /ipfs /ipns \
  && chown ipfs:users /ipfs /ipns

ENV IPFS_LOGGING ""

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/start_ipfs"]

CMD ["daemon", "--migrate=true"]
