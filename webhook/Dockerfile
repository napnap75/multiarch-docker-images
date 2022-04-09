FROM golang:alpine3.11 AS build
WORKDIR /go/src/github.com/adnanh/webhook
RUN apk add --update -t build-deps curl libc-dev gcc libgcc jq \
	&& DOWNLOAD_URL=$(curl -s https://api.github.com/repos/adnanh/webhook/releases/latest | jq -r '.tarball_url') \
	&& curl -L --silent -o webhook.tgz ${DOWNLOAD_URL} \
	&& tar -xzf webhook.tgz --strip 1 \
	&& go get -d \
	&& go build -o /usr/local/bin/webhook

FROM alpine:3.11
RUN apk add --no-cache openssl expect
COPY --from=build /usr/local/bin/webhook /usr/local/bin/webhook
WORKDIR /etc/webhook
VOLUME ["/etc/webhook"]
EXPOSE 9000
ENTRYPOINT ["/usr/local/bin/webhook"]
