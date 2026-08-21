FROM debian:stable-slim

RUN apt update && apt update && apt install rsync -y \
 && touch /etc/rsyncd.secrets \
 && chmod 600 /etc/rsyncd.secrets

VOLUME ["/shares"]

EXPOSE 873

COPY rsyncd.conf /etc/rsyncd.conf
COPY scripts/ /scripts/

HEALTHCHECK CMD ["/scripts/docker-healthcheck.sh"]
ENTRYPOINT ["/scripts/docker-entrypoint.sh"]

CMD [ "rsync", "--no-detach", "--daemon", "--config", "/etc/rsyncd.conf" ]
