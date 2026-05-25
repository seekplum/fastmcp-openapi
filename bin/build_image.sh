#!/usr/bin/env bash

set -xe

[[ -n "$BUILD_HOME" && -n "$BUILD_IMAGE" && -n "$BUILD_TAG" ]] || exit 1

cd "$BUILD_HOME"

docker buildx build \
    --build-arg BUILDKIT_INLINE_CACHE=1 \
    --build-arg BUILD_ID="${JENKINS_BUILD_ID}" \
    --build-arg BUILD_VERSION="${FASTMCP_OPENAPI_BUILD_TAG}" \
    --file ./docker/Dockerfile \
    --load \
    --network=host \
    --tag "$BUILD_IMAGE:$BUILD_TAG" .
