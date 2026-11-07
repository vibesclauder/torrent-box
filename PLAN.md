# torrent-box — plan

Replace FlexGet + `periodic.sh` (cron) + `torrent_done.sh` with one small program of our own,
and bundle it with transmission-daemon and a media server (Jellyfin or minidlna) behind an
idempotent `setup.sh`, so a new
machine is `git clone && ./setup.sh`.

Status: **live since 2026-10-17 23:02** (cutover done; FlexGet stopped).
Milestones 1–4 done; still open: root-crontab cleanup (needs sudo), removing the old pieces
after a few quiet days, shellcheck + a clean-VM trial of `setup.sh`.

## Decisions

| Topic | Decision | Why (short) |
|---|---|---|
| Language | Python 3, standard library only | Already on every Ubuntu box, no build step; HTTP/XML/JSON/sqlite are built in |
| Process model | systemd **timer** every 5 min; each start = one run | A crash/hang costs one run; `systemctl list-timers` shows health |
| Download-done notices | Daemon polls Transmission (replaces `torrent_done.sh`) | One config/log/notifier; no `settings.json` hook to manage |
| Notifications | **ntfy** (ntfy.sh, or an own server on the box) | One HTTP POST, phone push, no SMTP/Gmail setup |
| Config format | INI (`configparser`) | Python 3.10 on this box has no `tomllib` |
| State | SQLite file | Atomic writes, easy to query by hand |
| Bundle | GitHub repo `vibesclauder/torrent-box` | Backup lives off the machine it rebuilds |
| Media server | Jellyfin by default, minidlna as the light option, or none | Jellyfin: library, apps, transcoding; minidlna still fits weak/32-bit boxes |
| File access | Optional Samba share of the disk (LAN only, own user) | Manage files from Windows/Mac/phones; included file, not a rewritten smb.conf |
| Remote access | Optional Tailscale; services opened to the tailnet, nothing on the router | No port forwarding or exposed web UIs; works from phones; free tier is enough |
| Start page | Own stdlib page on port 80 (`dashboard.py`), not Homepage/Heimdall | Works without Docker and on small boxes; knows our setup (config.ini, units, API keys) with no config of its own |

## Repo layout

```
torrent-box/
  PLAN.md
  README.md
  setup.sh                      # idempotent installer, safe to re-run
  setup_helper.py               # setup.sh's Python parts (settings merge, LAN, INI, units)
  rss2transmission.py           # the program (single file)
  dashboard.py                  # start page on port 80: links + health (single file)
  config.example.ini            # copied to ~/.config/rss2transmission/config.ini by setup.sh
  secrets.example.ini           # template for secrets.ini (real one never committed)
  systemd/
    rss2transmission.service    # user unit, Type=oneshot, sandboxed, 10 min timeout
    rss2transmission.timer      # OnBootSec=2min, OnUnitActiveSec=5min
    shadow.conf                 # drop-in forcing --dry-run (shadow mode before cutover)
    storage-override.conf       # drop-in for transmission + media server (@STORAGE_ROOT@ template)
    torrent-box-dashboard.service  # system unit for the start page, runs as the user
    torrent-box-arr-update.{service,timer}  # weekly docker compose pull + up, Monday ~04:30
  templates/
    transmission-settings.json
    minidlna.conf
    jellyfin.sources            # Jellyfin's apt repo (deb822)
    samba.conf                  # the [storage] share, included from smb.conf
    push                        # root's ntfy push (rendered with the topic, mode 700)
    smartd-notify               # smartd -M exec -> push
    arr-update                  # pull, up -d, push which apps got a new image
  tests/
    helpers.py                  # fake nCore + Transmission server, config writer
    test_routing.py  test_feed.py  test_rpc.py  test_run.py  test_completions.py  test_notify.py
    test_setup_helper.py  test_dashboard.py
    fixtures/feed.xml           # sanitised sample feed (no passkey)
```

## Program behaviour

Each run (the program always does one run and exits; the timer repeats it), in order:

1. **Sanity:** storage root exists and is a mountpoint (`require_mount = yes`). If not → log
   error, notify once (rate-limited), skip adding. *Protects against Transmission silently
   downloading onto the system disk when the HDD isn't mounted.*
2. **Feed:** GET the bookmark RSS (timeout 30 s; nCore sends no ETag/Last-Modified, and the
   feed is ~30 KB, so it's fetched in full every run). For each
   item not in `seen` (key = nCore torrent id from the link), oldest first:
   - download the .torrent; filename comes from `Content-Disposition`
     (`[nCore][hd_hun]Name.torrent`) → category code `hd_hun`
   - look the code up in `[routes]` → download dir (unknown code → `misc_dir`); for the
     `show_folders` categories (default: nCore's series codes) one level more, a folder per
     show: the release name up to the season marker (`S01`, `S01E02`, `S01-S03`, `Season 1`,
     `1.evad`), dots → spaces, initials kept (`S.W.A.T`), a year before the marker →
     `(2005)`. An existing folder whose name matches ignoring case, punctuation and a
     missing `(year)` is reused (so hand-renamed folders stick; another year is another
     show). With `[setup] arr = yes`, not in `hdser_hun`/`hdser` (`SONARR_ROUTES`: Sonarr's
     root folders; it imports files found in its show folders in place). No marker → the plain category
     folder. Transmission creates the folder (setgid parent → right group); a media server
     sees one show instead of one per season pack/episode.
   - Transmission RPC `torrent-add` (metainfo base64 + `download-dir`);
     `torrent-duplicate` counts as success
   - on success: insert into `seen` (id, info-hash, title, category, dir, time), archive the
     .torrent into `archive_dir`
   - Transmission unreachable → stop this run, item stays unseen → retried next run
3. **Watch dir:** any `*.torrent` dropped by hand into `watch_dir` → same routing → on success
   move to `archive_dir`.
4. **Completions:** RPC `torrent-get` (hashString, name, percentDone, doneDate, downloadDir,
   sizeWhenDone). Every finished torrent whose hash is not in `announced` → notify, then insert.
   Insert only after ntfy accepted it → failed sends retry next run.
5. **Disk space:** `shutil.disk_usage(storage_root)`, percent computed and rounded up like
   `df`'s Use%; at/above `disk_warn_percent` → warning push, at most once per 24 h while it
   lasts (timestamp in state; cleared when it drops below, so the next crossing warns at once).
   Same rate-limiting for the mount warning. Skipped when the disk isn't mounted.
6. **Notify:** batch per run — one "Added" push listing new torrents, one "Downloaded" push
   listing completions (with sizes), separate high-priority push for disk/mount problems.
   Sent as JSON to the ntfy server root (not via headers), so non-ASCII names survive. Lists
   longer than 20 lines end with "… and N more". No `ntfy_topic` → notifications off.
   A failed "Added" push is an error but not retried (the torrent is in Transmission anyway);
   a failed "Downloaded" push leaves the torrents unannounced, so it's retried next run.

**First run** (empty state): mark everything currently in the feed as seen and everything
currently finished in Transmission as announced — no flood, nothing re-added. A short message
says how many were skipped.

**CLI:** `--dry-run` (log only; no RPC writes, no state changes, no pushes), `--catch-up`
(re-seed seen/announced from current feed/Transmission), `--test-notify` (send a test push,
milestone 2), `-c PATH` (config; secrets.ini is read from the same directory),
`--state PATH` (state database).

**Exit status:** 0 ok, 1 something failed this run (shows in `systemctl --user --failed` until
the next good run), 2 bad config/usage. A lock file stops two runs overlapping (e.g. a manual
run during a timer run); the second one just exits.

**Logging:** stderr → journald (`journalctl --user -u rss2transmission`). Passkey always masked.

## Config

`~/.config/rss2transmission/config.ini` (non-secret, from `config.example.ini`):

```ini
[general]
storage_root      = /mnt/storage
require_mount     = yes
watch_dir         = ~/torrents
archive_dir       = ~/old_torrents
disk_warn_percent = 90
show_folders      = hdser_hun, hdser, xvidser_hun, xvidser, dvdser_hun, dvdser

[transmission]
rpc_url = http://localhost:9091/transmission/rpc

[ntfy]
server = https://ntfy.sh

[routes]
# nCore category code = folder under storage_root; unknown codes -> misc_dir
misc_dir     = MISC
hd_hun       = MOVIES/HD/HU
hd           = MOVIES/HD/EN
xvid_hun     = MOVIES/SD/HU
xvid         = MOVIES/SD/EN
hdser_hun    = SERIES/HD/HU
hdser        = SERIES/HD/EN
xvidser_hun  = SERIES/SD/HU
xvidser      = SERIES/SD/EN
mp3_hun      = MUSIC/LOSSY/HU
mp3          = MUSIC/LOSSY/EN
lossless_hun = MUSIC/LOSSLESS/HU
lossless     = MUSIC/LOSSLESS/EN
game_iso     = GAMES/PC
game_rip     = GAMES/PC
console      = GAMES/CNS
iso          = PROGRAMS/PC
misc         = PROGRAMS/PC
mobil        = PROGRAMS/MBL
ebook_hun    = EBOOKS/HU
ebook        = EBOOKS/EN
xxx_hd       = XXX/HD
xxx_xvid     = XXX/SD
xxx_imageset = XXX/IMG
```

The table is the exact equivalent of `periodic.sh` for every code seen in `old_torrents`
(verified: `iso`/`misc` were what `[im][si][os]` matched). nCore's DVD codes (`dvd`, `dvd_hun`,
`dvdser…`, `xxx_dvd`) and `clip` went to MISC before and still do unless added.

`~/.config/rss2transmission/secrets.ini` (mode 0600, never in git):

```ini
[secrets]
feed_url      = https://ncore.pro/bookmarks/<key>
rpc_user      = ...
rpc_password  = ...
ntfy_topic    = torrent-box-<16 random a-z0-9>
```

State: `~/.local/state/rss2transmission/state.sqlite` — tables `seen`, `announced`, `meta`.

## setup.sh

Idempotent; run as the normal user, uses `sudo` for system steps; prompts only for values not
already configured (re-running never clobbers existing config/secrets). System files are only
rewritten, and services only restarted, when their content actually changes. The logic worth
testing lives in `setup_helper.py` (stdlib Python, unit-tested); `setup.sh` is the glue.
`--dry-run` prints every change instead of making it and never calls sudo or prompts;
`--shadow` also installs `shadow.conf`. Env vars `STORAGE_ROOT`, `STORAGE_UUID`, `STORAGE_DISK`,
`MEDIA_SERVER`, `SAMBA`, `SAMBA_PASSWORD`, `AVAHI`, `TAILSCALE`, `TS_AUTHKEY`, `ARR`, `DOWNLOAD_MBPS`, `FEED_URL`,
`RPC_USER`/`RPC_PASSWORD`, `LAN_SUBNET` answer the prompts for a non-interactive run.
`LC_ALL=C` for the whole script (it parses command output; this box's locale is Hungarian).

1. Check OS (Ubuntu 22.04+/Debian 12+, warns on older), `apt install transmission-daemon
   python3 curl` (only the missing ones). Detect the LAN subnet: the connected
   network on the default route's interface (`ip -o -4 route`).
   **Media server:** `[setup] media_server` in config.ini (`jellyfin`, `minidlna`, `none`;
   empty in the example), else `MEDIA_SERVER`, else whichever of the two is already installed
   (boxes set up before the option existed), else asked. Default jellyfin, but minidlna when
   Jellyfin's repo has no packages for this release/architecture (checked in its `Release`
   file; e.g. 32-bit ARM) or RAM is under 3 GB. **Samba:** `[setup] samba` (yes/no), else
   `SAMBA`, else asked (default yes). **avahi:** `[setup] avahi`, same way (default yes;
   asked also on an installed box). **Tailscale:** `[setup] tailscale`, same way.
   **Download speed:** `[setup] download_mbps`, else `DOWNLOAD_MBPS`, else measured and asked
   (measured value as the default): `setup_helper.py speed-test`, 8 parallel downloads of
   Hetzner's 10 GB test file (nbg1, then fsn1, then OVH), 2 s warm-up, 6 s counted. Not
   Cloudflare's speed test: 50 MB cap per request, and 429 after a few fast runs. A dry run
   measures too (it changes nothing). **Search and requests:** `[setup] arr`, else `ARR`, else
   asked (default no). All five saved to config.ini in step 5.
2. **Storage:** mount point = `storage_root` from config.ini if it exists, else asked (default
   `/mnt/storage`). Already mounted → nothing to do (warns if it isn't in fstab). In fstab →
   just mount. Otherwise a menu (`setup_helper.py storage-candidates` on `lsblk -J`, biggest
   first) of unmounted filesystems and blank disks (no partitions, no partition table, no
   filesystem, ≥ 1 GiB, writable; never RAID/LVM/LUKS members, swap, loop or zram), or
   `STORAGE_UUID` / `STORAGE_DISK`. A blank disk is re-checked with `blkid -p` (lsblk only
   knows udev's view), confirmed by typing its name (or `STORAGE_DISK`), then `sfdisk` (GPT,
   one Linux partition) → `mkfs.ext4 -L storage -m 0` (a data disk: root's 5 % would be
   600 GB of 12 TB), from 256 GiB also `-T largefile` (an inode per MiB instead of mke2fs's
   one per 32/64 KiB for big disks: ~90 GB of inode tables on 12 TB for files that are
   mostly GBs; 11 M inodes is still plenty), and its top folder gets group
   debian-transmission, 2775. A disk with
   anything on it is never offered for formatting. → fstab line with `nofail` (fs type
   from `lsblk`, `blkid` as fallback; ntfs → `ntfs-3g`, installed if missing; fstab backed up
   once to `/etc/fstab.before-torrent-box`) → `daemon-reload` → `mount <mount point>` (not
   `mount -a`, which would touch unrelated entries) → verify mountpoint. A UUID that's already
   in fstab under another mount point is an error.
3. **Drop-ins** for Transmission, the media server and smbd (moved up from step 7 so they're
   in place before the services are restarted; for smbd it also means nothing can be written
   into an empty mount point): `RequiresMountsFor=<storage>`, after network-online,
   `Restart=on-failure`, `RestartSec=30`, no start limit. Installed without the template's
   comment lines (they're identical to the ones already on this box).
4. **Secrets:** ask for feed URL; generate ntfy topic (`torrent-box-` + 16 random a-z0-9) →
   `secrets.ini` (0600, written atomically: temp file with mode 0600, then rename; only
   missing keys are added, comments kept). Print the topic + `https://ntfy.sh/<topic>`.
   **RPC login is user-chosen** (the same login is used by the Transmission web UI on :9091 and
   Transmission Remote GUI): prompt for username + password (hidden input, asked twice). If a
   login is already known (`secrets.ini`, or `~/.config/transmission-auth` on the current box)
   and a test RPC call with it succeeds, offer to keep it. A known login that doesn't work is
   reported and a new one asked for (setup then sets it in Transmission).
5. **Config:** copy `config.example.ini` if missing (with the chosen `storage_root`); create
   the folder tree under the storage root from `[routes]` (all parents too), plus
   `.incomplete` (Transmission's incomplete-dir), and with `arr = yes` `downloads/series` and
   `downloads/movies`. On a POSIX
   filesystem: owner `$USER`, group `debian-transmission`, mode 2775 (setgid: new files get the
   group); existing folders get `chgrp` + `g+rwxs,o+rx` (not recursive); `$USER` is added to
   `debian-transmission`. The media server reads as "other". On ntfs-3g/FAT/exFAT owners and modes
   don't exist (chmod/chown do nothing), so only missing folders are created.
6. **Transmission:** `systemctl stop` → **merge** our keys into the existing
   `/etc/transmission-daemon/settings.json` (Debian defaults on a fresh box; your tuning
   survives on an existing one) → start. Keys we own: `rpc-enabled`,
   `rpc-authentication-required`, `rpc-username`, `rpc-password`, `rpc-port` (9091),
   `rpc-whitelist-enabled` + `rpc-whitelist` (127.0.0.1 + the LAN subnet, auto-detected,
   e.g. `192.168.1.*`; + `100.64.*.*` .. `100.127.*.*` with Tailscale, the tailnet's
   100.64.0.0/10 in whole-octet wildcards), `rpc-host-whitelist-enabled` +
   `rpc-host-whitelist` (`<hostname>,<hostname>.*`: by default Transmission answers 421 to any
   Host header that isn't an IP address, so `http://<box>:9091/` and the MagicDNS name would
   fail), `download-dir`, `peer-port` (51413), `port-forwarding-enabled` (true: UPnP /
   NAT-PMP opens the peer port on the router, otherwise nobody can connect in and private
   trackers' torrents barely seed; it follows the box's DHCP address by itself),
   `dht-enabled`, `pex-enabled`, `lpd-enabled` (false: nCore's torrents are private, so these
   only ever matter for a public torrent added by hand, and that one stays out of the public
   swarm), `script-torrent-done-enabled`
   (false), `umask` (2 = 002, so files are group-writable and world-readable for the media server),
   `incomplete-dir` (`<storage>/.incomplete`, enabled: partial files stay out of the media
   folders, and on the same disk finishing is a rename, not a copy; hidden, so minidlna skips it;
   not on the SSD: at 2 Gbit/s that would copy every finished torrent and could fill `/`),
   `preallocation` (2 = full: each file is reserved in one piece up front, so the randomly
   arriving pieces don't fragment it on the HDD, and a full disk fails at add time).
   `templates/transmission-settings.json` holds just these keys. Sized from `download_mbps` in
   `setup_helper.speed_settings`: `cache-size-mb` (~2 s of download, power of two, 16..1024,
   at most RAM/16; 2 Gbit/s → 512) and `peer-limit-global`/`-per-torrent` (200/50 under
   300 Mbit/s, 400/80 under 1 Gbit/s, else 600/120). (Transmission rewrites
   settings on stop, hence stop-first and re-read after the stop; the plain password is hashed
   by Transmission on start.) If the login already works, auth is on and the username is the
   same, the hashed `rpc-password` is left alone — otherwise every run would rewrite it (new
   salt) and restart Transmission for nothing. Nothing differs → no stop/start at all. If the
   merge fails after the stop, Transmission is started again unchanged. Then verify with a
   test RPC call using the same login (up to 30 s) — fail loudly if it doesn't work. A dry run
   can't read settings.json (root-only), so it only lists the keys.
7. **Media server** (only the chosen one; the other, if enabled, is stopped and disabled but
   not removed, since both would want the DLNA port).
   **Jellyfin:** its apt repo as Jellyfin's install script sets it up (key from
   `repo.jellyfin.org/jellyfin_team.gpg.key`, kept armored as `/etc/apt/keyrings/jellyfin.asc`;
   `templates/jellyfin.sources`, suite = codename, `UBUNTU_CODENAME` on derivatives) →
   `apt install jellyfin` → `jellyfin` user into the group(s) owning `/dev/dri/renderD*`
   (hardware transcoding) → enable/start (restart if a group was added) → the first-run
   wizard on a fresh install (admin + libraries, see **Jellyfin wizard** under 9b). The VAAPI
   setting is done once in the web UI (README).
   **minidlna:** install, render `templates/minidlna.conf` (media_dir = storage root, port 8200,
   inotify, album art names as on this box, friendly name = hostname, db_dir
   `/var/cache/minidlna`) → restart if it changed. The distribution's file is kept once as
   `minidlna.conf.orig`. Rescan (stop, delete `files.db`, start) when `media_dir` changes —
   this is "first install" on a fresh box, but doesn't rescan an existing box whose media_dir
   is already right.
8. **Samba** (`samba = yes`): install `samba` (+ `wsdd-server`, or `wsdd` on releases that
   don't split it, for Windows' Network view; its service enabled) → render `templates/samba.conf` to `/etc/samba/torrent-box.conf`
   (`[storage]`: path = storage root, `valid users = $USER`, writable, `hosts allow` =
   localhost + LAN subnet + IPv6 link-local (+ the tailnet's 100.64.0.0/10 and
   fd7a:115c:a1e0::/48 with Tailscale), masks 0664/0775 so the setgid folders keep files
   usable by Transmission and the media server) → append `include =` for it to the
   distribution's `smb.conf` (backed up once to `smb.conf.before-torrent-box`) → `testparm`
   → if `$USER` has no Samba password (`pdbedit -u`), ask for one (`SAMBA_PASSWORD`) and set
   it with `smbpasswd -s -a` on stdin → enable/start smbd, restart if the share changed.
   `samba = no` after a share was set up: remove the include line, the file and the smbd
   drop-in; Samba itself and its users stay.
   **avahi** (`avahi = yes`): install `avahi-daemon` (it answers `<hostname>.local`) and
   write `/etc/avahi/services/torrent-box.service`: `_http._tcp` on the start page's port,
   plus `_smb._tcp` 445 with Samba (Finder / Linux file managers list the box). avahi
   picks up the file by itself. `avahi = no`: remove the file; avahi stays installed.
9. **Tailscale** (`tailscale = yes`): Tailscale's apt repo exactly as their install script
   sets it up (`<family>/<suite>.noarmor.gpg` → `/usr/share/keyrings/`, their
   `.tailscale-keyring.list`; family ubuntu/debian/raspbian) → install → enable tailscaled →
   if `tailscale status --json` isn't `Running` (or `Stopped`, which is left alone):
   `tailscale up`, interactive login link, or `--auth-key=file:` with `TS_AUTHKEY` written to
   a 0600 temp file (never on a command line). Never logs out or re-logs in on later runs.
   `tailscale = no`: nothing is uninstalled or disconnected (it may be how you're logged in
   right now); only the tailnet entries in the RPC whitelist and the share go away (ufw rules
   are only ever added, so the `tailscale0` ones stay until deleted by hand).
9a. **Own ntfy server** (`own_ntfy = yes`, asked after Tailscale, default yes with it):
   `systemd/ntfy-torrent-box.conf` → `/etc/systemd/system/ntfy.service.d/torrent-box.conf`
   *before* `apt install ntfy` (the distro's package, Ubuntu universe / Debian 13+; its unit
   isn't enabled on install, so it never starts on its default :80, the start page's):
   `NTFY_LISTEN_HTTP=:2586`, `NTFY_CACHE_FILE` in `CacheDirectory=ntfy`,
   `NTFY_CACHE_DURATION=72h` (a phone off the tailnet catches up), `Restart=always`. The
   package's `server.yml` (a conffile) stays untouched: no upgrade prompts. Enable, start
   (restart if the drop-in changed), wait for `/v1/health`. `ntfy_server` becomes
   `http://localhost:2586` for everything (the Docker apps use host networking), and
   `setup_config` writes it to `[ntfy] server`; `own_ntfy = no` with that value there sets it
   back to `https://ntfy.sh` and `disable --now`s ntfy. No accounts or ACLs: ufw (LAN +
   `tailscale0` on 2586) and the random topic, as on ntfy.sh. The summary prints
   `http://<MagicDNS name>:2586` for the phone (the LAN address without Tailscale). No iOS
   upstream (`upstream-base-url`): the phones here are Android.
9b. **Prowlarr, Sonarr, Radarr, Seerr** (`arr = yes`): `apt install docker.io
   docker-compose-v2` (Debian: `docker-compose`, which is Compose v2 there) → render
   `templates/arr-compose.yml` to `~/.local/share/torrent-box/arr/compose.yml` (configs in
   subfolders, user-owned) → `sudo docker compose up -d --remove-orphans`. Host networking: the
   apps reach Transmission/Jellyfin at localhost, and ufw applies (Docker-published ports would
   bypass it). linuxserver images with PUID = `$USER`, PGID = debian-transmission, UMASK 002, and
   the storage root mounted at the same path: imports are hardlinks, no remote path mappings.
   Seerr (`ghcr.io/seerr-team/seerr`, the merged Overseerr/Jellyseerr) as `PUID:PGID` too.
   Then `setup_helper.py arr-connect` (waits for each app's `config.xml` API key and API):
   nCore first (Cardigann definition `ncore`; username/password from secrets.ini, the 2FA
   code asked right before — `ncore-status` waits for all apps first, since a code is only
   valid ~30 s; POSTed disabled, then PUT enabled with the code: Prowlarr only keeps the
   cookies of a login for a saved indexer (`UpdateCookies`: `indexerId > 0`), so a login while
   adding is thrown away; then `ApplicationIndexerSync`; `ncore-code` does the PUT + sync with
   a fresh code later; only added once, never PUT again by setup once it's on, since that
   re-logs in with an old code; one that exists but was never switched on (`ncore-status`
   "off": the first login failed) gets a fresh code asked and the PUT;
   appProfileId = first app profile; no seed limits); forms login with the Transmission
   login, required for every address (`disabledForLocalAddresses` would need an Allowed Hosts
   list of every name the box is reached by: 400 without one; `*` isn't accepted) (the
   password can't be read back — Sonarr masks it, Radarr/Prowlarr return the hash — so it's
   set again, with the download clients, only when the Transmission login differs from the
   last run's: a PBKDF2 fingerprint in `arr/torrent-box-state.json`, written only once every
   app has it); a download client "Transmission" in Sonarr
   (directory `<storage>/downloads/series`; no category: with Transmission it can't have both,
   and the directory already tells its torrents apart), Radarr (`.../movies`) and Prowlarr
   (directory = misc_dir), `removeCompletedDownloads` false; applications "Sonarr"/"Radarr"
   in Prowlarr (fullSync); root folders = the `hdser_hun`, `hdser` / `hd_hun`, `hd` routes;
   custom format HUN (`ReleaseTitleSpecification` `\bHUN\b`, once; the app adds it to every
   profile with score 0, we PUT +100 where it's still 0, until a run has done every profile:
   `hun_scored` in the state file); a Prowlarr ntfy notification (onHealthIssue/Restored,
   priority high). Items are created from the app's `/schema` template; an existing one is
   only PUT when one of our fields differs (masked secrets aren't compared); other items and
   settings are left alone. POSTs/PUTs test the connection (forceSave only skips warnings),
   which is why everything waits for the apps first.
   **Seerr** (with Jellyfin only; failures only warn): `GET /api/v1/settings/public`; new →
   `POST auth/jellyfin` (serverType 2, localhost:8096; makes the Jellyfin admin Seerr's admin;
   Seerr creates its own Jellyfin API key, so the password is used once — the Transmission
   login if it's a Jellyfin admin, else JELLYFIN_USER/JELLYFIN_PASSWORD or asked, not stored)
   → `POST settings/jellyfin/library/sync` + `PUT .../library/<id> {enabled}` → Sonarr/Radarr
   (`settings/<kind>/test` for profiles; HD-1080p or the first; root = the first HU route;
   Seerr doesn't de-duplicate, so only if none on localhost:<port>) → `settings/main`
   `applicationUrl` = `http://<hostname>:5055` if empty (POST without `apiKey`: read-only, 400)
   → `settings/notifications/ntfy`: an agent without a topic gets enabled, types 402
   (pending 2 + failed 16 + auto-approved 128 + issue 256; not available 8, rss2transmission
   pushes that), url/topic; later only url/topic kept current, a disabled one left off
   → `settings/initialize`.
   Later runs: `X-Api-Key` from `seerr/settings.json` `main.apiKey`, only the Sonarr/Radarr check.
   **Jellyfin wizard** (step 7, fresh install only: `StartupWizardCompleted` false):
   `Startup/Configuration` (hu/HU metadata), `GET Startup/User` (creates the first user; the
   POST 404s without it), `POST Startup/User` (Transmission login), `Startup/RemoteAccess`,
   libraries (README's table, from `[routes]`; skip existing names, a duplicate would become
   "Movies2"), `Startup/Complete`.
   **Hardware transcoding** (every run): first `/dev/dri/renderD*` whose
   `/sys/class/drm/<node>/device/vendor` is AMD (0x1002) or Intel (0x8086); `sudo -u jellyfin
   /usr/lib/jellyfin-ffmpeg/vainfo --display drm --device <node>` → `jellyfin-hwaccel`: VLD
   profiles → `HardwareDecodingCodecs`, HEVCMain10/VP9Profile2 → the 10-bit flags, H.264 Enc →
   `EnableHardwareEncoding`; `POST System/Configuration/encoding` with `vaapi` + the device,
   only while `HardwareAccelerationType` is `none` (a choice made in Jellyfin stays).
   `arr = no` with a compose.yml: `compose down`, rename it `.off`; config kept.
10. **Daemon:** install `rss2transmission.py` as `~/.local/bin/rss2transmission` (755), units
   to `~/.config/systemd/user/` with `ReadWritePaths=` built from config.ini (state dir, watch
   dir, archive dir; paths under home written as `%h/...`, so the default config gives exactly
   the unit in the repo); create those dirs; `loginctl enable-linger $USER`; `systemctl --user
   enable --now rss2transmission.timer` (first run seeds state). `shadow.conf` only with
   `--shadow`; an existing `shadow.conf` is never removed (going live without `--catch-up`
   would add everything bookmarked since shadow start) — setup just says it's there.
10b. **Disk health:** `apt install smartmontools`; `templates/push` → `/usr/local/lib/torrent-box/push`
   (700, `@NTFY_URL@`), `smartd-notify` (755). The storage root's disk: `findmnt -no SOURCE`
   (a mount point only) → `lsblk PKNAME` → its `/dev/disk/by-id/{ata,scsi,usb,nvme}-*` name
   (`-d sat` on USB). A marked block in `/etc/smartd.conf` before the first `DEVICESCAN`
   (smartd skips a device it already has, "same identity"): `-a -o on -S on -n standby,24,q
   -s (S/../.././03|L/../01/./04) -W 4,50,55 -m <nomailer> -M daily -M exec smartd-notify`;
   original kept as `.before-torrent-box`; `smartmontools` enabled, restarted on change.
   **App updates** (`arr = yes`, `[setup] arr_auto_update` not `no`): `arr-update` +
   `torrent-box-arr-update.{service,timer}`; image ids per container before/after `pull` +
   `up -d`, the changed ones pushed (low), a failure pushed (high); otherwise the timer is
   disabled.
11. **Start page:** `dashboard.py` → `/usr/local/bin/torrent-box-dashboard` (root's: it runs
   with `CAP_NET_BIND_SERVICE`), `torrent-box-dashboard.service` → `/etc/systemd/system/`
   (`User=` you, `XDG_RUNTIME_DIR` for `systemctl --user`, read-only sandbox); restart only
   when either changed; not started if something else listens on port 80. Checks per request
   (cached 5 s, in parallel, 3 s timeout each): `systemctl show` for the system units and
   rss2transmission's timer/service, Transmission RPC `torrent-get` (counts, speeds, local
   errors), Jellyfin `/health`, the *arrs' `/api/vN/health` (key from config.xml; `/ping`
   without one), Seerr `/api/v1/status`, `tailscale status --json`, `statvfs`, /proc, hwmon.
   Links are built from the request's `Host` (validated) so they work by IP, name or tailnet.
   Disk health row: `smartmontools` active + the newest line of each fresh (< 2 h)
   `/var/lib/smartmontools/attrlog.*.ata.csv` (194/190 temperature, 5/197/198 raw > 0).
   Pushes: a thread checks every 60 s; a check at WARN or worse for 10 min → one push
   (`priority_problem`), back to OK → one more; storage/ncore never (rss2transmission and
   Prowlarr push those), prowlarr/disk only when DOWN (their own pushes cover the rest).
   Tile logos: the apps' own, from their install (Jellyfin `web/icon-transparent.*.png`,
   transmission-common's hicolor SVG) or served by the container (`Content/Images/logo.svg`,
   Seerr `os_icon.svg`); fixed sources only, cached, served at `/icon/<app>` with a CSP that
   blocks scripts in an SVG; none found → a glyph of ours on the app's colours.
   nCore row (with arr): `indexer` (enabled?) + `indexerstatus` (`disabledTill` in the future
   and no good page login since `mostRecentFailure` → needs a code; the form is only on the
   page then). `POST /ncore-code` does
   `ncore-code`'s PUT + `ApplicationIndexerSync` (copied: the program is installed alone);
   6–8 digits, one at a time, 5 per 15 min, rejected when `Origin` isn't the page's own host;
   303 back to `/#ncore`, the outcome shown in the row for 10 min. Nothing written to disk.
12. **Firewall:** if ufw is installed, active or not (ufw keeps rules while off, so a later
   `ufw enable` doesn't cut off SSH or the services) → SSH 22/tcp from the LAN (and on
   tailscale0) if openssh-server is installed; allow 51413 (tcp+udp) from anywhere; 9091/tcp (web UI /
   Remote GUI) and 80/tcp (start page), and from the LAN subnet only: Jellyfin 8096/tcp + 7359/udp (client discovery)
   + 1900/udp (DLNA plugin), or minidlna 8200/tcp + 1900/udp; Samba 445/tcp + wsdd 3702/udp
   and 5357/tcp, also from fe80::/10 like the share (if ufw has `IPV6=yes`); with Tailscale 41641/udp from anywhere and the services' TCP ports `in on
   tailscale0` (ufw itself skips rules that exist); with `arr`, 9696/8989/7878/5055 from the LAN
   (and on tailscale0). `ufw status` needs root, so a dry run
   goes by `/etc/ufw/ufw.conf`.
13. `rss2transmission -c ~/.config/rss2transmission/config.ini --test-notify`; print a summary
    (services active, timer next run, start page and web UI URLs, ntfy subscribe URL).

## systemd notes

- User units (no root): program at `~/.local/bin/rss2transmission`, units in
  `~/.config/systemd/user/`, linger enabled so they run while logged out.
- Sandbox: `ProtectSystem=strict`, `ProtectHome=read-only`, writable only the state dir, watch
  dir and archive dir (`ReadWritePaths=`; they must exist). **In user units on systemd 249
  these are silently ignored unless `PrivateUsers=yes` is set** — verified: without it home and
  /mnt/storage were writable; with it only the three dirs are, and files keep the user's
  ownership. setup.sh must template `ReadWritePaths` from config.ini.
- No `network-online.target` (doesn't exist in the user manager); `OnBootSec=2min` covers boot.
- `TimeoutStartSec=10min`: a hung run is killed instead of blocking the timer for good.
- A failed run (exit 1/2) shows in `systemctl --user --failed` until the next good run.

## Testing

- `python3 -m unittest` with no network: routing table vs. every filename in `old_torrents`
  (and vs. a port of the periodic.sh regex chain), feed parsing on the fixture, RPC client
  against a tiny fake server (409 session-id handshake, success, duplicate, auth failure),
  completion/first-run logic against fake `torrent-get` data.
- Live dry run on this box: `--dry-run` against the real feed and Transmission.
- `setup.sh`: unit tests for `setup_helper.py` (settings merge keeps unrelated keys / is
  idempotent / password handling, LAN → whitelist, INI and unit rewriting, folder list);
  `bash -n`; `./setup.sh --dry-run` on this box (detects the existing setup: only
  minidlna.conf would change — friendly_name + db_dir); a throwaway harness that sources
  setup.sh with all paths under /dev/shm and sudo/systemctl stubbed, running the real
  (non-dry) steps twice against the fake Transmission/ntfy server (second run changes
  nothing) plus the interactive prompts. Still to do: `shellcheck` (couldn't be run in the
  agent sandbox) and a trial run in a throwaway Ubuntu 22.04 VM/container (Transmission +
  minidlna start, timer fires, test push arrives).

## Cutover on this machine (needs explicit go-ahead)

Done 2026-10-17 except steps 3 and 6 (marked).

1. ✅ Shadow mode: compare `journalctl --user -u rss2transmission` "would add X to DIR"
   lines with `~/scripts/periodic_log` — same torrents, same folders, about the same time.
   Result: one real bookmark (hd_hun) — same file, same folder; ours ~3½ min earlier.
2. ✅ Stop + disable `flexget` → `~/.local/bin/rss2transmission --catch-up` (state was seeded at
   shadow start; anything bookmarked/finished since then was handled by the old setup, so
   re-seed to avoid duplicate adds and a burst of old "finished" pushes) → remove
   `~/.config/systemd/user/rss2transmission.service.d/shadow.conf` → `daemon-reload`.
   The timer is already enabled, so real runs start within 5 minutes.
3. ⏳ `sudo crontab -e` → remove the periodic.sh line (user; until then it only acts on
   manual drops in `~/torrents`).
4. ✅ Disable Transmission's `script-torrent-done` — done without sudo via RPC
   `session-set {"script-torrent-done-enabled": false}`; Transmission writes it to
   settings.json on its next clean stop. (`./setup.sh` sets the same key.)
5. ⏳ Watch for a few days: `journalctl --user -u rss2transmission`, pushes arriving.
6. ⏳ Then delete `~/.flexget-venv`, `~/.flexget`, `periodic.sh`, `torrent_done.sh`, and the
   leftovers of the first draft: `~/scripts/rss2transmission.py`,
   `~/.config/rss2transmission/feed-url` (now in secrets.ini).

Rollback at any point: disable the timer, re-enable `flexget`, restore the cron line, and
re-enable the done-script (RPC `session-set {"script-torrent-done-enabled": true}`).

## Milestones

1. ✅ Repo skeleton + `rss2transmission.py` (feed, routing, RPC, watch dir, state) + unit tests.
2. ✅ Completions, disk warning, ntfy notifier, first-run seeding, CLI flags.
3. ✅ systemd units; shadow mode (dry runs next to FlexGet) on this box.
4. ✅ `setup.sh` + templates (dry-run on this box, stubbed test runs); still to do: shellcheck
   and a test in a clean VM/container.
5. ✅ Cutover on this box (2026-10-17); ⏳ root-cron cleanup, then remove the old pieces.

## Not in scope (for now)

- Web UI, multiple feeds/trackers, filtering rules beyond category routing.
- Moving finished downloads (Transmission downloads straight into the final folder, as today).

## Side issue spotted (unrelated to this project)

`~/.msmtp.log`: mail addressed to `root` (likely cron job output) is rejected by Gmail
("not a valid RFC 5321 address"), so those mails are lost. Fix: `aliases /etc/aliases` in
`/etc/msmtprc` with `root: <address>` — or let it go once email is no longer used.
