#!/bin/sh
set -eu
if ! getent passwd millionaire-ce >/dev/null; then
    adduser --system --group --home /var/lib/millionaire-ce millionaire-ce
fi
install -d -m 0700 -o millionaire-ce -g millionaire-ce /var/lib/millionaire-ce
/opt/millionaire-ce/runtime/bin/python3.13 -m quantide.cli service install
systemctl start "${MILLIONAIRE_SERVICE_NAME:-millionaire-ce}"
