## Development env
docker run --rm -it -v $PWD:/go/src/napnap75/immich-souvenirs -v immich-souvenirs_config:/config golang:1.21-alpine /bin/sh
apk add --no-cache git gcc musl-dev nano
cd src/napnap75/immich-souvenirs/
go mod init github.com/napnap75/multiarch-docker-files/immich-souvenirs
go get -d -v
go run immich-souvenirs.go --immich-url="https://immich.nappez.com" --immich-key="wRJWYUGlEfWGQ3a5lckHivLCU40s7ldEZSmikpmsE30" --whatsapp-session-file=/config/ws.gob --whatsapp-group="120363288639885954@g.us" --run-once
