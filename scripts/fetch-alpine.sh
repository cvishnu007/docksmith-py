#!/bin/sh
# One-time setup: download the Alpine Linux mini root filesystem and import it
# as a Docksmith base image. This is the ONLY step that touches the network;
# every build and run afterwards is fully offline.
#
#   sudo sh scripts/fetch-alpine.sh            # imports alpine:3.20
#   sudo sh scripts/fetch-alpine.sh alpine:dev # custom name:tag
set -eu

REF="${1:-alpine:3.20}"
VERSION="3.20.3"
BRANCH="v3.20"

case "$(uname -m)" in
  x86_64|amd64) ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  armv7l) ARCH=armv7 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

FILE="alpine-minirootfs-${VERSION}-${ARCH}.tar.gz"
URL="https://dl-cdn.alpinelinux.org/alpine/${BRANCH}/releases/${ARCH}/${FILE}"
cd "$(dirname "$0")/.."
DEST="${DOCKSMITH_DOWNLOADS:-.downloads}"

mkdir -p "$DEST"
if [ ! -f "$DEST/$FILE" ]; then
  echo "Downloading $URL"
  curl -fL --retry 3 -o "$DEST/$FILE.part" "$URL"
  mv "$DEST/$FILE.part" "$DEST/$FILE"
fi

python3 -m docksmith import "$DEST/$FILE" "$REF"
