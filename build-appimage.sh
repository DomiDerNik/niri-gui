#!/bin/sh
# build-appimage.sh - builds a portable AppImage of niri-settings.py
#
#   ./build-appimage.sh            needs podman or docker; result: ./dist/*.AppImage
#
# How it works: an Arch Linux container installs Python, GTK4, libadwaita and PyGObject, then
# "quick-sharun" (https://github.com/pkgforge-dev/Anylinux-AppImages) bundles everything,
# including glibc, so the AppImage does not depend on the libraries of the host distro and
# does not need FUSE. Building on Arch is what that project requires.
#
# Options (environment variables):
#   VERSION=1.2.3         version in the file name (default: today's date)
#   NO_TEST=1             skip the 12 second start test at the end
#   CONTAINER_ENGINE=...  podman or docker (default: whichever is found)
#   QS_URL=...            where to download quick-sharun from (pin a commit if you want reproducible builds)
#
# Without podman/docker: push this folder to a GitHub repo - the included workflow
# (.github/workflows/appimage.yml) runs "--inside" on GitHub and offers the AppImage as a download.
#
# "--inside" is used by this script itself and MUST only run inside the container
# (it installs packages and writes to /usr). "--assets-dir DIR" only writes the .desktop file and icon.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=${IMAGE:-docker.io/library/archlinux:latest}
QS_URL=${QS_URL:-https://raw.githubusercontent.com/pkgforge-dev/Anylinux-AppImages/refs/heads/main/useful-tools/quick-sharun.sh}

die() { echo "ERROR: $*" >&2; exit 1; }

write_assets() {
    mkdir -p "$1"
    cat > "$1/niri-settings.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Niri Settings
Comment=Settings for the niri compositor
Exec=niri-settings
Icon=niri-settings
Terminal=false
Categories=Settings;DesktopSettings;GTK;
StartupWMClass=dev.niri.Settings
EOF
    cat > "$1/niri-settings.svg" <<'EOF'
<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <rect width="256" height="256" rx="56" fill="#1f2430"/>
  <path d="M64 88h128M64 128h128M64 168h128" fill="none" stroke="#7fc8ff" stroke-width="14" stroke-linecap="round"/>
  <circle cx="104" cy="88" r="20" fill="#ffc87f"/>
  <circle cx="152" cy="128" r="20" fill="#ffc87f"/>
  <circle cx="116" cy="168" r="20" fill="#ffc87f"/>
</svg>
EOF
}

host_main() {
    [ -f "$HERE/niri-settings.py" ] || die "niri-settings.py must be in the same folder as this script"
    engine=${CONTAINER_ENGINE:-}
    if [ -z "$engine" ]; then
        engine=$(command -v podman || command -v docker || true)
    fi
    [ -n "$engine" ] || die "podman or docker is required (NixOS: virtualisation.podman.enable = true;)"
    mkdir -p "$HERE/dist"
    echo "Building in an Arch Linux container with $engine ..."
    exec "$engine" run --rm \
        -v "$HERE:/work:Z" -w /work \
        -e VERSION="${VERSION:-}" -e NO_TEST="${NO_TEST:-0}" -e QS_URL="$QS_URL" \
        -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
        "$IMAGE" sh /work/build-appimage.sh --inside
}

inside_main() {
    [ "$(id -u)" = 0 ] || die "--inside must run as root in the build container"
    WORK=${WORK:-/work}   # the folder with niri-settings.py (GitHub Actions sets it to the checkout)
    [ -f "$WORK/niri-settings.py" ] || die "$WORK/niri-settings.py not found"

    pacman -Syu --noconfirm --needed \
        gcc patchelf binutils gawk grep findutils wget \
        python python-gobject python-cairo gtk4 libadwaita \
        adwaita-icon-theme hicolor-icon-theme shared-mime-info librsvg mesa \
        xorg-server-xvfb xorg-xauth dbus ttf-dejavu

    # quick-sharun deploys what is installed in /usr, so the app has to be installed there first
    install -Dm755 "$WORK/niri-settings.py" /usr/bin/niri-settings
    python -m py_compile "$WORK/niri-settings.py"
    python -c "import gi, cairo; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')
from gi.repository import Gtk, Adw; print('GTK', Gtk.get_minor_version(), 'libadwaita', Adw.get_minor_version())"

    B=/tmp/build
    rm -rf "$B"
    mkdir -p "$B"
    write_assets "$B/assets"
    cd "$B"
    wget --retry-connrefused --tries=30 -qO quick-sharun "$QS_URL"
    chmod +x quick-sharun

    ARCH=$(uname -m)
    VERSION=${VERSION:-$(date +%Y.%m.%d)}
    export ARCH VERSION
    export OUTPATH="$WORK/dist"
    export MAIN_BIN=niri-settings
    export DESKTOP="$B/assets/niri-settings.desktop"
    export ICON="$B/assets/niri-settings.svg"
    export DEPLOY_PYTHON=1          # bundle the system Python (and, with GTK, the gobject-introspection typelibs)
    export DEPLOY_GTK=1
    export GTK_DIR=gtk-4.0
    export STRACE_BINARY=niri-settings   # only run our app (not the bare python binaries) to discover dlopen()ed libs

    # extra things quick-sharun cannot discover by itself: libraries that Python modules load,
    # the two compiled PyGObject/pycairo modules (so their dependencies get bundled) and the icon theme
    EXTRA=""
    for f in /usr/lib/libadwaita-1.so* /usr/lib/libgtk-4.so* /usr/lib/libcairo-gobject.so* \
             /usr/lib/python3*/site-packages/gi/_gi*.so /usr/lib/python3*/site-packages/cairo/_cairo*.so \
             /usr/share/icons/Adwaita; do
        [ -e "$f" ] && EXTRA="$EXTRA $f"
    done
    # shellcheck disable=SC2086
    ./quick-sharun /usr/bin/niri-settings $EXTRA
    ./quick-sharun --make-appimage

    img=$(ls -1 "$OUTPATH"/*.AppImage 2>/dev/null | head -n 1 || true)
    [ -n "$img" ] || die "no AppImage was produced"
    if [ "${NO_TEST:-0}" != 1 ]; then
        ./quick-sharun --test "$img" || echo "WARNING: the start test failed. This can be a false alarm in a container without a GPU - try the AppImage on your desktop."
    fi
    if [ -n "${HOST_UID:-}" ]; then
        chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$OUTPATH"
    fi
    echo
    echo "Done: dist/$(basename "$img")"
}

case "${1:-}" in
    --inside)     inside_main ;;
    --assets-dir) [ -n "${2:-}" ] || die "--assets-dir needs a folder"; write_assets "$2" ;;
    "")           host_main ;;
    *)            die "unknown option '$1' (see the top of this file)" ;;
esac
