#!/bin/sh
# Compatibility entrypoint; only the deployed application is executed.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec bash "$HERE/project-start.sh" "$@"
