#!/bin/sh
set -eu
install_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ -f "$install_root/.millionaire-ce-install" ] || {
    printf '%s\n' 'Refusing to remove an unrecognized installation.' >&2; exit 1;
}
[ "$install_root" != / ] || exit 1
scope=$(cat "$install_root/service-scope")
set --
if [ "$scope" = system ]; then set -- --system; fi
"$install_root/current/millionaire-ce" service uninstall "$@"
bin_dir=$(cat "$install_root/bin-dir")
if [ -L "$bin_dir/millionaire-ce" ] && \
    [ "$(readlink "$bin_dir/millionaire-ce")" = "$install_root/current/millionaire-ce" ]; then
    rm "$bin_dir/millionaire-ce"
fi
rm -rf "$install_root"
printf '%s\n' 'Millionaire CE removed. User settings, extensions and data were retained.'
