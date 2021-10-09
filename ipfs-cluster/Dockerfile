# Note: when updating the go minor version here, also update the go-channel in snap/snapcraft.yml
FROM golang:1.16.7-buster

RUN apt-get update && apt-get install -y libssl-dev ca-certificates fuse 

ENV IPFS_CLUSTER_VERSION v0.14.1
RUN set -eux; \
    dpkgArch="$(dpkg --print-architecture)"; \
    case "${dpkgArch##*-}" in \
        "armhf") dpkgArch="arm";; \
    esac \
  && cd /tmp \
  && for BINARY in ipfs-cluster-ctl ipfs-cluster-follow ipfs-cluster-service ; do \
    curl -L -s -o ${BINARY}.tgz https://dist.ipfs.io/${BINARY}/$IPFS_CLUSTER_VERSION/${BINARY}_${IPFS_CLUSTER_VERSION}_linux-${dpkgArch}.tar.gz; \
    tar xzf ${BINARY}.tgz; \
  done \
  && curl -L -s -o entrypoint.sh https://raw.githubusercontent.com/ipfs/ipfs-cluster/master/docker/entrypoint.sh

ENV SUEXEC_VERSION v0.2
ENV TINI_VERSION v0.19.0
RUN set -eux; \
    dpkgArch="$(dpkg --print-architecture)"; \
    case "${dpkgArch##*-}" in \
        "amd64" | "armhf" | "arm64") tiniArch="tini-static-$dpkgArch" ;;\
        *) echo >&2 "unsupported architecture: ${dpkgArch}"; exit 1 ;; \
    esac; \
  cd /tmp \
  && git clone https://github.com/ncopa/su-exec.git \
  && cd su-exec \
  && git checkout -q $SUEXEC_VERSION \
  && make su-exec-static \
  && cd /tmp \
  && wget -q -O tini https://github.com/krallin/tini/releases/download/$TINI_VERSION/$tiniArch \
  && chmod +x tini

# Now comes the actual target image, which aims to be as small as possible.
FROM busybox:1.31.1-glibc

COPY --from=0 /tmp/ipfs-cluster-ctl/ipfs-cluster-ctl /tmp/ipfs-cluster-follow/ipfs-cluster-follow /tmp/ipfs-cluster-service/ipfs-cluster-service /usr/local/bin/
COPY --from=0 /tmp/su-exec/su-exec-static /sbin/su-exec
COPY --from=0 /tmp/tini /sbin/tini
COPY --from=0 /bin/fusermount /usr/local/bin/fusermount
COPY --from=0 /etc/ssl/certs /etc/ssl/certs

ENV IPFS_CLUSTER_PATH      /data/ipfs-cluster
ENV IPFS_CLUSTER_CONSENSUS crdt
ENV IPFS_CLUSTER_DATASTORE leveldb

RUN mkdir -p $IPFS_CLUSTER_PATH && \
    adduser -D -h $IPFS_CLUSTER_PATH -u 1000 -G users ipfs && \
    chown ipfs:users $IPFS_CLUSTER_PATH

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]

CMD ["daemon"]
