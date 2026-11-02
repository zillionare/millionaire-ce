#!/bin/sh
set -eu
runtime_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$runtime_root/runtime/bin/python3.13" -m quantide.cli "$@"
