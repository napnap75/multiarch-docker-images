#!/bin/bash

echo "-----------------------------------------------------------------------"
echo "apk add --no-cache git gcc musl-dev"
echo "go mod init github.com/napnap75/multiarch-docker-files/immich-souvenirs"
echo "go mod tidy"
echo "env CGO_ENABLED=1 env DEVELOPMENT-MODE=run-once go run immich-souvenirs.go"
echo "-----------------------------------------------------------------------"

docker run -it -v $(pwd)/immich-souvenirs.go:/app/immich-souvenirs.go -v $(pwd)/internals:/app/internals -w /app -v immich-souvenirs_config:/config --env-file test.env --rm golang:1.23-alpine /bin/sh
