#!/usr/bin/env bash
TAG=latest

# Enable common error handling options.
set -o errexit
set -o nounset
set -o pipefail

# Parse input arguments
while [[ $# -gt 0 ]]; do
    key="$1"
    case $key in
    -t | --tag)
        TAG="$2"
        shift
        shift
        ;;
        # Add the ability to specify build-arg MAMBA_USER_ID
    -u | --user-id)
        MAMBA_USER_ID="$2"
        shift
        shift
        ;;
    *)
        echo "Unknown option: $key"
        exit 1
        ;;
    esac
done

# Use 'latest' as the default tag if not specified
if [ -z "$TAG" ]; then
    TAG="latest"
fi

# Build the Docker image
if [ -z "${MAMBA_USER_ID+x}" ]; then
    docker build --network=host -t opera-adt/dolphin:"$TAG" --file docker/Dockerfile .
else
    docker build --network=host -t opera-adt/dolphin:"$TAG" --build-arg MAMBA_USER_ID="$MAMBA_USER_ID" --file docker/Dockerfile .
fi
