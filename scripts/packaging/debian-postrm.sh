#!/bin/sh
set -eu
if [ "$1" = remove ] || [ "$1" = purge ]; then
    systemctl disable "${MILLIONAIRE_SERVICE_NAME:-millionaire-ce}" || true
    rm -f "/etc/systemd/system/${MILLIONAIRE_SERVICE_NAME:-millionaire-ce}.service"
    systemctl daemon-reload
fi
if [ "$1" = purge ]; then
    rm -rf /var/lib/millionaire-ce
fi
