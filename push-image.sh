#!/bin/bash
set -e

# Pushes the images built by build-image.sh to Docker Hub and joins them into a
# single multi-architecture tag. It builds nothing.
#
# Run this by hand, from a machine holding the credential.

# The version defaults to the one in main.py, matching build-image.sh.
VERSION=${1:-$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' main.py)}
IMAGE=${IMAGE:-philterd/philter-diffuse}
ARCHES=${ARCHES:-"amd64 arm64"}

for arch in $ARCHES; do
    docker push "${IMAGE}:${VERSION}-${arch}"
done

# Joins the pushed per-architecture images under one tag, in the registry.
sources=""
for arch in $ARCHES; do
    sources="${sources} ${IMAGE}:${VERSION}-${arch}"
done

docker buildx imagetools create -t "${IMAGE}:${VERSION}" ${sources}

echo
echo "Pushed ${IMAGE}:${VERSION} for ${ARCHES}"
