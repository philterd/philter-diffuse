#!/bin/bash
set -e

# Builds the Philter Diffuse Docker image for amd64 and arm64. Pushing it is a
# separate, manual step: see push-image.sh.
#
# Each architecture is built and loaded under its own tag, so both are here to
# run and test. push-image.sh pushes those tags and joins them into one
# multi-architecture tag.

# The version defaults to the one in main.py. Pass one to override it, such as latest.
VERSION=${1:-$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' main.py)}
IMAGE=${IMAGE:-philterd/philter-diffuse}
ARCHES=${ARCHES:-"amd64 arm64"}

# The default builder cannot cross-build, so use a container builder.
docker buildx inspect philter-diffuse-builder > /dev/null 2>&1 ||
    docker buildx create --name philter-diffuse-builder --driver docker-container > /dev/null

for arch in $ARCHES; do
    docker buildx build --builder philter-diffuse-builder \
        --platform "linux/${arch}" --load \
        -t "${IMAGE}:${VERSION}-${arch}" .
done

echo
for arch in $ARCHES; do
    echo "Built ${IMAGE}:${VERSION}-${arch}"
done
echo "Push them with: ./push-image.sh ${VERSION}"
