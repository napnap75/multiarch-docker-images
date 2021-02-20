
BUILD_PLATFORMS="linux/amd64,linux/arm/v6,linux/arm/v7,linux/arm64"
if [ -f $1/.build-env ] ; then
        source $1/.build-env
fi

docker buildx build --platform $BUILD_PLATFORMS -o type=image -t napnap75/$1:latest $1
