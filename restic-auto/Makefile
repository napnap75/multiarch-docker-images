all: build push

build:
	docker build -t napnap75/restic-auto:latest-amd64 -f Dockerfile.amd64 .
	docker build -t napnap75/restic-auto:latest-arm32v6 -f Dockerfile.arm32v6 .

push:
	if [ "${DOCKER_USERNAME}" != "" ]; then docker login -u="${DOCKER_USERNAME}" -p="${DOCKER_PASSWORD}" ; fi
	docker push napnap75/restic-auto:latest-amd64
	docker push napnap75/restic-auto:latest-arm32v6
	env DOCKER_CLI_EXPERIMENTAL=enabled docker manifest create napnap75/restic-auto:latest napnap75/restic-auto:latest-amd64 napnap75/restic-auto:latest-arm32v6
	env DOCKER_CLI_EXPERIMENTAL=enabled docker manifest push -p napnap75/restic-auto:latest
	version=$$(docker run --rm -it napnap75/restic-auto:latest restic version | egrep -o "restic [.0-9]+ compiled" | egrep -o "[.0-9]+") ; \
		docker tag napnap75/restic-auto:latest-amd64 napnap75/restic-auto:$$version-amd64 ; \
		docker push napnap75/restic-auto:$$version-amd64 ; \
		docker tag napnap75/restic-auto:latest-arm32v6 napnap75/restic-auto:$$version-arm32v6 ; \
		docker push napnap75/restic-auto:$$version-arm32v6 ; \
		env DOCKER_CLI_EXPERIMENTAL=enabled docker manifest create napnap75/restic-auto:$$version napnap75/restic-auto:$$version-amd64 napnap75/restic-auto:$$version-arm32v6 ; \
		env DOCKER_CLI_EXPERIMENTAL=enabled docker manifest push -p napnap75/restic-auto:$$version
