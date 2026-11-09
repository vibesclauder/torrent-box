#!/usr/bin/env bash
# torrent-box installer: transmission-daemon + rss2transmission + a media server (Jellyfin or
# minidlna) + optionally a Samba share of the storage disk and Tailscale for remote access, on
# Ubuntu/Debian. Optionally Prowlarr, Sonarr, Radarr and Seerr in Docker, to search nCore and
# request movies and series. A start page on port 80 links to it all and shows its health.
#
# Idempotent: run it on a fresh box or again on an installed one. It only asks for what it
# doesn't know yet, never overwrites config.ini or existing secrets, and only touches
# system files whose content actually changes. Run as your normal user; it uses sudo for
# the system steps. See PLAN.md ("setup.sh") for the reasoning behind each step.

set -euo pipefail
umask 022
export LC_ALL=C  # we parse command output; keep it in English whatever the box's locale

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONF_DIR=$HOME/.config/rss2transmission
CONFIG=$CONF_DIR/config.ini
SECRETS=$CONF_DIR/secrets.ini
LEGACY_AUTH=$HOME/.config/transmission-auth  # "user:pass", from the pre-torrent-box setup
BIN=$HOME/.local/bin/rss2transmission
USER_UNITS=$HOME/.config/systemd/user
STATE_DIR=$HOME/.local/state/rss2transmission
TR_SETTINGS=/etc/transmission-daemon/settings.json
MINIDLNA_CONF=/etc/minidlna.conf
MINIDLNA_DB=/var/cache/minidlna/files.db  # db_dir in templates/minidlna.conf
JELLYFIN_REPO=https://repo.jellyfin.org
JELLYFIN_KEY=/etc/apt/keyrings/jellyfin.asc
JELLYFIN_SOURCES=/etc/apt/sources.list.d/jellyfin.sources
SMB_CONF=/etc/samba/smb.conf
SAMBA_SHARE=/etc/samba/torrent-box.conf  # our share, included from smb.conf
AVAHI_SERVICE=/etc/avahi/services/torrent-box.service  # what avahi announces besides the name
TAILSCALE_REPO=https://pkgs.tailscale.com/stable
TAILSCALE_KEY=/usr/share/keyrings/tailscale-archive-keyring.gpg  # where their .list expects it
TAILSCALE_SOURCES=/etc/apt/sources.list.d/tailscale.list
TAILNET_V4=100.64.0.0/10  # the addresses Tailscale hands out
TAILNET_V6=fd7a:115c:a1e0::/48
ARR_DIR=$HOME/.local/share/torrent-box/arr  # compose.yml + the apps' config
DASHBOARD_BIN=/usr/local/bin/torrent-box-dashboard
DASHBOARD_UNIT=/etc/systemd/system/torrent-box-dashboard.service
DASHBOARD_PORT=80  # the box's name alone opens the start page
DESIREERR_REPO=https://github.com/vibesclauder/desireerr.git  # requests for StashDB scenes (public)
DESIREERR_DIR=$HOME/desireerr  # its user unit runs %h/desireerr/desireerr.py
DESIREERR_PORT=5056
DESIREERR_DATA=$HOME/.local/state/desireerr  # its unit's StateDirectory
LIB=/usr/local/lib/torrent-box  # root's scripts: push, smartd-notify, arr-update
NTFY_PUBLIC=https://ntfy.sh
NTFY_PORT=2586  # our own ntfy server ([setup] own_ntfy = yes); 80 is the start page's
NTFY_LOCAL=http://localhost:$NTFY_PORT  # what everything on the box pushes to (the apps too: host networking)
NTFY_DROPIN=/etc/systemd/system/ntfy.service.d/torrent-box.conf
NTFY_REPO=https://archive.ntfy.sh/apt  # ntfy's own packages: the distro's have no web app
NTFY_KEY=/etc/apt/keyrings/ntfy.gpg
NTFY_KEY_FP=55BA774A6F5EE67431E4B6B7CFDB962D4F1EC4AF  # as ntfy's install docs give it
NTFY_SOURCES=/etc/apt/sources.list.d/ntfy.sources
NTFY_PIN=/etc/apt/preferences.d/ntfy
SMARTD_CONF=/etc/smartd.conf
JF_VAINFO=/usr/lib/jellyfin-ffmpeg/vainfo  # comes with jellyfin-ffmpeg, with its VAAPI drivers
USER=${USER:-$(id -un)}

DRY_RUN=0
SHADOW=0

usage() {
    cat <<EOF
Usage: ./setup.sh [--dry-run] [--shadow]

Installs and configures transmission-daemon, rss2transmission, a media server (Jellyfin,
minidlna or none), and optionally a Samba share of the storage disk and Tailscale for remote
access (asked on the first run, saved in config.ini [setup]), plus a start page with
links and health at http://<box>/.
Safe to re-run: existing config and secrets are kept, and it only asks for missing values.

  --dry-run   show what would be done; change nothing, never call sudo
  --shadow    also install systemd/shadow.conf (rss2transmission only does dry runs)
  -h, --help  this help

Answers can be given as environment variables instead of prompts:
  STORAGE_ROOT   mount point of the storage disk (default /mnt/storage)
  STORAGE_UUID   partition UUID to put in /etc/fstab (if not mounted / not in fstab yet)
  STORAGE_DISK   instead: the device to use (e.g. sdb1), or a blank disk to format (e.g. sdb)
  FEED_URL       nCore bookmark RSS URL
  RPC_USER, RPC_PASSWORD   Transmission login (web UI, Remote GUI, rss2transmission)
  LAN_SUBNET     e.g. 192.168.1.0/24, if auto-detection picks the wrong one
  MEDIA_SERVER   jellyfin, minidlna or none (first run only; later: config.ini [setup])
  SAMBA          yes or no: share the storage disk with Samba (same)
  SAMBA_PASSWORD Samba password for your user (only used when it has none yet)
  AVAHI          yes or no: announce the box on the LAN as <name>.local (asked once, later:
                 config.ini [setup])
  TAILSCALE      yes or no: remote access through Tailscale (first run only, like MEDIA_SERVER)
  TS_AUTHKEY     Tailscale auth key, to join the tailnet without opening a login link
  OWN_NTFY       yes or no: pushes through an ntfy server on this box instead of ntfy.sh
                 (asked once, later: config.ini [setup])
  ARR            yes or no: Prowlarr, Sonarr, Radarr and Seerr in Docker (first run only)
  WHISPARR       yes or no: Whisparr and Desireerr too, for nCore's XXX categories (with
                 ARR=yes; asked once, later: config.ini [setup])
  STASHDB_KEY    StashDB API key, for Desireerr (with WHISPARR=yes; kept in secrets.ini)
  NCORE_USER, NCORE_PASSWORD   nCore login for Prowlarr (with ARR=yes)
  NCORE_2FA      a current nCore 2FA code, when setup adds nCore to Prowlarr
  JELLYFIN_USER, JELLYFIN_PASSWORD   Jellyfin admin, if it isn't the Transmission login
  DOWNLOAD_MBPS  internet download speed in Mbit/s, sizes Transmission's cache (first run
                 only; otherwise setup measures it and asks)
EOF
}

### Output and running commands

banner() {
    cat <<'EOF'
 _                            _        _
| |_ ___  _ __ _ __ ___ _ __ | |_     | |__   _____  __
| __/ _ \| '__| '__/ _ \ '_ \| __|____| '_ \ / _ \ \/ /
| || (_) | |  | | |  __/ | | | ||_____| |_) | (_) >  <
 \__\___/|_|  |_|  \___|_| |_|\__|    |_.__/ \___/_/\_\
EOF
    printf '%55s\n\n' "$TB_VERSION"
}

# "1.0 rev 2, 2026-11-09 13:31": VERSION's number, one revision more for each commit since
# the one that last changed VERSION, and when the checked-out commit was made. Without git
# history (a downloaded archive) just what VERSION says.
repo_version() {
    local v base n
    v=$(<"$REPO/VERSION")
    git -C "$REPO" rev-parse -q --verify HEAD >/dev/null 2>&1 || { printf '%s' "$v"; return; }
    base=$(git -C "$REPO" log -1 --format=%H -- VERSION 2>/dev/null || true)
    if [[ -n $base && $v =~ ^(.*rev\ )([0-9]+)$ ]]; then
        n=$(git -C "$REPO" rev-list --count "$base..HEAD" 2>/dev/null || echo 0)
        v=${BASH_REMATCH[1]}$((BASH_REMATCH[2] + n))
    fi
    printf '%s, %s' "$v" "$(git -C "$REPO" log -1 --format=%cd --date=format-local:'%Y-%m-%d %H:%M')"
}

# A copy of the program SRC with the version written into its VERSION = "" line, to install.
stamped() {
    local out
    out=$TMP/$(basename "$1")
    sed "s|^VERSION = \"\".*|VERSION = \"$TB_VERSION\"|" "$1" >"$out"
    printf '%s' "$out"
}

step() { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

quote() {
    local out=() arg
    for arg in "$@"; do
        if [[ $arg =~ ^[[:alnum:]_./:=,+@%-]+$ ]]; then out+=("$arg"); else out+=("$(printf '%q' "$arg")"); fi
    done
    printf '%s' "${out[*]}"
}

# Commands that change something: printed instead of run in a dry run.
# None of them ever gets a secret on its command line.
run() {
    if (( DRY_RUN )); then info "would run: $(quote "$@")"; else "$@"; fi
}

as_root() {
    if (( DRY_RUN )); then info "would run: sudo $(quote "$@")"; else sudo "$@"; fi
}

helper() { PYTHONDONTWRITEBYTECODE=1 python3 "$REPO/setup_helper.py" "$@"; }

# Install SRC as DEST (mode MODE, as root if 4th arg is "root") unless it's already identical.
# Returns 0 if DEST changed (or would change), 1 if it was up to date.
update_file() {
    local src=$1 dest=$2 mode=$3 owner=${4:-}
    if [[ -f $dest ]] && cmp -s "$src" "$dest"; then
        info "up to date: $dest"
        return 1
    fi
    if (( DRY_RUN )); then
        if [[ -f $dest && -r $dest ]]; then
            info "would update $dest (changes other than comments and blank lines):"
            diff -u -B -I '^#' "$dest" "$src" | tail -n +3 | grep -Ev '^[-+](#|[[:space:]]*$)' |
                awk 'NR <= 40 { print "      " $0 } END { if (NR > 40) print "      ... " NR - 40 " more lines" }' || true
        elif [[ -f $dest ]]; then
            info "would update $dest (not readable, so no diff)"
        else
            info "would create $dest (mode $mode)"
        fi
        return 0
    fi
    if [[ $owner == root ]]; then
        sudo install -D -m "$mode" "$src" "$dest"
    else
        install -D -m "$mode" "$src" "$dest"
    fi
    info "installed $dest"
    return 0
}

need_terminal() {
    [[ -t 0 ]] || die "need an answer for '$1'; run setup.sh in a terminal (or see --help for env vars)"
}

# Can we draw a menu? (a terminal both ways that understands cursor movement)
interactive() { [[ -t 0 && -t 1 && ${TERM:-dumb} != dumb ]]; }

# ask VAR "question" [default]: a typed answer; in a terminal the default is already filled
# in (Enter takes it, or edit it), otherwise shown in brackets
ask() {  # (locals with a _ask prefix: VAR may be a caller's local of a common name)
    local _ask_reply _ask_prompt=$2
    need_terminal "$2"
    if interactive && [[ -n ${3:-} ]]; then
        read -r -e -i "$3" -p "   $_ask_prompt: " _ask_reply
    else
        [[ -z ${3:-} ]] || _ask_prompt+=" [$3]"
        read -r -p "   $_ask_prompt: " _ask_reply
    fi
    printf -v "$1" '%s' "${_ask_reply:-${3:-}}"
}

# menu VAR DEFAULT "question" VALUE LABEL [VALUE LABEL]...: pick one with the arrow keys (or
# j/k, or its number) and Enter; the default is selected to start with. Without a terminal
# that can draw it, the value is typed instead.
menu() {
    local var=$1 default=$2 question=$3 values=() labels=() i sel=0 n width=0 cols key rest
    shift 3
    while (( $# >= 2 )); do values+=("$1"); labels+=("$2"); shift 2; done
    n=${#values[@]}
    for i in "${!values[@]}"; do
        [[ ${values[i]} != "$default" ]] || sel=$i
        (( ${#values[i]} <= width )) || width=${#values[i]}
    done
    need_terminal "$question"
    if ! interactive; then
        while :; do
            ask "$var" "$question ($(IFS=/; echo "${values[*]}"))" "$default"
            for i in "${!values[@]}"; do [[ ${!var} != "${values[i]}" ]] || return 0; done
            info "choose one of: ${values[*]}"
        done
    fi
    cols=$(tput cols 2>/dev/null || echo 80)
    printf '   %s\n' "$question"
    printf '\e[?25l'  # no blinking cursor over the list
    trap 'printf "\e[?25h\n"; exit 130' INT
    while :; do
        for i in "${!values[@]}"; do  # one line each, cut to the screen: the redraw counts lines
            if (( i == sel )); then
                printf '\e[2K   \e[1;36m> %-*s\e[0m  %s\n' "$width" "${values[i]}" \
                    "${labels[i]:0:$(( cols - width - 9 > 0 ? cols - width - 9 : 0 ))}"
            else
                printf '\e[2K     %-*s  \e[2m%s\e[0m\n' "$width" "${values[i]}" \
                    "${labels[i]:0:$(( cols - width - 9 > 0 ? cols - width - 9 : 0 ))}"
            fi
        done
        printf '\e[2K   \e[2mUp/Down to move, Enter to choose\e[0m'
        IFS= read -rsn1 key || key=""
        case $key in
            $'\e')  # an arrow key: ESC [ A/B (or ESC O A/B)
                IFS= read -rsn2 -t 0.1 rest || rest=""
                case $rest in
                    '[A'|OA) sel=$(( (sel + n - 1) % n )) ;;
                    '[B'|OB) sel=$(( (sel + 1) % n )) ;;
                esac ;;
            k) sel=$(( (sel + n - 1) % n )) ;;
            j) sel=$(( (sel + 1) % n )) ;;
            [1-9]) (( key > n )) || sel=$(( key - 1 )) ;;
            '') break ;;  # Enter
        esac
        printf '\r\e[%dA' "$n"  # back up to the first option
    done
    # leave just the question and the answer
    printf '\r\e[%dA\e[J   %s \e[1m%s\e[0m\n\e[?25h' "$(( n + 1 ))" "$question" "${values[sel]}"
    trap - INT
    printf -v "$var" '%s' "${values[sel]}"
}

# ask_yes "question": default yes
ask_yes() {
    local yes_no
    menu yes_no yes "$1" yes "" no ""
    [[ $yes_no == yes ]]
}

# ask_no "question": default no
ask_no() {
    local yes_no
    menu yes_no no "$1" yes "" no ""
    [[ $yes_no == yes ]]
}

pkg_installed() {
    [[ $(dpkg-query -W -f='${Status}' "$1" 2>/dev/null) == "install ok installed" ]]
}

ini() { helper ini-get "$@" 2>/dev/null || true; }  # prints "" if the file/key is missing

### 1. OS and packages

check_os() {
    step "System"
    [[ -r /etc/os-release ]] || die "no /etc/os-release; this is for Ubuntu/Debian"
    local id like version name codename ubuntu_codename
    { read -r id; read -r like; read -r version; read -r name; read -r codename
      read -r ubuntu_codename; } < <(
        # shellcheck source=/dev/null
        . /etc/os-release
        printf '%s\n' "${ID:-}" "${ID_LIKE:-}" "${VERSION_ID:-}" "${PRETTY_NAME:-unknown}" \
            "${VERSION_CODENAME:-}" "${UBUNTU_CODENAME:-}")
    # For third-party apt repos (Jellyfin, Tailscale): which distribution and release to use.
    case "$id $like" in
        *ubuntu*) os_family=ubuntu os_suite=${ubuntu_codename:-$codename} ;;
        raspbian*) os_family=raspbian os_suite=$codename ;;
        *) os_family=debian os_suite=$codename ;;
    esac
    case "$id $like" in
        ubuntu*) [[ ${version%%.*} -ge 22 ]] || warn "made for Ubuntu 22.04+; this is $name" ;;
        debian*) [[ ${version%%.*} -ge 12 ]] || warn "made for Debian 12+; this is $name" ;;
        *debian*|*ubuntu*) warn "$name is Debian-based but untested" ;;
        *) die "this is for Ubuntu/Debian, not $name" ;;
    esac
    command -v systemctl >/dev/null || die "systemd is required"
    info "$name"
}

APT_UPDATED=0

apt_install() {  # apt_install PKG...: install the ones that are missing
    local pkg missing=()
    for pkg in "$@"; do
        pkg_installed "$pkg" || missing+=("$pkg")
    done
    if (( ${#missing[@]} == 0 )); then
        info "already installed: $*"
        return
    fi
    info "installing: ${missing[*]}"
    if (( ! APT_UPDATED )); then
        as_root apt-get update
        APT_UPDATED=1
    fi
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

install_packages() {
    apt_install transmission-daemon python3 curl  # the media server comes in its own step
}

### Network

detect_lan() {
    local out
    out=$(ip -o -4 route show | helper lan ${LAN_SUBNET:+--subnet "$LAN_SUBNET"}) ||
        die "could not detect the LAN subnet; set LAN_SUBNET (e.g. LAN_SUBNET=192.168.1.0/24)"
    read -r lan_net lan_whitelist lan_ip <<<"$out"
    info "LAN: $lan_net (Transmission whitelist $lan_whitelist)"
}

### 2. Storage disk

choose_storage_root() {
    if [[ -f $CONFIG ]]; then
        config_src=$CONFIG
        storage_root=$(ini --path "$CONFIG" general storage_root)
        [[ -n $storage_root ]] || die "$CONFIG has no [general] storage_root"
        if [[ -n ${STORAGE_ROOT:-} && ${STORAGE_ROOT%/} != "$storage_root" ]]; then
            warn "ignoring STORAGE_ROOT=$STORAGE_ROOT: $CONFIG says $storage_root"
        fi
    else
        if [[ -n ${STORAGE_ROOT:-} ]]; then
            storage_root=$STORAGE_ROOT
        elif (( DRY_RUN )); then
            storage_root=/mnt/storage
            info "would ask for the storage mount point (default /mnt/storage)"
        else
            ask storage_root "Mount point for the storage disk" /mnt/storage
        fi
        storage_root=${storage_root%/}
        # config.ini doesn't exist yet: prepare it here, it's installed in the config step
        config_src=$TMP/config.ini
        cp "$REPO/config.example.ini" "$config_src"
        if [[ $storage_root != /mnt/storage ]]; then
            printf 'storage_root=%s\n' "$storage_root" | helper ini-set "$config_src" general
        fi
    fi
    [[ $storage_root == /?* ]] || die "storage root must be an absolute path, not '$storage_root'"
    [[ $storage_root != *[[:space:]]* ]] || die "storage root can't contain spaces: '$storage_root'"
}

in_fstab() {  # in_fstab FIELD VALUE: is there an uncommented fstab line with field N = VALUE?
    awk -v n="$1" -v v="$2" '$1 !~ /^#/ && $n == v { found = 1 } END { exit !found }' /etc/fstab
}

setup_storage() {
    step "Storage disk ($storage_root)"
    if mountpoint -q "$storage_root"; then
        info "mounted: $(findmnt -no SOURCE,FSTYPE --target "$storage_root")"
        in_fstab 2 "$storage_root" ||
            warn "$storage_root is not in /etc/fstab, so it won't be mounted after a reboot"
        return
    fi

    if in_fstab 2 "$storage_root"; then
        info "already in /etc/fstab, mounting it"
    else
        local uuid=${STORAGE_UUID:-} fstype pass=2 fresh_fs=0
        if [[ -z $uuid ]]; then
            pick_storage_device || return 0  # sets uuid; formats a blank disk first
        fi
        [[ -e /dev/disk/by-uuid/$uuid ]] || die "no partition with UUID '$uuid'"
        if in_fstab 1 "UUID=$uuid"; then
            die "UUID $uuid is already in /etc/fstab with another mount point; fix /etc/fstab or set STORAGE_ROOT to that mount point"
        fi
        fstype=$(lsblk -dno FSTYPE "/dev/disk/by-uuid/$uuid")
        if [[ -z $fstype ]] && (( ! DRY_RUN )); then
            fstype=$(sudo blkid -s TYPE -o value "/dev/disk/by-uuid/$uuid")
        fi
        [[ -n $fstype ]] || die "can't tell the filesystem type of UUID $uuid"
        case $fstype in
            ntfs)
                fstype=ntfs-3g
                apt_install ntfs-3g ;;
        esac
        case $fstype in ntfs-3g|vfat|exfat) pass=0 ;; esac
        local line="UUID=$uuid $storage_root $fstype defaults,nofail 0 $pass"
        if (( DRY_RUN )); then
            info "would add to /etc/fstab: $line"
        else
            [[ -e /etc/fstab.before-torrent-box ]] || sudo cp -p /etc/fstab /etc/fstab.before-torrent-box
            printf '%s\n' "$line" | sudo tee -a /etc/fstab >/dev/null
            info "added to /etc/fstab: $line"
        fi
        as_root systemctl daemon-reload  # systemd generates mount units from fstab
    fi
    [[ -d $storage_root ]] || as_root mkdir -p "$storage_root"
    as_root mount "$storage_root"
    if (( ! DRY_RUN )); then
        mountpoint -q "$storage_root" || die "$storage_root is still not mounted"
        info "mounted: $(findmnt -no SOURCE,FSTYPE --target "$storage_root")"
    fi
    if (( ${fresh_fs:-0} )); then
        # a new filesystem's top folder is root's: let the debian-transmission group (you,
        # Transmission) add folders to it, like the ones make_folders creates
        as_root chgrp debian-transmission "$storage_root"
        as_root chmod 2775 "$storage_root"
    fi
}

# Which device becomes the storage disk: an unmounted filesystem (mounted as it is), or a blank
# disk (no partition table, no filesystem), which gets one partition, formatted ext4. A disk
# with anything on it is never offered for formatting; `sudo wipefs -a /dev/sdX` makes one
# blank, if you're sure. Sets uuid (and fresh_fs) in setup_storage; returns 1 in a dry run.
pick_storage_device() {
    local out dev kind id desc choice="" default="" items=()
    declare -A ids kinds
    out=$(lsblk -J -b -o PATH,PKNAME,TYPE,SIZE,FSTYPE,PTTYPE,MOUNTPOINT,RO,UUID,LABEL,MODEL |
        helper storage-candidates)
    while IFS=$'\t' read -r dev kind id desc; do
        [[ -n $dev ]] || continue
        ids[${dev#/dev/}]=$id
        kinds[${dev#/dev/}]=$kind
        items+=("${dev#/dev/}" "$desc")
        [[ -n $default ]] || default=${dev#/dev/}  # the biggest
    done <<<"$out"
    if (( ${#items[@]} == 0 )); then
        lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL | sed 's/^/   /'
        die "no unmounted filesystem or blank disk to use as $storage_root: plug in the disk, or wipe an old one with 'sudo wipefs -a /dev/sdX', and run setup again"
    fi
    if (( DRY_RUN )); then
        info "would ask which one becomes $storage_root (a blank disk is partitioned and formatted ext4):"
        printf '     %s  %s\n' "${items[@]}"
        return 1
    fi
    if [[ -n ${STORAGE_DISK:-} ]]; then
        choice=${STORAGE_DISK#/dev/}
        [[ -n ${kinds[$choice]:-} ]] || die "STORAGE_DISK=$STORAGE_DISK is not an unmounted filesystem or a blank disk"
    else
        menu choice "$default" "Which one is the storage disk ($storage_root)?" "${items[@]}"
    fi
    if [[ ${kinds[$choice]} == blank ]]; then
        format_blank_disk "/dev/$choice"
    else
        uuid=${ids[$choice]}
    fi
}

format_blank_disk() {  # format_blank_disk /dev/sdX: one GPT partition over it all, ext4
    local dev=$1 name=${1#/dev/} answer part opts=(-q -L storage -m 0)
    # lsblk goes by udev's view; ask the disk itself before writing to it
    if sudo blkid -p "$dev" >/dev/null 2>&1 || [[ -n $(lsblk -nro NAME "$dev" | sed 1d) ]]; then
        die "$dev has a partition table or filesystem after all; not touching it"
    fi
    info "$name ($(lsblk -dno SIZE,MODEL "$dev" | xargs)) gets one partition over the whole disk,"
    info "formatted ext4 (label 'storage', no space reserved for root: it's a data disk)"
    if [[ ${STORAGE_DISK:-} == "$name" || ${STORAGE_DISK:-} == "$dev" ]]; then
        info "STORAGE_DISK=$STORAGE_DISK: formatting it without asking"
    else
        ask answer "Type '$name' to format it, anything else to stop"
        [[ $answer == "$name" ]] || die "not formatting $dev; nothing was changed"
    fi
    printf 'label: gpt\n,,L\n' | sudo sfdisk --quiet "$dev"
    sudo udevadm settle
    part=$(lsblk -nrpo NAME,TYPE "$dev" | awk '$2 == "part" { print $1; exit }')
    [[ -n $part ]] || die "no partition on $dev after partitioning it"
    # From 256 GiB, one inode per MiB (mke2fs's "largefile"), not its default of one per 16-64
    # KiB: a media disk holds big files, and 12 TB would otherwise keep ~90 GB for inode tables.
    # Still millions of files' worth; below that size the default, or it would be too few.
    (( $(lsblk -bdno SIZE "$dev") < 256 << 30 )) || opts+=(-T largefile)
    info "formatting $part (a minute or two on a big disk)"
    sudo mkfs.ext4 "${opts[@]}" "$part"
    sudo udevadm settle
    uuid=$(sudo blkid -s UUID -o value "$part")
    [[ -n $uuid ]] || die "no UUID on $part after formatting it"
    fresh_fs=1
    info "formatted $part (UUID $uuid)"
}

### Media server choice

# Jellyfin's apt repo for this system: sets jf_url, jf_suite, jf_arch; fails if the repo has
# no packages for this release and architecture (or can't be reached).
jellyfin_repo() {
    local release archs
    jf_url=$JELLYFIN_REPO/${os_family/raspbian/debian} jf_suite=$os_suite
    jf_arch=$(dpkg --print-architecture)
    [[ -n $jf_suite ]] || return 1
    release=$(curl -fsS --max-time 30 "$jf_url/dists/$jf_suite/Release" 2>/dev/null) || return 1
    archs=$(sed -n 's/^Architectures://p' <<<"$release")
    [[ " $archs " == *" $jf_arch "* ]]
}

valid_media_server() {
    case $1 in
        minidlna|none) return 0 ;;
        jellyfin)
            jellyfin_repo && return 0
            info "Jellyfin has no packages for $jf_suite on $jf_arch ($jf_url), or the repo can't be reached"
            return 1 ;;
        *) info "choose jellyfin, minidlna or none"; return 1 ;;
    esac
}

choose_media_server() {
    local saved default mem_kb
    saved=$(ini "$config_src" setup media_server)
    if [[ -n $saved ]]; then
        media_server=$saved
        if [[ -n ${MEDIA_SERVER:-} && $MEDIA_SERVER != "$saved" ]]; then
            warn "ignoring MEDIA_SERVER=$MEDIA_SERVER: $CONFIG says $saved (change [setup] media_server there to switch)"
        fi
        case $saved in jellyfin|minidlna|none) ;;
            *) die "$CONFIG: [setup] media_server must be jellyfin, minidlna or none, not '$saved'" ;;
        esac
    elif [[ -n ${MEDIA_SERVER:-} ]]; then
        media_server=$MEDIA_SERVER
        valid_media_server "$media_server" || die "MEDIA_SERVER=$MEDIA_SERVER can't be used"
    elif pkg_installed jellyfin && ! pkg_installed minidlna; then
        media_server=jellyfin  # set up before media_server was an option: keep what's there
    elif pkg_installed minidlna && ! pkg_installed jellyfin; then
        media_server=minidlna
    else
        default=jellyfin
        mem_kb=$(awk '/^MemTotal:/ { print $2 }' /proc/meminfo)
        if ! jellyfin_repo || (( mem_kb < 3000000 )); then default=minidlna; fi
        step "Media server"
        if (( DRY_RUN )); then
            media_server=$default
            info "would ask: jellyfin, minidlna or none (default $default); assuming $default below"
        else
            while :; do
                menu media_server "$default" "Media server for the storage disk?" \
                    jellyfin "media library, apps for TVs/phones/browsers, transcodes (64-bit, a few GB RAM)" \
                    minidlna "lightweight DLNA: TVs and players browse the folders as they are" \
                    none "no media server"
                valid_media_server "$media_server" && break
            done
        fi
    fi
}

choose_samba() {
    local saved
    saved=$(ini "$config_src" setup samba)
    if [[ -n $saved ]]; then
        samba=$saved
        if [[ -n ${SAMBA:-} && $SAMBA != "$saved" ]]; then
            warn "ignoring SAMBA=$SAMBA: $CONFIG says $saved (change [setup] samba there to switch)"
        fi
    elif [[ -n ${SAMBA:-} ]]; then
        samba=$SAMBA
    elif (( DRY_RUN )); then
        samba=yes
        step "Samba share"
        info "would ask whether to share the storage disk with Samba (default yes); assuming yes below"
    else
        step "Samba share"
        info "a LAN-only share of the storage disk, to manage the files from Windows, Mac or phones"
        if ask_yes "Share the storage disk with Samba?"; then samba=yes; else samba=no; fi
    fi
    case $samba in yes|no) ;; *) die "samba must be yes or no, not '$samba'" ;; esac
}

# Asked also on an installed box (it came later).
choose_avahi() {
    local saved
    saved=$(ini "$config_src" setup avahi)
    if [[ -n $saved ]]; then
        avahi=$saved
        if [[ -n ${AVAHI:-} && $AVAHI != "$saved" ]]; then
            warn "ignoring AVAHI=$AVAHI: $CONFIG says $saved (change [setup] avahi there to switch)"
        fi
    elif [[ -n ${AVAHI:-} ]]; then
        avahi=$AVAHI
    elif (( DRY_RUN )); then
        avahi=yes
        step "LAN name"
        info "would ask whether to announce the box as $(hostname).local (default yes); assuming yes below"
    else
        step "LAN name"
        info "avahi (mDNS): phones and computers on the LAN find the box as $(hostname).local without"
        info "the router's help, and Macs and Linux file managers list the share by themselves"
        if ask_yes "Announce the box on the LAN?"; then avahi=yes; else avahi=no; fi
    fi
    case $avahi in yes|no) ;; *) die "avahi must be yes or no, not '$avahi'" ;; esac
}

choose_tailscale() {
    local saved
    saved=$(ini "$config_src" setup tailscale)
    if [[ -n $saved ]]; then
        tailscale=$saved
        if [[ -n ${TAILSCALE:-} && $TAILSCALE != "$saved" ]]; then
            warn "ignoring TAILSCALE=$TAILSCALE: $CONFIG says $saved (change [setup] tailscale there to switch)"
        fi
    elif [[ -n ${TAILSCALE:-} ]]; then
        tailscale=$TAILSCALE
    elif (( DRY_RUN )); then
        tailscale=yes
        step "Tailscale"
        info "would ask whether to set up Tailscale (default yes); assuming yes below"
    else
        step "Tailscale"
        info "remote access from your phone or laptop anywhere (Transmission, the media server,"
        info "the share), without opening ports on the router; needs a (free) Tailscale account"
        if ask_yes "Set up Tailscale?"; then tailscale=yes; else tailscale=no; fi
    fi
    case $tailscale in yes|no) ;; *) die "tailscale must be yes or no, not '$tailscale'" ;; esac
}

# Asked also on a box that already has the others (it came later).
choose_own_ntfy() {
    local saved default=no
    [[ $tailscale != yes ]] || default=yes
    saved=$(ini "$config_src" setup own_ntfy)
    if [[ -n $saved ]]; then
        own_ntfy=$saved
        if [[ -n ${OWN_NTFY:-} && $OWN_NTFY != "$saved" ]]; then
            warn "ignoring OWN_NTFY=$OWN_NTFY: $CONFIG says $saved (change [setup] own_ntfy there to switch)"
        fi
    elif [[ -n ${OWN_NTFY:-} ]]; then
        own_ntfy=$OWN_NTFY
    elif (( DRY_RUN )); then
        own_ntfy=$default
        step "Notifications"
        info "would ask whether to run an own ntfy server (default $default); assuming $default below"
    else
        step "Notifications"
        info "pushes go through ntfy: the public ntfy.sh, whose operator can read them (torrent"
        info "names, requests), or an ntfy server on this box that only the LAN and the tailnet"
        info "reach. With your own, the phone gets them while it's on the tailnet (Tailscale on)"
        info "or at home; what it missed comes when it's back (kept 3 days)."
        if [[ $default == yes ]]; then
            if ask_yes "Run your own ntfy server?"; then own_ntfy=yes; else own_ntfy=no; fi
        else
            info "(without Tailscale only at home)"
            if ask_no "Run your own ntfy server?"; then own_ntfy=yes; else own_ntfy=no; fi
        fi
    fi
    case $own_ntfy in yes|no) ;; *) die "own_ntfy must be yes or no, not '$own_ntfy'" ;; esac
}

choose_arr() {
    local saved
    saved=$(ini "$config_src" setup arr)
    if [[ -n $saved ]]; then
        arr=$saved
        if [[ -n ${ARR:-} && $ARR != "$saved" ]]; then
            warn "ignoring ARR=$ARR: $CONFIG says $saved (change [setup] arr there to switch)"
        fi
    elif [[ -n ${ARR:-} ]]; then
        arr=$ARR
    elif (( DRY_RUN )); then
        arr=no
        step "Search and requests"
        info "would ask whether to set up Prowlarr, Sonarr, Radarr and Seerr (default no); assuming no below"
    else
        step "Search and requests"
        info "Prowlarr, Sonarr, Radarr and Seerr (in Docker): search nCore, request movies and"
        info "series from your phone or the TV, and have them downloaded, renamed and filed"
        if ask_no "Set them up?"; then arr=yes; else arr=no; fi
    fi
    case $arr in yes|no) ;; *) die "arr must be yes or no, not '$arr'" ;; esac
}

# Asked also on a box that already has the others (it came later); left empty, and not saved,
# while arr = no, so switching arr on asks it then.
choose_whisparr() {
    local saved
    saved=$(ini "$config_src" setup whisparr)
    if [[ -n $saved ]]; then
        whisparr=$saved
        if [[ -n ${WHISPARR:-} && $WHISPARR != "$saved" ]]; then
            warn "ignoring WHISPARR=$WHISPARR: $CONFIG says $saved (change [setup] whisparr there to switch)"
        fi
    elif [[ $arr != yes ]]; then
        whisparr=""
        return
    elif [[ -n ${WHISPARR:-} ]]; then
        whisparr=$WHISPARR
    elif (( DRY_RUN )); then
        whisparr=no
        step "Whisparr"
        info "would ask whether to set up Whisparr (default no); assuming no below"
    else
        step "Whisparr"
        info "Whisparr (in Docker): the same for adult scenes and movies, from nCore's XXX"
        info "categories, into the xxx_hd / xxx_xvid folders; with Desireerr, its request app"
        info "(Seerr for StashDB scenes, github.com/vibesclauder/desireerr)"
        if ask_no "Set up Whisparr and Desireerr too?"; then whisparr=yes; else whisparr=no; fi
    fi
    case $whisparr in yes|no) ;; *) die "whisparr must be yes or no, not '$whisparr'" ;; esac
}

# Desireerr (Whisparr's request app) comes and goes with Whisparr.
desireerr_on() { [[ $arr == yes && $whisparr == yes ]]; }

valid_mbps() { [[ $1 =~ ^[1-9][0-9]{0,5}$ ]]; }

choose_download_speed() {
    local saved measured
    saved=$(ini "$config_src" setup download_mbps)
    if [[ -n $saved ]]; then
        download_mbps=$saved
        if [[ -n ${DOWNLOAD_MBPS:-} && $DOWNLOAD_MBPS != "$saved" ]]; then
            warn "ignoring DOWNLOAD_MBPS=$DOWNLOAD_MBPS: $CONFIG says $saved (change [setup] download_mbps there)"
        fi
    elif [[ -n ${DOWNLOAD_MBPS:-} ]]; then
        download_mbps=$DOWNLOAD_MBPS
    else
        step "Internet speed"
        info "Transmission's write cache and peer limits are sized for your download speed."
        info "measuring it (about 10 s, downloads from Hetzner's speed-test servers)..."
        if measured=$(helper speed-test); then
            info "measured: $measured Mbit/s (less than the plan if the box is on Wi-Fi or a"
            info "1 Gbit port, or the line is busy: then enter the plan's speed)"
        else
            measured=""
            info "the speed test failed; enter your plan's download speed"
        fi
        if (( DRY_RUN )); then
            download_mbps=${measured:-100}
            info "would ask for the speed (default ${measured:-none}); assuming $download_mbps below"
        else
            while :; do
                ask download_mbps "Internet download speed in Mbit/s" "$measured"
                valid_mbps "$download_mbps" && break
                info "enter a whole number of Mbit/s, e.g. 1000"
            done
        fi
    fi
    valid_mbps "$download_mbps" ||
        die "download_mbps must be a whole number of Mbit/s, not '$download_mbps'"
}

### 3. systemd drop-ins for transmission-daemon, the media server and Samba

install_dropins() {
    step "systemd drop-ins (start after the disk is mounted, restart on failure), AppArmor"
    local unit units=(transmission-daemon) changed=0
    [[ $media_server == none ]] || units+=("$media_server")  # unit name = package name
    [[ $samba != yes ]] || units+=(smbd)  # never let it write into an empty mount point
    for unit in "${units[@]}"; do
        helper render --strip-comments "$REPO/systemd/storage-override.conf" \
            "STORAGE_ROOT=$storage_root" >"$TMP/$unit-override.conf"
        if update_file "$TMP/$unit-override.conf" \
                "/etc/systemd/system/$unit.service.d/override.conf" 644 root; then
            changed=1
        fi
    done
    if (( changed )); then as_root systemctl daemon-reload; fi
    allow_storage_apparmor
}

# Ubuntu confines transmission-daemon with an AppArmor profile that has no access to the
# storage disk; its local include is the supported place to add some.
allow_storage_apparmor() {
    [[ -f /etc/apparmor.d/transmission ]] || return 0
    helper render "$REPO/templates/apparmor-transmission-daemon" \
        "STORAGE_ROOT=$storage_root" >"$TMP/apparmor-transmission-daemon"
    if update_file "$TMP/apparmor-transmission-daemon" \
            /etc/apparmor.d/local/transmission-daemon 644 root &&
       [[ $(cat /sys/module/apparmor/parameters/enabled 2>/dev/null) == Y ]]; then
        as_root apparmor_parser -r /etc/apparmor.d/transmission  # applies to the running daemon
    fi
}

### 4. Secrets

check_login() {  # check_login USER PASSWORD; sets login_rc (0 ok, 3 refused, 4 unreachable)
    if login_msg=$(RPC_USER=$1 RPC_PASSWORD=$2 helper rpc-check "$rpc_url"); then
        login_rc=0
    else
        login_rc=$?
    fi
    return "$login_rc"
}

ask_password() {  # ask_password VAR [PROMPT]: hidden input, asked twice
    local p1 p2
    need_terminal "${2:-Password}"
    while :; do
        read -r -s -p "   ${2:-Password}: " p1; echo
        read -r -s -p "   ${2:-Password} again: " p2; echo
        if [[ -z $p1 ]]; then info "the password can't be empty"
        elif [[ $p1 != "$p2" ]]; then info "the passwords don't match"
        elif [[ $p1 =~ ^[[:space:]] || $p1 =~ [[:space:]]$ ]]; then
            info "the password can't start or end with a space"
        else break
        fi
    done
    printf -v "$1" '%s' "$p1"
}

ask_new_login() {
    while :; do
        ask rpc_user "Transmission username (web UI, Remote GUI, rss2transmission)" "${1:-$USER}"
        [[ -n $rpc_user && $rpc_user != *:* && $rpc_user != *[[:space:]]* ]] && break
        info "the username can't be empty or contain ':' or spaces"
    done
    ask_password rpc_password
}

choose_login() {
    local known_user known_pass from=$SECRETS line
    known_user=$(ini "$SECRETS" secrets rpc_user)
    known_pass=$(ini "$SECRETS" secrets rpc_password)
    [[ $known_user != "<"*">" ]] || known_user=  # still the placeholder from secrets.example.ini
    if [[ (-z $known_user || -z $known_pass) && -r $LEGACY_AUTH ]]; then
        IFS= read -r line <"$LEGACY_AUTH" || true
        if [[ $line == ?*:?* ]]; then
            known_user=${line%%:*} known_pass=${line#*:} from=$LEGACY_AUTH
        fi
    fi

    if [[ -n ${RPC_USER:-} && -n ${RPC_PASSWORD:-} ]]; then
        rpc_user=$RPC_USER rpc_password=$RPC_PASSWORD
        info "Transmission login: user '$rpc_user' (from RPC_USER/RPC_PASSWORD)"
    elif [[ -n $known_user && -n $known_pass ]]; then
        if check_login "$known_user" "$known_pass"; then
            rpc_user=$known_user rpc_password=$known_pass
            if (( DRY_RUN )); then
                info "the login in $from (user '$known_user') works with Transmission; would offer to keep it"
            elif ! ask_yes "Keep the current Transmission login (user '$known_user', from $from)?"; then
                ask_new_login "$known_user"
            fi
        else
            info "the login in $from (user '$known_user') doesn't work with Transmission now: $login_msg"
            if (( DRY_RUN )); then
                rpc_user=$known_user rpc_password=$known_pass
                info "would ask for a username and password (setup then sets them in Transmission)"
            else
                info "choose the login; setup will set it in Transmission"
                ask_new_login "$known_user"
            fi
        fi
    elif (( DRY_RUN )); then
        rpc_user="" rpc_password=""
        info "would ask for a Transmission username and password"
    else
        info "choose a login for Transmission (web UI on :9091, Transmission Remote GUI, rss2transmission)"
        ask_new_login
    fi

    rpc_login_works=0
    if [[ -n $rpc_user ]] && check_login "$rpc_user" "$rpc_password"; then rpc_login_works=1; fi
}

# nCore login for Prowlarr's indexer (arr = yes): from secrets.ini, NCORE_USER/NCORE_PASSWORD,
# or asked. Appends to the caller's `updates`.
ncore_login() {
    ncore_user=$(ini "$SECRETS" secrets ncore_user)
    ncore_password=$(ini "$SECRETS" secrets ncore_password)
    [[ $ncore_user != "<"*">" ]] || ncore_user=  # placeholder from secrets.example.ini
    if [[ -n $ncore_user && -n $ncore_password ]]; then
        info "nCore login (for Prowlarr): already set (user '$ncore_user')"
        return
    fi
    if [[ -n ${NCORE_USER:-} && -n ${NCORE_PASSWORD:-} ]]; then
        ncore_user=$NCORE_USER ncore_password=$NCORE_PASSWORD
        info "nCore login (for Prowlarr): from NCORE_USER/NCORE_PASSWORD"
    elif (( DRY_RUN )); then
        info "would ask for your nCore username and password (for Prowlarr's nCore indexer)"
        return
    else
        info "your nCore login, for Prowlarr's nCore indexer (kept in $SECRETS)"
        ask ncore_user "nCore username"
        ask_password ncore_password "nCore password"
    fi
    updates+=("ncore_user=$ncore_user" "ncore_password=$ncore_password")
}

# StashDB API key for Desireerr (whisparr = yes): from secrets.ini, STASHDB_KEY, Whisparr's
# StashDB import list (a box set up before), or asked; Enter skips it (Desireerr's first
# visit then asks, or its Settings). Appends to the caller's `updates`.
stashdb_login() {
    local saved
    saved=$(ini "$SECRETS" secrets stashdb_key)
    [[ $saved != "<"*">" ]] || saved=""  # placeholder from secrets.example.ini
    if [[ -n ${STASHDB_KEY:-} ]]; then
        stashdb_key=$STASHDB_KEY
        if [[ $stashdb_key == "$saved" ]]; then
            info "StashDB key (for Desireerr): already set"
        else
            info "StashDB key (for Desireerr): from STASHDB_KEY"
            updates+=("stashdb_key=$stashdb_key")
        fi
    elif [[ -n $saved ]]; then
        stashdb_key=$saved
        info "StashDB key (for Desireerr): already set"
    elif stashdb_key=$(helper whisparr-stashdb-key --arr-dir "$ARR_DIR") && [[ -n $stashdb_key ]]; then
        info "StashDB key (for Desireerr): taken from Whisparr's StashDB import list"
        updates+=("stashdb_key=$stashdb_key")
    elif (( DRY_RUN )); then
        stashdb_key=""
        info "would ask for your StashDB API key (for Desireerr; Enter skips it)"
    elif [[ ! -t 0 ]]; then
        stashdb_key=""
        info "no StashDB key (for Desireerr): set STASHDB_KEY, or add it in Desireerr's Settings"
    else
        info "Desireerr browses StashDB with your API key: stashdb.org, your profile, API key"
        info "(kept in $SECRETS; Enter skips it, then Desireerr asks for it)"
        read -r -s -p "   StashDB API key: " stashdb_key; echo
        stashdb_key=${stashdb_key//[[:space:]]/}
        if [[ -n $stashdb_key ]]; then updates+=("stashdb_key=$stashdb_key"); else info "skipped"; fi
    fi
}

setup_secrets() {
    step "Secrets ($SECRETS)"
    local updates=() feed
    rpc_url=$(ini "$config_src" transmission rpc_url)
    ntfy_server=$(ini "$config_src" ntfy server)
    ntfy_server=${ntfy_server:-$NTFY_PUBLIC}
    ntfy_server=${ntfy_server%/}
    if [[ $own_ntfy == yes ]]; then
        ntfy_server=$NTFY_LOCAL
    elif [[ $ntfy_server == "$NTFY_LOCAL" ]]; then
        ntfy_server=$NTFY_PUBLIC  # own_ntfy was switched off
    fi

    feed=$(ini "$SECRETS" secrets feed_url)
    [[ $feed != *"<"* ]] || feed=""
    if [[ -n $feed ]]; then
        info "feed URL: already set"
    elif [[ -n ${FEED_URL:-} ]]; then
        updates+=("feed_url=$FEED_URL")
        info "feed URL: from FEED_URL"
    elif (( DRY_RUN )); then
        info "would ask for the nCore bookmark feed URL"
    else
        while :; do
            ask feed "nCore bookmark RSS feed URL (https://ncore.pro/bookmarks/...)"
            [[ $feed == http*://* ]] && break
            info "that doesn't look like a URL"
        done
        updates+=("feed_url=$feed")
    fi

    ntfy_topic=$(ini "$SECRETS" secrets ntfy_topic)
    [[ $ntfy_topic != *"<"* ]] || ntfy_topic=""
    if [[ -n $ntfy_topic ]]; then
        info "ntfy topic: already set"
    else
        ntfy_topic=$(helper random-topic)
        updates+=("ntfy_topic=$ntfy_topic")
        info "new ntfy topic: $ntfy_topic"
        if [[ $own_ntfy == yes ]]; then
            info "subscribe to it in the ntfy phone app (the server's address is in the summary at the end)"
        else
            info "subscribe to it in the ntfy phone app, or open $ntfy_server/$ntfy_topic"
        fi
        (( ! DRY_RUN )) || info "(a dry run generates a fresh topic each time; a real run saves it)"
    fi

    choose_login
    [[ $arr != yes ]] || ncore_login
    stashdb_key=""
    ! desireerr_on || stashdb_login
    if [[ -n $rpc_user ]]; then
        [[ $rpc_user == "$(ini "$SECRETS" secrets rpc_user)" ]] || updates+=("rpc_user=$rpc_user")
        [[ $rpc_password == "$(ini "$SECRETS" secrets rpc_password)" ]] ||
            updates+=("rpc_password=$rpc_password")
    fi

    if (( ${#updates[@]} == 0 )); then
        info "secrets.ini is complete"
    elif (( DRY_RUN )); then
        info "would set in $SECRETS (mode 600): $(printf '%s\n' "${updates[@]}" | cut -d= -f1 | tr '\n' ' ')"
    else
        mkdir -p -m 700 "$CONF_DIR"
        printf '%s\n' "${updates[@]}" | helper ini-set "$SECRETS" secrets
        info "saved: $(printf '%s\n' "${updates[@]}" | cut -d= -f1 | tr '\n' ' ')"
    fi
    if [[ -f $SECRETS && $(stat -c %a "$SECRETS") != 600 ]]; then run chmod 600 "$SECRETS"; fi
}

### 5. Config and download folders

setup_config() {
    step "Config ($CONFIG)"
    if [[ -f $CONFIG ]]; then
        info "exists, leaving it as it is"
    else
        [[ -d $CONF_DIR ]] || run mkdir -p -m 700 "$CONF_DIR"
        if (( DRY_RUN )); then
            info "would create it from config.example.ini (storage_root = $storage_root)"
        else
            install -m 644 "$config_src" "$CONFIG"
            info "created from config.example.ini (storage_root = $storage_root)"
        fi
    fi
    # Remember the choices, so re-runs don't ask again.
    local key updates=()
    for key in media_server samba avahi tailscale own_ntfy arr whisparr download_mbps; do
        [[ $(ini "$CONFIG" setup "$key") == "${!key}" ]] || updates+=("$key=${!key}")
    done
    if (( ${#updates[@]} == 0 )); then
        :
    elif (( DRY_RUN )); then
        info "would set in [setup]: ${updates[*]}"
    else
        printf '%s\n' "${updates[@]}" | helper ini-set "$CONFIG" setup
        info "saved in [setup]: ${updates[*]}"
    fi
    # rss2transmission and the start page push to [ntfy] server: kept in step with own_ntfy
    local current
    current=$(ini "$CONFIG" ntfy server)
    if [[ ${current%/} != "$ntfy_server" ]]; then
        if (( DRY_RUN )); then
            info "would set in [ntfy]: server=$ntfy_server"
        else
            helper ini-set "$CONFIG" ntfy <<<"server=$ntfy_server"
            info "saved in [ntfy]: server=$ntfy_server"
        fi
    fi
    make_folders
}

make_folders() {
    step "Download folders under $storage_root"
    local out dirs d fstype posix=1 group mode created=0 fixed=0
    out=$(helper route-dirs "$config_src")
    mapfile -t dirs <<<"$out"
    dirs+=("$storage_root/.incomplete")  # Transmission's incomplete-dir (step 6)
    if [[ $arr == yes ]]; then  # Sonarr's and Radarr's downloads, before they're imported
        dirs+=("$storage_root/downloads" "$storage_root/downloads/series" "$storage_root/downloads/movies")
        [[ $whisparr != yes ]] || dirs+=("$storage_root/downloads/xxx")
    fi
    fstype=$(findmnt -no FSTYPE --target "$storage_root" 2>/dev/null || true)
    case $fstype in fuseblk|ntfs|ntfs3|vfat|exfat|msdos) posix=0 ;; esac

    if (( ! posix )); then
        # No Unix permissions on this filesystem (chown/chmod do nothing): just the folders.
        info "$fstype filesystem: no owners/permissions to set, only creating missing folders"
        for d in "${dirs[@]}"; do
            [[ ! -d $d ]] || continue
            if [[ -w $(dirname "$d") ]]; then run mkdir "$d"; else as_root mkdir "$d"; fi
            created=$((created + 1))
        done
    else
        # Transmission writes as debian-transmission, the media server reads as "other", you manage
        # the files: owner you, group debian-transmission, setgid so new files inherit the
        # group, and Transmission's umask 002 keeps them group-writable and world-readable.
        for d in "${dirs[@]}"; do
            if [[ ! -d $d ]]; then
                as_root install -d -o "$USER" -g debian-transmission -m 2775 "$d"
                created=$((created + 1))
                continue
            fi
            read -r group mode <<<"$(stat -c '%G %a' "$d")"
            if [[ $group != debian-transmission ]] || (( (8#$mode & 8#2075) != 8#2075 )); then
                as_root chgrp debian-transmission "$d"
                as_root chmod g+rwxs,o+rx "$d"
                fixed=$((fixed + 1))
            fi
        done
        if [[ " $(id -nG "$USER") " != *" debian-transmission "* ]]; then
            as_root usermod -aG debian-transmission "$USER"
            (( DRY_RUN )) ||
                info "added $USER to the debian-transmission group (applies from your next login)"
        fi
    fi
    if (( created + fixed == 0 )); then
        info "all ${#dirs[@]} folders exist"
    elif (( DRY_RUN )); then
        info "${#dirs[@]} folders: $created to create, $fixed to fix permissions on"
    else
        info "${#dirs[@]} folders: $created created, $fixed with permissions fixed"
    fi
}

### 6. Transmission

merge_settings() {  # merge_settings CURRENT OUT: prints the keys that change
    local keep=()
    (( ! rpc_login_works )) || keep=(--keep-password)
    RPC_PASSWORD=$rpc_password helper merge-settings \
        --template "$REPO/templates/transmission-settings.json" --current "$1" --out "$2" \
        --var "RPC_USER=$rpc_user" --var "DOWNLOAD_DIR=$download_dir" \
        --var "INCOMPLETE_DIR=$storage_root/.incomplete" \
        --var "RPC_WHITELIST=$(rpc_whitelist)" --var "HOSTNAME=$(hostname)" \
        --download-mbps "$download_mbps" "${keep[@]}"
}

rpc_whitelist() {  # LAN, plus the tailnet: Transmission only knows whole-octet wildcards, so
    # 100.64.0.0/10 is 100.64.*.* .. 100.127.*.* (100.*.*.* would let in public addresses too)
    printf '%s' "$lan_whitelist"
    [[ $tailscale != yes ]] || printf ',100.%s.*.*' {64..127}
}

read_tr_settings() {  # root-only file -> $TMP/settings.current.json (empty if there's none)
    if sudo test -f "$TR_SETTINGS"; then
        sudo cat "$TR_SETTINGS" >"$TMP/settings.current.json"
    else
        : >"$TMP/settings.current.json"
    fi
}

wait_for_rpc() {
    local _
    for _ in $(seq 30); do
        check_login "$rpc_user" "$rpc_password" && return 0
        (( login_rc != 3 )) || break  # Transmission answered and said no: waiting won't help
        sleep 1
    done
    die "Transmission doesn't accept the login after the restart ($login_msg). Check: sudo journalctl -u transmission-daemon; $TR_SETTINGS"
}

setup_transmission() {
    step "Transmission"
    local changed keys misc
    # Default folder for torrents added by hand (web UI, Remote GUI) without picking one:
    # the same catch-all folder rss2transmission uses for unknown categories.
    misc=$(ini "$config_src" routes misc_dir)
    download_dir=$storage_root/${misc:-MISC}
    keys=$(grep -o '"[a-z-]*":' "$REPO/templates/transmission-settings.json" | tr -d '":' | tr '\n' ' ')
    keys+="cache-size-mb peer-limit-global peer-limit-per-torrent"
    if (( DRY_RUN )); then
        info "$TR_SETTINGS is only readable by root, so a dry run can't compare it."
        info "would merge these keys into it (stop, merge, start; only if any differ): $keys"
        info "  rpc-whitelist = 127.0.0.1,$(rpc_whitelist)   download-dir = $download_dir"
        info "  incomplete-dir = $storage_root/.incomplete   cache and peer limits for $download_mbps Mbit/s"
        if (( rpc_login_works )); then
            info "Transmission accepts the login now"
        else
            info "would verify the login with an RPC call after the restart"
        fi
        return
    fi

    read_tr_settings
    changed=$(merge_settings "$TMP/settings.current.json" "$TMP/settings.new.json")
    if [[ -z $changed ]]; then
        info "settings.json already has our settings"
    else
        info "updating settings.json: $(tr '\n' ' ' <<<"$changed")"
        # Transmission writes its settings back when it stops, so stop first, then merge
        # into what it just saved.
        sudo systemctl stop transmission-daemon
        if ! { read_tr_settings &&
               merge_settings "$TMP/settings.current.json" "$TMP/settings.new.json" >/dev/null &&
               sudo install -m 600 -o debian-transmission -g debian-transmission \
                   "$TMP/settings.new.json" "$TR_SETTINGS"; }; then
            sudo systemctl start transmission-daemon
            die "could not update $TR_SETTINGS; Transmission was restarted unchanged"
        fi
        rm -f "$TMP/settings.current.json" "$TMP/settings.new.json"  # contain the password
    fi
    systemctl is-enabled -q transmission-daemon || sudo systemctl enable transmission-daemon
    systemctl is-active -q transmission-daemon || sudo systemctl start transmission-daemon
    wait_for_rpc
    info "Transmission is running and accepts the login (user '$rpc_user')"
}

### 7. Media server

setup_media_server() {
    case $media_server in
        jellyfin) setup_jellyfin ;;
        minidlna) setup_minidlna ;;
        none) step "Media server"; info "none ([setup] media_server = none in $CONFIG)" ;;
    esac
    stop_other_media
}

# Only the chosen one runs (both would want the DLNA port): after switching, the other is
# stopped and disabled, but stays installed with its library.
stop_other_media() {
    local unit
    for unit in jellyfin minidlna; do
        [[ $unit != "$media_server" ]] || continue
        if systemctl is-enabled -q "$unit" 2>/dev/null || systemctl is-active -q "$unit" 2>/dev/null; then
            info "media server is $media_server: stopping and disabling $unit (still installed; 'sudo apt remove $unit' removes it)"
            as_root systemctl disable --now "$unit"
        fi
    done
}

setup_jellyfin() {
    step "Jellyfin"
    local changed=0 restart=0 dev group have
    # Jellyfin's own apt repo, as their install script sets it up (key kept armored: .asc)
    jellyfin_repo ||
        pkg_installed jellyfin ||
        die "Jellyfin has no packages for $jf_suite on $jf_arch ($jf_url), or the repo can't be reached; set [setup] media_server = minidlna in $CONFIG"
    if curl -fsSL --max-time 30 "$JELLYFIN_REPO/jellyfin_team.gpg.key" -o "$TMP/jellyfin.asc"; then
        if update_file "$TMP/jellyfin.asc" "$JELLYFIN_KEY" 644 root; then changed=1; fi
    elif [[ ! -f $JELLYFIN_KEY ]]; then
        die "could not download Jellyfin's signing key from $JELLYFIN_REPO"
    fi
    helper render "$REPO/templates/jellyfin.sources" "URL=$jf_url" "SUITE=$jf_suite" \
        "ARCH=$jf_arch" "KEY=$JELLYFIN_KEY" >"$TMP/jellyfin.sources"
    if update_file "$TMP/jellyfin.sources" "$JELLYFIN_SOURCES" 644 root; then changed=1; fi
    if (( changed )); then APT_UPDATED=0; fi  # new repo: apt has to fetch its package list
    apt_install jellyfin

    # Hardware transcoding (VAAPI / Intel QSV) needs the GPU's render node(s).
    have=" $(id -nG jellyfin 2>/dev/null || true) "
    for dev in /dev/dri/renderD*; do
        [[ -e $dev ]] || continue
        group=$(stat -c %G "$dev")
        if [[ $have != *" $group "* ]]; then
            as_root usermod -aG "$group" jellyfin
            have+="$group "
            restart=1
        fi
    done
    if ! compgen -G '/dev/dri/renderD*' >/dev/null; then
        info "no GPU render node (/dev/dri/renderD*): transcoding will use the CPU only"
    fi

    systemctl is-enabled -q jellyfin 2>/dev/null || as_root systemctl enable jellyfin
    if (( restart )) && systemctl is-active -q jellyfin 2>/dev/null; then
        as_root systemctl restart jellyfin  # pick up the new group
    elif ! systemctl is-active -q jellyfin 2>/dev/null; then
        as_root systemctl start jellyfin
    fi

    # First-run wizard (a fresh install only): admin = the Transmission login, the libraries
    # from [routes] (README's layout). A Jellyfin that's set up is left alone.
    if (( DRY_RUN )); then
        info "would run Jellyfin's first-run wizard if it hasn't been (admin '$rpc_user', libraries from [routes])"
    elif ! JF_USER=$rpc_user JF_PASSWORD=$rpc_password helper jellyfin-setup "$config_src" |
            sed 's/^/   /'; then
        warn "Jellyfin's first-run setup failed; finish it at http://${lan_ip:-<this box>}:8096/"
    fi
    jellyfin_hwaccel
}

# Hardware transcoding: VAAPI on an AMD or Intel GPU, with the codecs it decodes (asked with
# vainfo, as the jellyfin user). Only while Jellyfin's acceleration is off: a choice made in
# its dashboard stays. NVIDIA needs its own driver: left to you (README).
jellyfin_hwaccel() {
    local dev="" d vendor jf_user=${JELLYFIN_USER:-$rpc_user} jf_password=${JELLYFIN_PASSWORD:-$rpc_password}
    for d in /dev/dri/renderD*; do
        [[ -e $d ]] || continue
        vendor=$(cat "/sys/class/drm/${d##*/}/device/vendor" 2>/dev/null || true)
        case $vendor in 0x1002|0x8086) dev=$d; break ;; esac  # AMD, Intel
    done
    if [[ -z $dev ]]; then
        info "transcoding: no AMD/Intel GPU found, Jellyfin uses the CPU"
        return
    elif [[ ! -x $JF_VAINFO ]]; then
        info "transcoding: no $JF_VAINFO (jellyfin-ffmpeg) to ask the GPU; left as is"
        return
    elif (( DRY_RUN )); then
        info "would switch on hardware transcoding (VAAPI, $dev) if it's off in Jellyfin"
        return
    fi
    if ! sudo -u jellyfin "$JF_VAINFO" --display drm --device "$dev" >"$TMP/vainfo" 2>/dev/null; then
        warn "transcoding: the GPU ($dev) doesn't answer through VAAPI; Jellyfin stays on the CPU"
        return
    fi
    if ! JF_USER=$jf_user JF_PASSWORD=$jf_password helper jellyfin-hwaccel "$dev" \
            <"$TMP/vainfo" | sed 's/^/   /'; then
        warn "transcoding: not switched on (above); Jellyfin: Dashboard -> Playback -> Transcoding -> VAAPI, $dev"
    fi
}

setup_minidlna() {
    step "minidlna"
    local new=$TMP/minidlna.conf old_media new_media
    apt_install minidlna
    helper render "$REPO/templates/minidlna.conf" \
        "STORAGE_ROOT=$storage_root" "HOSTNAME=$(hostname)" >"$new"
    old_media=$(grep '^media_dir=' "$MINIDLNA_CONF" 2>/dev/null || true)
    new_media=$(grep '^media_dir=' "$new")
    if [[ -f $MINIDLNA_CONF && ! -e $MINIDLNA_CONF.orig ]] && ! cmp -s "$new" "$MINIDLNA_CONF"; then
        as_root cp -p "$MINIDLNA_CONF" "$MINIDLNA_CONF.orig"
    fi
    if update_file "$new" "$MINIDLNA_CONF" 644 root; then
        if [[ $old_media != "$new_media" ]]; then
            info "media folders changed: rebuilding the library (can take a while on a big disk)"
            as_root systemctl stop minidlna
            as_root rm -f "$MINIDLNA_DB"
            as_root systemctl start minidlna
        else
            as_root systemctl restart minidlna
        fi
    fi
    systemctl is-enabled -q minidlna 2>/dev/null || as_root systemctl enable minidlna
    systemctl is-active -q minidlna 2>/dev/null || as_root systemctl start minidlna
}

### 8. Samba share

setup_samba() {
    step "Samba share"
    if [[ $samba != yes ]]; then
        remove_samba_share
        return
    fi
    local pkgs=(samba) changed=0 password
    # wsdd makes the box show up in Windows' Network view (\\host\storage works without it).
    # Newer releases split it: wsdd is only the client there, wsdd-server runs the service.
    if apt-cache show wsdd-server >/dev/null 2>&1; then
        pkgs+=(wsdd-server)
    elif apt-cache show wsdd >/dev/null 2>&1; then
        pkgs+=(wsdd)
    fi
    apt_install "${pkgs[@]}"
    local wsdd
    wsdd=$(wsdd_unit)
    if [[ -n $wsdd ]] && ! systemctl is-enabled -q "$wsdd" 2>/dev/null; then
        as_root systemctl enable --now "$wsdd"
    fi

    local allow="127.0.0.1 ::1 $lan_net fe80::/10"
    [[ $tailscale != yes ]] || allow+=" $TAILNET_V4 $TAILNET_V6"
    helper render "$REPO/templates/samba.conf" "STORAGE_ROOT=$storage_root" "USER=$USER" \
        "HOSTS_ALLOW=$allow" >"$TMP/samba.conf"
    if update_file "$TMP/samba.conf" "$SAMBA_SHARE" 644 root; then changed=1; fi
    # smb.conf stays the distribution's; it only gets one include line for our file.
    if ! grep -qxF "include = $SAMBA_SHARE" "$SMB_CONF" 2>/dev/null; then
        if (( DRY_RUN )); then
            info "would add to $SMB_CONF: include = $SAMBA_SHARE"
        else
            [[ -e $SMB_CONF.before-torrent-box ]] || sudo cp -p "$SMB_CONF" "$SMB_CONF.before-torrent-box"
            printf '\n# torrent-box: share of the storage disk (setup.sh)\ninclude = %s\n' \
                "$SAMBA_SHARE" | sudo tee -a "$SMB_CONF" >/dev/null
            info "added to $SMB_CONF: include = $SAMBA_SHARE"
        fi
        changed=1
    fi
    if (( ! DRY_RUN )); then
        testparm -s "$SMB_CONF" >/dev/null 2>"$TMP/testparm.err" ||
            die "Samba rejects the configuration: $(tail -n 3 "$TMP/testparm.err")"
    fi

    # Samba keeps its own passwords: give this user one if it has none yet.
    if (( DRY_RUN )); then
        info "would ask for a Samba password for $USER, unless it already has one"
    elif sudo pdbedit -u "$USER" >/dev/null 2>&1; then
        info "Samba user $USER exists (to change its password: sudo smbpasswd $USER)"
    else
        if [[ -n ${SAMBA_PASSWORD:-} ]]; then
            password=$SAMBA_PASSWORD
        else
            info "choose the password for the share (user $USER)"
            ask_password password "Samba password"
        fi
        printf '%s\n%s\n' "$password" "$password" | sudo smbpasswd -s -a "$USER" >/dev/null
        info "Samba user $USER added"
    fi

    systemctl is-enabled -q smbd 2>/dev/null || as_root systemctl enable smbd
    if (( changed )) && systemctl is-active -q smbd 2>/dev/null; then
        as_root systemctl restart smbd
    elif ! systemctl is-active -q smbd 2>/dev/null; then
        as_root systemctl start smbd
    fi
}

wsdd_unit() {  # the wsdd service's name: wsdd-server.service where the package is split
    local unit
    for unit in wsdd-server wsdd; do
        if systemctl cat "$unit.service" >/dev/null 2>&1; then echo "$unit"; return; fi
    done
}

# samba = no after it was yes: take our share out again. Samba stays installed (it may serve
# other shares), and so do its users.
remove_samba_share() {
    local dropin=/etc/systemd/system/smbd.service.d/override.conf
    if [[ ! -f $SAMBA_SHARE ]] && ! grep -qxF "include = $SAMBA_SHARE" "$SMB_CONF" 2>/dev/null; then
        info "not shared ([setup] samba = no in $CONFIG)"
        return
    fi
    info "samba = no: removing the storage share (Samba itself stays installed)"
    as_root sed -i -e "\|^# torrent-box: share of the storage disk|d" \
        -e "\|^include = $SAMBA_SHARE\$|d" "$SMB_CONF"
    as_root rm -f "$SAMBA_SHARE"
    if [[ -f $dropin ]]; then
        as_root rm -f "$dropin"
        as_root systemctl daemon-reload
    fi
    if systemctl is-active -q smbd 2>/dev/null; then as_root systemctl restart smbd; fi
}

# avahi answers <hostname>.local by itself; the service file adds the start page and the
# share, which Finder and Linux file managers list under Network.
setup_avahi() {
    step "LAN name (avahi)"
    if [[ $avahi != yes ]]; then
        if [[ -f $AVAHI_SERVICE ]]; then
            info "avahi = no: no longer announcing the start page and the share (avahi stays installed)"
            as_root rm -f "$AVAHI_SERVICE"
        else
            info "not announced ([setup] avahi = no in $CONFIG)"
        fi
        return
    fi
    apt_install avahi-daemon
    {
        printf '%s\n' '<?xml version="1.0" standalone="no"?>' \
            '<!DOCTYPE service-group SYSTEM "avahi-service.dtd">' \
            '<!-- torrent-box: written by setup.sh; re-running it overwrites this file -->' \
            '<service-group>' \
            '  <name replace-wildcards="yes">%h</name>' \
            "  <service><type>_http._tcp</type><port>$DASHBOARD_PORT</port></service>"
        [[ $samba != yes ]] || printf '%s\n' '  <service><type>_smb._tcp</type><port>445</port></service>'
        printf '%s\n' '</service-group>'
    } >"$TMP/avahi.service"
    # avahi-daemon notices changes in its services folder by itself
    update_file "$TMP/avahi.service" "$AVAHI_SERVICE" 644 root || true
    { systemctl is-enabled -q avahi-daemon && systemctl is-active -q avahi-daemon; } 2>/dev/null ||
        as_root systemctl enable --now avahi-daemon
}

### 9. Tailscale

tailscale_status() {  # sets ts_state ("" if not installed/running), ts_name, ts_ip
    local out=""
    if command -v tailscale >/dev/null; then
        out=$(tailscale status --json 2>/dev/null || true)
    fi
    read -r ts_state ts_name ts_ip <<<"$(helper tailscale-status <<<"$out")"
    [[ $ts_state != - ]] || ts_state=""
}

setup_tailscale() {
    step "Tailscale"
    if [[ $tailscale != yes ]]; then
        if pkg_installed tailscale; then
            info "tailscale = no: torrent-box doesn't open its services to the tailnet (Tailscale itself is left as it is)"
        else
            info "not used ([setup] tailscale = no in $CONFIG)"
        fi
        return
    fi
    # Tailscale's own apt repo, exactly as their install script sets it up.
    local changed=0 url=$TAILSCALE_REPO/$os_family/$os_suite
    if curl -fsSL --max-time 30 "$url.noarmor.gpg" -o "$TMP/tailscale.gpg" &&
       curl -fsSL --max-time 30 "$url.tailscale-keyring.list" -o "$TMP/tailscale.list"; then
        if update_file "$TMP/tailscale.gpg" "$TAILSCALE_KEY" 644 root; then changed=1; fi
        if update_file "$TMP/tailscale.list" "$TAILSCALE_SOURCES" 644 root; then changed=1; fi
    elif ! pkg_installed tailscale; then
        die "Tailscale has no packages for $os_family $os_suite ($url.*), or the repo can't be reached; set [setup] tailscale = no in $CONFIG"
    fi
    if (( changed )); then APT_UPDATED=0; fi
    apt_install tailscale
    systemctl is-enabled -q tailscaled 2>/dev/null || as_root systemctl enable tailscaled
    systemctl is-active -q tailscaled 2>/dev/null || as_root systemctl start tailscaled

    # Join the tailnet once. Later runs leave it alone (also when it was stopped on purpose).
    tailscale_status
    case $ts_state in
        Running)
            info "connected: $ts_name ($ts_ip)" ;;
        Stopped)
            info "Tailscale is stopped ('tailscale down'); 'sudo tailscale up' reconnects it" ;;
        *)
            if (( DRY_RUN )); then
                info "would run: sudo tailscale up (log in with the link it prints, or TS_AUTHKEY)"
                return
            fi
            if [[ -n ${TS_AUTHKEY:-} ]]; then
                # from a file, so the key isn't on a command line
                (umask 077; printf '%s\n' "$TS_AUTHKEY" >"$TMP/ts-authkey")
                sudo tailscale up --auth-key="file:$TMP/ts-authkey"
                rm -f "$TMP/ts-authkey"
            else
                info "open the link below and log in to add this box to your tailnet"
                sudo tailscale up
            fi
            tailscale_status
            [[ $ts_state == Running ]] || die "Tailscale isn't connected after 'tailscale up' (state: ${ts_state:-unknown})"
            info "connected: $ts_name ($ts_ip)"
            info "tip: turn off key expiry for this box in the Tailscale admin console (Machines)" ;;
    esac
}

### 9a. Own ntfy server

setup_ntfy() {
    step "ntfy server"
    if [[ $own_ntfy != yes ]]; then
        if [[ -f $NTFY_DROPIN ]] && systemctl is-enabled -q ntfy 2>/dev/null; then
            as_root systemctl disable --now ntfy
            info "own_ntfy = no: our ntfy server is stopped (the package stays); pushes go to $ntfy_server"
        else
            info "not used ([setup] own_ntfy = no in $CONFIG): pushes go to $ntfy_server"
        fi
        return
    fi
    # Our settings in a drop-in, in place before the package (whose unit isn't enabled on
    # install), so it never starts on port 80.
    local changed=0 _
    helper render --strip-comments "$REPO/systemd/ntfy-torrent-box.conf" "PORT=$NTFY_PORT" \
        >"$TMP/ntfy-torrent-box.conf"
    if update_file "$TMP/ntfy-torrent-box.conf" "$NTFY_DROPIN" 644 root; then
        changed=1
        as_root systemctl daemon-reload
    fi
    ntfy_repo
    apt_install ntfy ||
        die "couldn't install ntfy; set [setup] own_ntfy = no in $CONFIG to use ntfy.sh"
    # Installed before from the distro: apt prefers ntfy's own now (the pin), so switch to it.
    local have want
    have=$(dpkg-query -W -f='${Version}' ntfy 2>/dev/null || true)
    want=$(LC_ALL=C apt-cache policy ntfy 2>/dev/null | awk '/Candidate:/ { print $2 }')
    if [[ -n $have && -n $want && $want != "(none)" && $want != "$have" ]]; then
        info "ntfy $have -> $want (ntfy's own package, with the web app)"
        as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
            -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold ntfy
        # The distro's ran as _ntfy, ntfy's own runs as ntfy: the message cache comes along.
        if id -u ntfy >/dev/null 2>&1; then as_root chown -R ntfy:ntfy /var/cache/ntfy; fi
        changed=1
    fi
    systemctl is-enabled -q ntfy 2>/dev/null || as_root systemctl enable ntfy
    if ! systemctl is-active -q ntfy 2>/dev/null; then
        as_root systemctl start ntfy
    elif (( changed )); then
        as_root systemctl restart ntfy
    fi
    (( ! DRY_RUN )) || return 0
    for _ in $(seq 20); do
        curl -fsS --max-time 2 -o /dev/null "$NTFY_LOCAL/v1/health" 2>/dev/null && break
        sleep 0.5
    done
    curl -fsS --max-time 5 -o /dev/null "$NTFY_LOCAL/v1/health" ||
        die "the ntfy server doesn't answer on port $NTFY_PORT. Check: sudo journalctl -u ntfy"
    info "running on port $NTFY_PORT (ufw: the LAN and the tailnet only)"
}

# ntfy's own apt repo, as its docs set it up, with the key checked against the fingerprint
# they give. Where it has no packages, the distro's ntfy stays (without the web app).
ntfy_repo() {
    local arch changed=0 fp
    arch=$(dpkg --print-architecture)
    case $arch in
        amd64|arm64|armhf) ;;
        *) warn "ntfy has no packages of its own for $arch: the distro's, without the web app"
           return ;;
    esac
    apt_install gpg
    if curl -fsSL --max-time 30 "$NTFY_REPO/keyring.gpg" -o "$TMP/ntfy.gpg"; then
        fp=$(gpg --show-keys --with-colons "$TMP/ntfy.gpg" 2>/dev/null | awk -F: '$1 == "fpr" { print $10; exit }')
        [[ $fp == "$NTFY_KEY_FP" ]] ||
            die "ntfy's signing key from $NTFY_REPO has fingerprint ${fp:-?}, not $NTFY_KEY_FP as its docs say"
        if update_file "$TMP/ntfy.gpg" "$NTFY_KEY" 644 root; then changed=1; fi
    elif [[ ! -f $NTFY_KEY ]]; then
        warn "could not download ntfy's signing key from $NTFY_REPO: the distro's ntfy, without the web app"
        return
    fi
    helper render "$REPO/templates/ntfy.sources" "URL=$NTFY_REPO" "ARCH=$arch" "KEY=$NTFY_KEY" \
        >"$TMP/ntfy.sources"
    if update_file "$TMP/ntfy.sources" "$NTFY_SOURCES" 644 root; then changed=1; fi
    if update_file "$REPO/templates/ntfy.pref" "$NTFY_PIN" 644 root; then changed=1; fi
    if (( changed )); then  # new repo: apt has to fetch its package list (also for the switch)
        as_root apt-get update
        APT_UPDATED=1
    fi
}

### 9b. Disk health

# The root jobs' way to push to your ntfy topic (it holds the topic: root-only).
install_push() {
    [[ -n ${ntfy_topic:-} ]] || return 0
    helper render "$REPO/templates/push" "NTFY_URL=$ntfy_server/$ntfy_topic" >"$TMP/push"
    update_file "$TMP/push" "$LIB/push" 700 root || true
}

storage_disk() {  # sets disk_dev (stable name of the storage root's disk, "" if none), disk_opts
    local src parent id
    disk_dev="" disk_opts=""
    src=$(findmnt -no SOURCE "$storage_root" 2>/dev/null || true)  # only if it's a mount point
    [[ $src == /dev/* ]] || return 0
    parent=$(lsblk -no PKNAME "$src" 2>/dev/null | head -n 1 || true)
    [[ -n $parent ]] || parent=${src#/dev/}  # a filesystem on the whole disk
    for id in /dev/disk/by-id/*; do
        [[ $id != *-part* && $(readlink -f "$id") == "/dev/$parent" ]] || continue
        case ${id##*/} in ata-*|scsi-*|usb-*|nvme-*) disk_dev=$id; break ;; esac
    done
    [[ -n $disk_dev ]] || disk_dev=/dev/$parent
    # USB enclosures mostly pass SMART through as SAT
    [[ $(lsblk -dno TRAN "/dev/$parent" 2>/dev/null || true) != usb ]] || disk_opts="-d sat "
}

# smartd checks the storage disk every 30 min, runs a short self-test daily and a long one
# monthly, and pushes (urgent) when the disk starts failing or runs hot. Our line goes before
# smartd.conf's DEVICESCAN, which keeps watching the other disks (and skips this one).
setup_disk_health() {
    step "Disk health (smartd)"
    local changed=0 line
    apt_install smartmontools
    install_push
    if update_file "$REPO/templates/smartd-notify" "$LIB/smartd-notify" 755 root; then changed=1; fi
    storage_disk
    if [[ -z $disk_dev ]]; then
        info "$storage_root is no disk of its own (not mounted?); smartd watches the disks it finds"
    else
        # -n standby,24,q: let a sleeping disk sleep, but check at least every 12 h;
        # -s: short test daily 03:00, long test on the 1st at 04:00; -W: push at 55 °C
        line="$disk_dev ${disk_opts}-a -o on -S on -n standby,24,q -s (S/../.././03|L/../01/./04)"
        line+=" -W 4,50,55 -m <nomailer> -M daily -M exec $LIB/smartd-notify"
        { cat "$SMARTD_CONF" 2>/dev/null || true; } | helper smartd-conf "$line" >"$TMP/smartd.conf"
        if [[ -f $SMARTD_CONF && ! -e $SMARTD_CONF.before-torrent-box ]] && (( ! DRY_RUN )); then
            sudo cp -p "$SMARTD_CONF" "$SMARTD_CONF.before-torrent-box"
        fi
        if update_file "$TMP/smartd.conf" "$SMARTD_CONF" 644 root; then changed=1; fi
        info "watching $disk_dev"
    fi
    # smartmontools.service (alias smartd); it doesn't start without a disk that has SMART
    systemctl is-enabled -q smartmontools 2>/dev/null || as_root systemctl enable smartmontools
    if (( changed )) && systemctl is-active -q smartmontools 2>/dev/null; then
        as_root systemctl restart smartmontools
    elif ! systemctl is-active -q smartmontools 2>/dev/null; then
        as_root systemctl start smartmontools
    fi
}

### 10. Prowlarr, Sonarr, Radarr and Seerr

route_dir() {  # route_dir CODE: the folder for an nCore category, if it's routed
    local rel
    rel=$(ini "$config_src" routes "$1")
    [[ -z $rel ]] || printf '%s/%s\n' "$storage_root" "${rel#/}"
}

setup_arr() {
    if [[ $arr != yes ]]; then
        # Switched off: stop the containers; their config stays for switching back on.
        if [[ -f $ARR_DIR/compose.yml ]]; then
            step "Prowlarr, Sonarr, Radarr and Seerr"
            info "[setup] arr = no: stopping the containers (config kept in $ARR_DIR)"
            as_root docker compose -f "$ARR_DIR/compose.yml" down
            run mv "$ARR_DIR/compose.yml" "$ARR_DIR/compose.yml.off"
        fi
        return
    fi
    step "Prowlarr, Sonarr, Radarr and Seerr (Docker)"
    local compose_pkg=docker-compose-v2 tz pgid app apps code dir roots=() series_root="" movie_root=""
    # Ubuntu's docker-compose-v2; Debian ships Compose v2 as docker-compose
    apt-cache show docker-compose-v2 >/dev/null 2>&1 || compose_pkg=docker-compose
    apt_install docker.io "$compose_pkg"
    systemctl is-enabled -q docker 2>/dev/null || as_root systemctl enable --now docker

    tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
    pgid=$(getent group debian-transmission | cut -d: -f3 || true)
    [[ -n $pgid ]] || (( DRY_RUN )) || die "no debian-transmission group"
    apps=(prowlarr sonarr radarr seerr)
    [[ $whisparr != yes ]] || apps+=(whisparr)
    for app in "${apps[@]}"; do
        [[ -d $ARR_DIR/$app ]] || run mkdir -p "$ARR_DIR/$app"
    done
    # Whisparr: its service appended; switched off, the next "up --remove-orphans" removes it
    # (its config stays in $ARR_DIR/whisparr)
    { cat "$REPO/templates/arr-compose.yml"
      [[ $whisparr != yes ]] || cat "$REPO/templates/arr-compose-whisparr.yml"
    } >"$TMP/compose.template"
    helper render "$TMP/compose.template" "PUID=$(id -u)" "PGID=${pgid:-107}" \
        "TZ=${tz:-Etc/UTC}" "ARR_DIR=$ARR_DIR" "STORAGE_ROOT=$storage_root" >"$TMP/compose.yml"
    update_file "$TMP/compose.yml" "$ARR_DIR/compose.yml" 644 || true
    # Pulls missing images and (re)creates only the containers whose definition changed.
    # Newer images are not pulled here; see the README for updating.
    as_root docker compose -f "$ARR_DIR/compose.yml" up -d --remove-orphans

    # Library folders: the HD series and movie folders from [routes] (Jellyfin's libraries)
    for code in hdser_hun hdser; do
        dir=$(route_dir "$code"); [[ -z $dir ]] || roots+=(--series-root "$dir")
        [[ -n $series_root ]] || series_root=$dir  # Seerr's default: the first (Hungarian)
    done
    for code in hd_hun hd; do
        dir=$(route_dir "$code"); [[ -z $dir ]] || roots+=(--movie-root "$dir")
        [[ -n $movie_root ]] || movie_root=$dir
    done
    if [[ $whisparr == yes ]]; then  # not xxx_imageset: Whisparr does videos only
        roots+=(--whisparr)
        for code in xxx_hd xxx_xvid xxx_dvd; do
            dir=$(route_dir "$code"); [[ -z $dir ]] || roots+=(--xxx-root "$dir")
        done
    fi
    if (( DRY_RUN )); then
        info "would connect them: logins ('$rpc_user'), Transmission as download client, Sonarr and"
        info "Radarr to Prowlarr, HUN custom format, nCore indexer, Prowlarr alerts to ntfy,"
        [[ $whisparr != yes ]] || info "and Whisparr: login, Transmission, Prowlarr's XXX categories"
        info "root folders: $(printf '%s\n' "${roots[@]}" | grep -v '^--' | paste -sd ' ')"
        [[ $media_server != jellyfin ]] || info "would set up Seerr with Jellyfin, Sonarr and Radarr (first run)"
        return
    fi
    info "connecting them (waits for the apps to start)"
    ncore_2fa
    if ! RPC_USER=$rpc_user RPC_PASSWORD=$rpc_password NCORE_USER=${ncore_user:-} \
            NCORE_PASSWORD=${ncore_password:-} NCORE_2FA=${ncore_code:-} \
            helper arr-connect --arr-dir "$ARR_DIR" --downloads-dir "$storage_root/downloads" \
            --misc-dir "$download_dir" --ntfy-server "$ntfy_server" --ntfy-topic "$ntfy_topic" \
            "${roots[@]}" | sed 's/^/   /'; then
        die "could not connect the apps; see the error above"
    fi
    setup_seerr "$series_root" "$movie_root"
}

# nCore's 2FA code, only when Prowlarr doesn't have the indexer yet, or has it but never
# logged in (a first run stopped there: "off"). Asked right before the login, since a code is
# only valid for a moment. Prowlarr can't make new codes, so when
# its nCore session expires it can't log in again: its health alert comes as a push, and you
# enter a fresh code on the start page (or setup_helper.py ncore-code, or in Prowlarr).
ncore_2fa() {
    ncore_code=${NCORE_2FA:-}
    [[ -n ${ncore_user:-} && -z $ncore_code ]] || return 0
    case $(helper ncore-status --arr-dir "$ARR_DIR") in missing|off) ;; *) return 0 ;; esac
    [[ -t 0 ]] || { info "no NCORE_2FA: adding nCore without a 2FA code"; return 0; }
    ask ncore_code "nCore 2FA code from your authenticator app (empty if you don't use 2FA)"
}

# Seerr's first-run setup (with Jellyfin only): sign in as the Jellyfin admin, which makes
# that account Seerr's admin; then Sonarr and Radarr. Not fatal: the web wizard can do it too.
setup_seerr() {  # setup_seerr SERIES_ROOT MOVIE_ROOT
    local jf_user=$rpc_user jf_password=$rpc_password
    if [[ $media_server != jellyfin ]]; then
        info "Seerr: set it up in its web page (http://${lan_ip:-<this box>}:5055/); it works with Jellyfin, Plex or Emby"
        return
    fi
    if [[ $(helper seerr-status) == new ]] &&
            ! JF_USER=$jf_user JF_PASSWORD=$jf_password helper jellyfin-login >/dev/null; then
        if [[ -n ${JELLYFIN_USER:-} && -n ${JELLYFIN_PASSWORD:-} ]]; then
            jf_user=$JELLYFIN_USER jf_password=$JELLYFIN_PASSWORD
        elif [[ -t 0 ]]; then
            info "Seerr signs in with a Jellyfin admin account once; the Transmission login isn't one"
            ask jf_user "Jellyfin admin username"
            read -r -s -p "   Jellyfin password: " jf_password; echo
        else
            warn "Seerr: no Jellyfin admin login (JELLYFIN_USER/JELLYFIN_PASSWORD); finish it in its web page"
            return
        fi
    fi
    if ! JF_USER=$jf_user JF_PASSWORD=$jf_password helper seerr-setup --arr-dir "$ARR_DIR" \
            --series-root "$1" --movie-root "$2" --app-url "http://$(hostname):5055" \
            --ntfy-server "$ntfy_server" --ntfy-topic "$ntfy_topic" | sed 's/^/   seerr: /'; then
        warn "Seerr's setup failed (see above); finish it at http://${lan_ip:-<this box>}:5055/"
    fi
}

# Weekly update of the apps to their newest images, pushing what changed. [setup]
# arr_auto_update = no switches it off (then update by hand, see README).
setup_arr_updates() {
    local auto changed=0
    auto=$(ini "$config_src" setup arr_auto_update)
    if [[ $arr != yes || ${auto,,} == no ]]; then
        if systemctl is-enabled -q torrent-box-arr-update.timer 2>/dev/null; then
            info "app updates: switched off ([setup] arr = no / arr_auto_update = no)"
            as_root systemctl disable --now torrent-box-arr-update.timer
        fi
        return
    fi
    step "Weekly app updates"
    if update_file "$REPO/templates/arr-update" "$LIB/arr-update" 755 root; then changed=1; fi
    helper render --strip-comments "$REPO/systemd/torrent-box-arr-update.service" \
        "COMPOSE=$ARR_DIR/compose.yml" >"$TMP/arr-update.service"
    if update_file "$TMP/arr-update.service" /etc/systemd/system/torrent-box-arr-update.service 644 root; then
        changed=1
    fi
    if update_file "$REPO/systemd/torrent-box-arr-update.timer" \
            /etc/systemd/system/torrent-box-arr-update.timer 644 root; then
        changed=1
    fi
    if (( changed )); then as_root systemctl daemon-reload; fi
    systemctl is-enabled -q torrent-box-arr-update.timer 2>/dev/null ||
        as_root systemctl enable --now torrent-box-arr-update.timer
}

### 11. rss2transmission

setup_rss2transmission() {
    step "rss2transmission"
    local changed=0 d dirs
    update_file "$(stamped "$REPO/rss2transmission.py")" "$BIN" 755 || true

    helper service-unit "$REPO/systemd/rss2transmission.service" "$config_src" \
        >"$TMP/rss2transmission.service"
    if update_file "$TMP/rss2transmission.service" "$USER_UNITS/rss2transmission.service" 644; then
        changed=1
    fi
    if update_file "$REPO/systemd/rss2transmission.timer" "$USER_UNITS/rss2transmission.timer" 644; then
        changed=1
    fi
    if (( SHADOW )); then
        if update_file "$REPO/systemd/shadow.conf" \
                "$USER_UNITS/rss2transmission.service.d/shadow.conf" 644; then
            changed=1
        fi
    elif [[ -f $USER_UNITS/rss2transmission.service.d/shadow.conf ]]; then
        info "shadow mode is on (rss2transmission.service.d/shadow.conf): runs are dry runs."
        info "to go live: remove it, run 'rss2transmission --catch-up', daemon-reload (PLAN.md, Cutover)"
    fi

    # The sandbox only allows writing to these, and they must exist.
    dirs=("$STATE_DIR" "$(ini --path "$config_src" general watch_dir)"
          "$(ini --path "$config_src" general archive_dir)")
    for d in "${dirs[@]}"; do
        [[ -d $d ]] || run mkdir -p "$d"
    done

    if [[ $(loginctl show-user "$USER" --property=Linger --value 2>/dev/null) != yes ]]; then
        as_root loginctl enable-linger "$USER"  # user units keep running while logged out
    fi
    if (( changed )); then run systemctl --user daemon-reload; fi
    if systemctl --user is-enabled -q rss2transmission.timer 2>/dev/null &&
            systemctl --user is-active -q rss2transmission.timer; then
        info "timer enabled and running"
    else
        # The first run only records what's already in the feed: nothing old gets added.
        run systemctl --user enable --now rss2transmission.timer
    fi
}

### 12. Start page

# dashboard.py on port 80: links to the web apps and their health. A system unit running as
# you (see systemd/torrent-box-dashboard.service); the program is root's, since it runs with
# the capability to use a low port.
# Desireerr, with Whisparr: its own (public) repo, cloned to ~/desireerr and fast-forwarded
# on each run unless it has local changes, run by a user unit like rss2transmission. With
# Whisparr switched off, the unit goes; the checkout and its data (~/.local/state/desireerr)
# stay.
setup_desireerr() {
    local unit=$USER_UNITS/desireerr.service changed=0 before
    if ! desireerr_on; then
        if [[ -f $unit ]]; then
            step "Desireerr"
            run systemctl --user disable --now desireerr.service
            run rm -f "$unit" "$USER_UNITS/desireerr.service.d/torrent-box.conf"
            run systemctl --user daemon-reload
            info "stopped; $DESIREERR_DIR and its data ($DESIREERR_DATA) are kept"
        fi
        return
    fi
    step "Desireerr (requests for StashDB scenes, port $DESIREERR_PORT)"
    if [[ ! -e $DESIREERR_DIR ]]; then
        run git clone -q "$DESIREERR_REPO" "$DESIREERR_DIR"
        changed=1
    elif [[ ! -d $DESIREERR_DIR/.git ]]; then
        warn "$DESIREERR_DIR is not a git checkout; leaving it as it is"
    elif [[ -n $(git -C "$DESIREERR_DIR" status --porcelain --untracked-files=no) ]]; then
        warn "$DESIREERR_DIR has local changes; not updating it"
    elif (( DRY_RUN )); then
        info "would run: git -C $(quote "$DESIREERR_DIR") pull --ff-only"
    else
        before=$(git -C "$DESIREERR_DIR" rev-parse HEAD)
        if ! git -C "$DESIREERR_DIR" pull -q --ff-only; then
            warn "couldn't update $DESIREERR_DIR; keeping the version it has"
        elif [[ $(git -C "$DESIREERR_DIR" rev-parse HEAD) != "$before" ]]; then
            info "updated to $(git -C "$DESIREERR_DIR" log -1 --format='%h %s')"
            changed=1
        else
            info "up to date: $DESIREERR_DIR"
        fi
    fi
    if [[ ! -f $DESIREERR_DIR/systemd/desireerr.service ]]; then
        if (( DRY_RUN )); then
            info "would install its user unit and start it"
        else
            warn "no $DESIREERR_DIR/systemd/desireerr.service; Desireerr is not started"
        fi
        return
    fi
    if update_file "$DESIREERR_DIR/systemd/desireerr.service" "$unit" 644; then
        changed=1
    fi
    # It writes the scenes' .nfo files for Jellyfin beside them: the storage disk is writable
    # in its sandbox (the rest stays read-only).
    printf '%s\n' "# torrent-box: Desireerr writes the scenes' .nfo files (setup.sh writes this)" \
        "[Service]" "ReadWritePaths=-$storage_root" >"$TMP/desireerr-storage.conf"
    if update_file "$TMP/desireerr-storage.conf" "$USER_UNITS/desireerr.service.d/torrent-box.conf" 644; then
        changed=1
    fi
    if (( changed )); then run systemctl --user daemon-reload; fi
    # Its admin is the login all the apps share, its StashDB key the one in secrets.ini, and
    # with Jellyfin an API key named Desireerr for its Watch button (all through the
    # environment, never the command line). It follows a changed login; an admin made on its
    # first visit, or a key changed in its Settings, stay as they are.
    if (( DRY_RUN )); then
        info "would set its admin ('${rpc_user:-?}', the shared login), StashDB key and Jellyfin key, where not done yet"
    elif [[ -n ${rpc_user:-} || -n $stashdb_key ]]; then
        local out line jf_key=""
        if [[ $media_server == jellyfin ]] &&
                ! jf_key=$(JF_USER=${JELLYFIN_USER:-${rpc_user:-}} JF_PASSWORD=${JELLYFIN_PASSWORD:-${rpc_password:-}} \
                    helper jellyfin-key Desireerr); then
            jf_key=""
            warn "no Jellyfin API key for its Watch button (above); add one in its Settings"
        fi
        if out=$(DESIREERR_ADMIN_USER=${rpc_user:-} DESIREERR_ADMIN_PASSWORD=${rpc_password:-} \
                DESIREERR_STASHDB_KEY=$stashdb_key DESIREERR_JELLYFIN_KEY=$jf_key \
                PYTHONDONTWRITEBYTECODE=1 python3 "$DESIREERR_DIR/desireerr.py" init --data "$DESIREERR_DATA"); then
            while IFS= read -r line; do info "$line"; done <<<"$out"
        else
            warn "couldn't set its admin and StashDB key (see above); its first visit asks instead"
        fi
    fi
    if ! systemctl --user is-enabled -q desireerr.service 2>/dev/null; then
        run systemctl --user enable --now desireerr.service
    elif ! systemctl --user is-active -q desireerr.service; then
        run systemctl --user start desireerr.service
    elif (( changed )); then
        run systemctl --user restart desireerr.service
    else
        info "running"
    fi
}

# Whisparr's folders as a Jellyfin library of the type "Home videos and photos": its videos
# aren't Movies, so neither the random button (Jellyfin Enhanced) nor Seerr offers them, and
# a scene's own picture is its cover (Desireerr's folder.jpg). A library that has them already
# stays (setup warns if it's another type). Hide it from other users in Jellyfin (README).
setup_xxx_library() {
    desireerr_on && [[ $media_server == jellyfin ]] || return 0
    step "Jellyfin library for Whisparr"
    local code dir out line roots=() jf_user=${JELLYFIN_USER:-${rpc_user:-}} jf_password=${JELLYFIN_PASSWORD:-${rpc_password:-}}
    for code in xxx_hd xxx_xvid xxx_dvd; do
        dir=$(route_dir "$code"); [[ -z $dir ]] || roots+=(--root "$dir")
    done
    if (( ${#roots[@]} == 0 )); then
        info "no XXX folders in [routes]; nothing to add"
        return
    fi
    if (( DRY_RUN )); then
        info "would add $(printf '%s\n' "${roots[@]}" | grep -v '^--' | paste -sd ' ') as library XXX (Home videos and photos), unless a library has them"
        return
    fi
    if ! out=$(JF_USER=$jf_user JF_PASSWORD=$jf_password helper jellyfin-xxx-library "${roots[@]}"); then
        warn "couldn't add Whisparr's folders to Jellyfin (above); add them as a Home videos and photos library"
        return
    fi
    while IFS= read -r line; do
        if [[ $line == warning:* ]]; then warn "${line#warning: }"; else info "$line"; fi
    done <<<"$out"
}

setup_dashboard() {
    step "Start page (http://$(hostname)/)"
    local changed=0
    if update_file "$(stamped "$REPO/dashboard.py")" "$DASHBOARD_BIN" 755 root; then changed=1; fi
    helper render --strip-comments "$REPO/systemd/torrent-box-dashboard.service" "USER=$USER" \
        "UID=$(id -u)" "PORT=$DASHBOARD_PORT" "CONFIG=$CONFIG" "ARR_DIR=$ARR_DIR" \
        >"$TMP/torrent-box-dashboard.service"
    if update_file "$TMP/torrent-box-dashboard.service" "$DASHBOARD_UNIT" 644 root; then
        changed=1
        as_root systemctl daemon-reload
    fi
    if systemctl is-active -q torrent-box-dashboard 2>/dev/null; then
        if (( changed )); then as_root systemctl restart torrent-box-dashboard; fi
    elif ss -Hltn "sport = :$DASHBOARD_PORT" 2>/dev/null | grep -q .; then
        warn "port $DASHBOARD_PORT is used by another program; the start page is not started"
        return
    else
        as_root systemctl start torrent-box-dashboard
    fi
    systemctl is-enabled -q torrent-box-dashboard 2>/dev/null ||
        as_root systemctl enable torrent-box-dashboard
}

### 13. Firewall

setup_firewall() {
    step "Firewall"
    if ! command -v ufw >/dev/null; then
        info "ufw not installed; nothing to do"
        return
    fi
    # Also while ufw is off: ufw keeps the rules, so a later 'sudo ufw enable' (which denies
    # everything else coming in) keeps the box reachable, SSH included.
    if (( DRY_RUN )); then
        # 'ufw status' needs root; ufw.conf says whether it's enabled at boot.
        if grep -q '^ENABLED=yes' /etc/ufw/ufw.conf 2>/dev/null; then
            info "ufw is enabled (per /etc/ufw/ufw.conf); existing rules are skipped by ufw itself"
        else
            info "ufw is not enabled (per /etc/ufw/ufw.conf); the rules are added for when it is"
        fi
    elif ! sudo ufw status | grep -q '^Status: active'; then
        info "ufw is not active; adding the rules anyway, for when it's turned on (sudo ufw enable)"
    fi
    if pkg_installed openssh-server; then
        as_root ufw allow from "$lan_net" to any port 22 proto tcp comment 'torrent-box: SSH'
        [[ $tailscale != yes ]] ||
            as_root ufw allow in on tailscale0 to any port 22 proto tcp comment 'torrent-box: SSH from the tailnet'
    fi
    as_root ufw allow 51413/tcp comment 'torrent-box: BitTorrent'
    as_root ufw allow 51413/udp comment 'torrent-box: BitTorrent'
    as_root ufw allow from "$lan_net" to any port 9091 proto tcp comment 'torrent-box: Transmission web UI'
    as_root ufw allow from "$lan_net" to any port "$DASHBOARD_PORT" proto tcp comment 'torrent-box: start page'
    [[ $own_ntfy != yes ]] ||
        as_root ufw allow from "$lan_net" to any port "$NTFY_PORT" proto tcp comment 'torrent-box: ntfy'
    case $media_server in
        jellyfin)
            as_root ufw allow from "$lan_net" to any port 8096 proto tcp comment 'torrent-box: Jellyfin'
            as_root ufw allow from "$lan_net" to any port 7359 proto udp comment 'torrent-box: Jellyfin discovery'
            # only used by Jellyfin's DLNA plugin, if installed
            as_root ufw allow from "$lan_net" to any port 1900 proto udp comment 'torrent-box: DLNA discovery' ;;
        minidlna)
            as_root ufw allow from "$lan_net" to any port 8200 proto tcp comment 'torrent-box: DLNA'
            as_root ufw allow from "$lan_net" to any port 1900 proto udp comment 'torrent-box: DLNA discovery' ;;
    esac
    if [[ $tailscale == yes ]]; then
        # Tailscale works without it (NAT traversal), but direct connections need this port.
        as_root ufw allow 41641/udp comment 'torrent-box: Tailscale'
        local port ports=(9091 "$DASHBOARD_PORT")
        case $media_server in jellyfin) ports+=(8096) ;; minidlna) ports+=(8200) ;; esac
        [[ $samba != yes ]] || ports+=(445)
        [[ $own_ntfy != yes ]] || ports+=("$NTFY_PORT")
        for port in "${ports[@]}"; do
            as_root ufw allow in on tailscale0 to any port "$port" proto tcp comment 'torrent-box: from the tailnet'
        done
    fi
    if [[ $arr == yes ]]; then
        local name names=(prowlarr:9696 sonarr:8989 radarr:7878 seerr:5055)
        [[ $whisparr != yes ]] || names+=(whisparr:6969)
        ! desireerr_on || names+=(desireerr:$DESIREERR_PORT)
        for name in "${names[@]}"; do
            as_root ufw allow from "$lan_net" to any port "${name#*:}" proto tcp comment "torrent-box: ${name%%:*}"
            [[ $tailscale != yes ]] ||
                as_root ufw allow in on tailscale0 to any port "${name#*:}" proto tcp comment 'torrent-box: from the tailnet'
        done
    fi
    if [[ $samba == yes ]]; then
        as_root ufw allow from "$lan_net" to any port 445 proto tcp comment 'torrent-box: Samba'
        as_root ufw allow from "$lan_net" to any port 3702 proto udp comment 'torrent-box: wsdd discovery'
        as_root ufw allow from "$lan_net" to any port 5357 proto tcp comment 'torrent-box: wsdd'
        # The share allows IPv6 link-local too (Windows often connects that way after finding
        # the box via wsdd); ufw has IPv6 rules only with IPV6=yes (its default).
        if grep -q '^IPV6=yes' /etc/default/ufw 2>/dev/null; then
            as_root ufw allow from fe80::/10 to any port 445 proto tcp comment 'torrent-box: Samba (IPv6 link-local)'
            as_root ufw allow from fe80::/10 to any port 3702 proto udp comment 'torrent-box: wsdd discovery (IPv6 link-local)'
            as_root ufw allow from fe80::/10 to any port 5357 proto tcp comment 'torrent-box: wsdd (IPv6 link-local)'
        fi
    fi
    if [[ $avahi == yes ]]; then
        as_root ufw allow from "$lan_net" to any port 5353 proto udp comment 'torrent-box: mDNS (avahi)'
        if grep -q '^IPV6=yes' /etc/default/ufw 2>/dev/null; then
            as_root ufw allow from fe80::/10 to any port 5353 proto udp comment 'torrent-box: mDNS (avahi, IPv6 link-local)'
        fi
    fi
}

### 14. Test push and summary

finish() {
    step "Test notification"
    if (( DRY_RUN )); then
        info "would run: ~/.local/bin/rss2transmission --test-notify"
    elif ! "$BIN" -c "$CONFIG" --test-notify; then
        warn "the test push failed; check the network and [ntfy] server in $CONFIG"
    fi

    step "Summary"
    local svc timer svcs=(transmission-daemon)
    [[ $media_server == none ]] || svcs+=("$media_server")
    [[ $samba != yes ]] || svcs+=(smbd)
    if [[ $samba == yes && -n $(wsdd_unit) ]]; then svcs+=("$(wsdd_unit)"); fi
    [[ $avahi != yes ]] || svcs+=(avahi-daemon)
    [[ $tailscale != yes ]] || svcs+=(tailscaled)
    [[ $own_ntfy != yes ]] || svcs+=(ntfy)
    svcs+=(smartmontools torrent-box-dashboard)
    for svc in "${svcs[@]}"; do
        printf '   %-24s %s\n' "$svc" "$(systemctl is-active "$svc" 2>/dev/null || true)"
    done
    printf '   %-24s %s\n' "rss2transmission.timer" \
        "$(systemctl --user is-active rss2transmission.timer 2>/dev/null || true)"
    timer=$(systemctl --user list-timers rss2transmission.timer --no-pager 2>/dev/null |
        awk 'NR == 2 && $1 != "n/a" { print $1, $2, $3, $4 }' || true)
    [[ -z $timer ]] || info "next run: $timer"
    info "Start page:             http://$(hostname)/  (links to all of these, and their health)"
    [[ $avahi != yes ]] ||
        info "LAN name:               $(hostname).local, e.g. http://$(hostname).local/ (works without the router's DNS)"
    info "Transmission web UI:    http://${lan_ip:-<this box>}:9091/  (user '${rpc_user:-?}')"
    case $media_server in
        jellyfin)
            info "Jellyfin:               http://${lan_ip:-<this box>}:8096/  (a new install: admin '${rpc_user:-?}' and the libraries set up; see README)" ;;
        minidlna)
            info "DLNA server:            '$(hostname)', status page http://${lan_ip:-<this box>}:8200/" ;;
    esac
    if [[ $samba == yes ]]; then
        info "Samba share:            \\\\$(hostname)\\storage (Windows), smb://${lan_ip:-<this box>}/storage (Mac, Linux, phones); user '$USER'"
    fi
    if [[ $arr == yes ]]; then
        info "Seerr (requests):       http://${lan_ip:-<this box>}:5055/  (sign in with your Jellyfin login)"
        info "Sonarr / Radarr:        http://${lan_ip:-<this box>}:8989/  /  :7878/"
        [[ $whisparr != yes ]] || info "Whisparr:               http://${lan_ip:-<this box>}:6969/"
        if desireerr_on; then
            if [[ -f $DESIREERR_DATA/setup-code ]]; then
                info "Desireerr:              http://${lan_ip:-<this box>}:$DESIREERR_PORT/  (first visit asks for the code in $DESIREERR_DATA/setup-code)"
            else
                info "Desireerr:              http://${lan_ip:-<this box>}:$DESIREERR_PORT/  (sign in as '${rpc_user:-?}')"
            fi
        fi
        info "Prowlarr (nCore):       http://${lan_ip:-<this box>}:9696/  (nCore's session ended: a new 2FA code on the start page)"
    fi
    [[ -z ${disk_dev:-} ]] ||
        info "Disk health:            smartd watches $disk_dev; problems come as urgent pushes"
    if [[ $arr == yes && $(ini "$config_src" setup arr_auto_update) != no ]]; then
        info "App updates:            weekly (Monday ~04:30), pushed when an app got a new version"
    fi
    if [[ $tailscale == yes ]]; then
        tailscale_status
        if [[ $ts_state == Running ]]; then
            info "Tailscale:              $ts_name ($ts_ip): from anywhere, use '${ts_name%%.*}' instead of ${lan_ip:-the LAN address} above"
        else
            info "Tailscale:              not connected (${ts_state:-not running})"
        fi
    fi
    if [[ $own_ntfy != yes ]]; then
        info "ntfy: subscribe to '$ntfy_topic' in the ntfy app, or open $ntfy_server/$ntfy_topic"
    elif [[ $tailscale == yes && ${ts_state:-} == Running ]]; then
        info "ntfy (own server):      in the ntfy app, + → Use another server: http://$ts_name:$NTFY_PORT"
        info "                        topic '$ntfy_topic' (keep Tailscale on on the phone; also at home)"
    else
        info "ntfy (own server):      in the ntfy app, + → Use another server: http://${lan_ip:-<this box>}:$NTFY_PORT"
        info "                        topic '$ntfy_topic' (reaches the phone at home only)"
    fi
    info "logs: journalctl --user -u rss2transmission"
    if (( DRY_RUN )); then
        printf '\nDry run: nothing was changed.\n'
    else
        printf '\nDone.\n'
    fi
}

### Main

main() {
    local arg
    for arg in "$@"; do
        case $arg in
            --dry-run) DRY_RUN=1 ;;
            --shadow) SHADOW=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; exit 2 ;;
        esac
    done
    (( EUID != 0 )) || die "run setup.sh as your normal user (it calls sudo itself)"
    # systemctl --user needs the user's runtime dir (missing e.g. after 'su')
    if [[ -z ${XDG_RUNTIME_DIR:-} && -d /run/user/$EUID ]]; then
        export XDG_RUNTIME_DIR=/run/user/$EUID
    fi

    TMP=$(mktemp -d -t torrent-box-setup.XXXXXX)
    trap 'rm -rf "$TMP"' EXIT
    TB_VERSION=$(repo_version)
    banner
    if (( DRY_RUN )); then
        echo "Dry run: showing what would be done; nothing is changed."
    else
        echo "torrent-box setup. sudo is used for the system steps."
        sudo -v
    fi

    check_os
    install_packages
    detect_lan
    choose_storage_root
    choose_media_server
    choose_samba
    choose_avahi
    choose_tailscale
    choose_own_ntfy
    choose_arr
    choose_whisparr
    choose_download_speed
    setup_storage
    install_dropins
    setup_secrets
    setup_config
    setup_transmission
    setup_media_server
    setup_samba
    setup_avahi
    setup_tailscale
    setup_ntfy
    setup_disk_health
    setup_arr
    setup_arr_updates
    setup_rss2transmission
    setup_desireerr
    setup_xxx_library
    setup_dashboard
    setup_firewall
    finish
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then  # not when sourced (lets a test drive the steps)
    main "$@"
fi
