# torrent-box

Torrent box in one install: **transmission-daemon** + **rss2transmission**, a small program
that loads torrents from an nCore bookmark RSS feed into Transmission, filed into folders by
nCore category, + a media server for the disk: **Jellyfin** or, on small/old hardware,
**minidlna**, + optionally a **Samba** share of the disk, a `<box>.local` name on the LAN
(**avahi**), **Tailscale** for remote access, and **Prowlarr, Sonarr, Radarr, Seerr** (and
**Whisparr** with [Desireerr](https://github.com/vibesclauder/desireerr)) to search nCore and
request movies and series, + a
[start page](#start-page) at `http://<box>/` with links to all of it and its health.

Status: work in progress, see [PLAN.md](PLAN.md).

## Installation

On a fresh Ubuntu 22.04+ / Debian 12+ box, as your normal user (sudo is used where needed):

```sh
git clone https://github.com/vibesclauder/torrent-box.git && cd torrent-box
./setup.sh --dry-run     # optional: show what it would do, change nothing
./setup.sh
```

It asks for what it doesn't know yet (choices are menus: arrow keys or j/k and Enter, the
default already selected; typed answers come with the default filled in, Enter takes it):
- the storage disk: mount point (default `/mnt/storage`) and, if it isn't in `/etc/fstab`
  yet, which disk to use, from a list of unmounted filesystems and blank disks (biggest
  first). A filesystem is used as it is. A blank disk (new, or wiped: no partition table,
  no filesystem) gets one partition, formatted ext4, once you type its name to confirm.
  A disk with anything on it is never offered for formatting (`sudo wipefs -a /dev/sdX`
  blanks one, if you're sure). It goes into `/etc/fstab` with `nofail`,
- the media server: `jellyfin`, `minidlna` or `none` (see [Media server](#media-server)),
- whether to share the disk with Samba, and if so a Samba password (see
  [Samba share](#samba-share)),
- whether to announce the box on the LAN as `<box>.local` (default yes; see
  [LAN name](#lan-name-avahi)),
- whether to set up Tailscale, and then to log in with the link it prints (see
  [Remote access](#remote-access-tailscale)),
- whether to set up search and requests: Prowlarr, Sonarr, Radarr and Seerr in Docker
  (default no; see [Search and requests](#search-and-requests-prowlarr-sonarr-radarr-seerr)),
  and with them whether to add Whisparr and Desireerr (default no; asked once, also on a box
  that already has the others),
- your internet download speed: setup measures it (about 10 s) and offers that as the
  default; enter your plan's speed if the box is on Wi-Fi or a 1 Gbit port. Saved as
  `[setup] download_mbps`; it sizes Transmission's write cache and peer limits,
- your nCore bookmark RSS URL,
- a Transmission username and password. The same login is used by the web UI
  (`http://<box>:9091/`), Transmission Remote GUI and rss2transmission.

and then:
- installs transmission-daemon, python3, curl, the chosen media server, and Samba (with
  wsdd), avahi, Tailscale and Docker with the apps (those that are wanted),
- writes `~/.config/rss2transmission/secrets.ini` (mode 600) with a freshly generated ntfy
  topic (it prints the topic: subscribe to it in the ntfy phone app), and with
  `own_ntfy = yes` runs an ntfy server on the box ([below](#own-ntfy-server)),
- creates `config.ini` from `config.example.ini` and the download folders from `[routes]`,
- merges its keys into Transmission's `settings.json` (your other settings are kept):
  partial downloads go to `<storage>/.incomplete` and are moved into their folder when done
  (a rename, same disk), files are allocated in full up front (no fragmentation), the
  cache and peer limits fit your speed, and Transmission opens its peer port (51413) on
  the router by UPnP / NAT-PMP, so peers can connect in and the torrents actually seed
  (if your router has UPnP off, forward 51413 TCP+UDP to the box by hand), and DHT, PEX and
  local peer discovery are off: nCore's torrents are private and never use them, so this only
  keeps a public torrent added by hand out of the public swarm,
  sets up the media server and the Samba share (below), installs systemd drop-ins so
  Transmission, the media server and Samba wait for the disk,
  lets Transmission write to the disk under Ubuntu's AppArmor profile (a rule in
  `/etc/apparmor.d/local/transmission-daemon`),
- installs rss2transmission and its systemd timer (user units; linger on), the
  [start page](#start-page), opens the ports in ufw (LAN-only for the web UIs, the media
  server, Samba, mDNS and SSH; also while ufw is off, so `sudo ufw enable` later doesn't lock
  you out), and sends a test push.

Re-running is safe: existing config/secrets are kept, it only asks for missing values, and
system files are only rewritten (and services only restarted) when something changes.
`./setup.sh --help` lists the environment variables for a non-interactive install
(`STORAGE_ROOT`, `STORAGE_UUID`, `STORAGE_DISK`, `MEDIA_SERVER`, `SAMBA`, `SAMBA_PASSWORD`,
`AVAHI`, `TAILSCALE`, `TS_AUTHKEY`, `ARR`, `WHISPARR`, `STASHDB_KEY`, `NCORE_USER`,
`NCORE_PASSWORD`, `NCORE_2FA`, `JELLYFIN_USER`, `JELLYFIN_PASSWORD`, `DOWNLOAD_MBPS`,
`FEED_URL`, `RPC_USER`, `RPC_PASSWORD`, `LAN_SUBNET`). `--shadow` also installs
`systemd/shadow.conf` (dry runs only, for a trial next to an old setup).

## Start page

`http://<box>/` (port 80, so the box's name is enough) links to the web apps that are set up
(Jellyfin or minidlna, Seerr, Transmission, Sonarr, Radarr, Whisparr, Desireerr, Prowlarr),
each with its own logo (taken from the app itself, so nothing is copied into this repo), and
shows whether each is healthy, plus the storage disk (free space), the nCore feed
(rss2transmission's last run), the Samba share, Tailscale and the box itself (uptime, load,
memory, CPU temperature). Anything that needs a look is listed at the top, and the tab's
icon turns orange or red. It refreshes itself every 20 s (not while you type in it);
`http://<box>/status.json` has the same data for scripts.

It also **pushes** what stays wrong for 10 minutes (an app down, the feed failing, Tailscale
logged out, the system disk full...), once, and again when it's fine. Not what already
pushes on its own: the storage disk (rss2transmission), Prowlarr's health and nCore (Prowlarr),
the disk's health (smartd); for those only when the app itself is down.

With Prowlarr, the "nCore search" row says whether Prowlarr is logged in to nCore, and has a
box for a fresh **2FA code** only while one is needed (its session has ended, or the indexer is
switched off; at other times `setup_helper.py ncore-code` does the same). It does what `setup_helper.py ncore-code` does: saves the code in the nCore indexer,
which makes Prowlarr log in, and syncs it to Sonarr and Radarr; the row then says whether the
code worked. Since anyone on the LAN can open the page, it takes at most 5 codes per 15
minutes (nCore's login isn't hammered) and only from its own page (another web site can't
post codes to it); a code is useless without your password anyway.

The links use the address you opened the page with, so they work by LAN IP, by name and
through Tailscale alike. The page shows no passwords or keys, but anyone on the LAN (and your
tailnet) can see it, like the apps' login pages. Its health comes from each app's own health
check (Prowlarr's tells you when nCore's session has expired) and from systemd.

It is `dashboard.py`, installed as `/usr/local/bin/torrent-box-dashboard` and run by the
system unit `torrent-box-dashboard` as your user (it reads your config and the apps' API
keys), with only the right to use port 80. Logs: `journalctl -u torrent-box-dashboard`. If
another program already uses port 80, setup leaves it alone and doesn't start the page.

## Media server

The choice is saved as `[setup] media_server` in `~/.config/rss2transmission/config.ini`. To
switch, change it there and run `./setup.sh` again: the old one is stopped and disabled (not
uninstalled). On a box set up before this option existed, setup keeps whichever is installed.

- **jellyfin** (default): a media library with posters, "continue watching", users, subtitles
  and apps for Android/Google TV, Fire TV, LG, Samsung, iOS, Android and browsers; transcodes
  what a device can't play. Needs a 64-bit CPU (amd64/arm64) and a few GB of RAM. Installed
  from Jellyfin's own apt repo; the `jellyfin` user is added to the GPU's render group for
  hardware transcoding. Web UI on `http://<box>:8096/`.
- **minidlna**: a lightweight DLNA server; TVs and players browse the folders as they are.
  For boxes too small for Jellyfin (setup suggests it below 3 GB RAM, or when Jellyfin has no
  packages for the system). Status page on `http://<box>:8200/`.
- **none**: no media server (e.g. you share the disk with Samba instead).

On a fresh install setup runs Jellyfin's first-run wizard: the admin is the Transmission login,
and the libraries below are created from the `[routes]` folders (a Jellyfin that's already set
up is left alone). By hand: open `http://<box>:8096/`, create the admin user, and add them
(a library can have several folders, and its metadata language is set per library):

| Library | Type | Folders |
|---|---|---|
| Filmek | Movies | `MOVIES/HD/HU`, `MOVIES/SD/HU` |
| Movies | Movies | `MOVIES/HD/EN`, `MOVIES/SD/EN` |
| Sorozatok | Shows | `SERIES/HD/HU`, `SERIES/SD/HU` |
| Series | Shows | `SERIES/HD/EN`, `SERIES/SD/EN` |
| Music | Music | `MUSIC` |

Series are filed into a folder per show by rss2transmission (see below), which is the layout
Jellyfin expects; it reads season and episode from the file names. Series downloaded before
that (release folders directly in `SERIES/...`) need moving into a folder per show once.

**Hardware transcoding:** with an AMD or Intel GPU, setup switches Jellyfin to VAAPI on its
render node and ticks the codecs the GPU decodes (it asks the GPU with jellyfin-ffmpeg's
`vainfo`, as the `jellyfin` user), so converting video for a TV, a browser or a phone on
mobile data costs the CPU little. It does that only while Jellyfin's acceleration is off:
whatever you pick in Dashboard → Playback → Transcoding stays (e.g. QSV on Intel). It signs
in with the Transmission login, or `JELLYFIN_USER`/`JELLYFIN_PASSWORD` if that isn't a
Jellyfin admin. NVIDIA (NVENC) needs NVIDIA's driver first; set it in the dashboard yourself.
TVs that only speak DLNA: install the DLNA plugin (Dashboard → Plugins).

## Disk health

Everything is on one disk, so setup installs **smartmontools** and has smartd watch the
storage disk's own health readings (SMART) every 30 minutes, run a short self-test every
night (03:00) and a long one on the 1st of the month (04:00; it reads the whole disk, many
hours on a big one, in the background). When the disk reports trouble (sectors it had to
move or can't read, a failed self-test, 55 °C or more) you get an **urgent push**, repeated
daily while it lasts: time to copy off what you can't download again. Details:
`sudo smartctl -a /dev/disk/by-id/<the disk>`.

Our line is a marked block in `/etc/smartd.conf` (the original is kept as
`smartd.conf.before-torrent-box`), before its `DEVICESCAN`, which keeps watching any other
disks. The pushes go through `/usr/local/lib/torrent-box/push` (root-only, it holds the ntfy
topic). The start page shows the disk's temperature and whether it has bad sectors.

## Samba share

With `[setup] samba = yes` the whole storage disk is shared as `storage`, read-write, for
your user only, and only on the LAN:

- Windows: `\\<box>\storage` (the box also shows up under Network, via wsdd),
- Mac: Finder → Go → Connect to Server → `smb://<box>/storage`,
- Android / iOS: any file manager or player with SMB support (e.g. the iOS Files app, VLC).

Samba has its own passwords: setup asks for one the first time (`sudo smbpasswd $USER`
changes it later). Files you add or move keep working for Transmission and the media server:
the folders' setgid group and the share's masks keep them group-writable and readable. smbd
only starts once the disk is mounted, so nothing can be copied onto the system disk by
mistake. The share lives in `/etc/samba/torrent-box.conf`, included from the end of
`/etc/samba/smb.conf`; set `samba = no` and re-run setup to remove it again.

## LAN name (avahi)

With `[setup] avahi = yes` (the default) the box runs avahi-daemon, so every device on the
LAN reaches it as `<box>.local` (`http://<box>.local/`, `\\<box>.local\storage`) even when
the router's DNS doesn't know the name and Tailscale is off. Linux, macOS, iOS, Android and
Windows 10/11 all resolve `.local` names out of the box.
`/etc/avahi/services/torrent-box.service` also announces the start page and, with Samba, the
share, so Finder and Linux file managers list the box under Network without typing an
address (Windows' Network view uses wsdd instead, which comes with the Samba share). Set
`avahi = no` and re-run setup to stop announcing those; avahi itself stays installed.

## Remote access (Tailscale)

With `[setup] tailscale = yes`, setup installs [Tailscale](https://tailscale.com) from its own
apt repo and runs `tailscale up` once: open the link it prints and log in (a free account is
enough), or pass `TS_AUTHKEY` to join without a browser. Then install the Tailscale app on
your phone/laptop, log in with the same account, and from anywhere use the box's name instead
of its LAN address:

- the start page: `http://<box>/`, with links to everything else,
- Transmission: `http://<box>:9091/` (also in Transmission Remote GUI),
- Jellyfin: `http://<box>:8096/` (as the server address in the apps),
- the share: `\\<box>\storage` / `smb://<box>/storage`.

Nothing needs forwarding on the router (Tailscale opens its own UDP port by UPnP when the
router allows it, for direct connections). What setup changes for it: Transmission's
`rpc-whitelist` gets the tailnet's addresses (`100.64.*.*` to `100.127.*.*`), the Samba
share allows them too, and if ufw is active the services are allowed in on `tailscale0`
(plus 41641/udp for direct connections). Transmission also accepts requests by name
(`rpc-host-whitelist` = `<box>,<box>.*`), which it otherwise refuses (HTTP 421) for anything
but IP addresses.

Worth doing once in the Tailscale admin console: **disable key expiry** for the box (Machines →
… → Disable key expiry), or it drops off the tailnet after 180 days until someone logs in
again. Later runs of setup never log out, re-login or `tailscale down`; with
`tailscale = no` Transmission and the share stop accepting tailnet addresses (Tailscale stays
installed and connected; the ufw rules stay too, `sudo ufw status numbered` and
`sudo ufw delete N` remove them). Extras not set up by torrent-box: HTTPS names via
`tailscale serve`, exit node, subnet router.

## Own ntfy server

All pushes (rss2transmission, the start page, the disk, Prowlarr, Seerr, the weekly app
update) go through [ntfy](https://ntfy.sh). On the public ntfy.sh the topic is the only
password, and the operator can read every push: torrent names, requests, nCore problems.
With `[setup] own_ntfy = yes` (asked once, default yes with Tailscale) setup runs ntfy on the
box instead, from the distro's own `ntfy` package:

- it listens on port **2586** (80 is the start page's); if ufw is active, only the LAN and the
  tailnet get in. Everything on the box pushes to `http://localhost:2586` (`[ntfy] server` in
  config.ini; setup keeps it in step, and sets it back to ntfy.sh when you switch it off);
- its settings are a systemd drop-in, `/etc/systemd/system/ntfy.service.d/torrent-box.conf`
  (ntfy reads `NTFY_*` variables), so the package's `/etc/ntfy/server.yml` stays untouched;
- messages are kept for **3 days**: a phone that was off the tailnet gets what it missed when
  it's back.

In the ntfy app (Android): **+ → Use another server**, `http://<box>.<tailnet>.ts.net:2586`
(the summary at the end of setup prints it), the same topic as before. Keep Tailscale on on
the phone (Always-on VPN in Android's settings), also at home: then one subscription works
everywhere. The app keeps a connection open for this ("instant delivery"), which it shows
as a quiet permanent notification; you can hide that one in Android's notification
settings. Without Tailscale, subscribe to `http://<LAN address>:2586`: pushes then only reach
the phone at home. The iPhone app isn't covered: it gets self-hosted pushes only through a
relay on ntfy.sh, which torrent-box doesn't set up.

The start page has a **Notifications** row for it (no push when it's down: it would have to
go through it). `own_ntfy = no` stops and disables the server; the package stays.

## Search and requests (Prowlarr, Sonarr, Radarr, Seerr)

With `[setup] arr = yes`, setup installs Docker (Ubuntu's `docker.io` + `docker-compose-v2`)
and runs four apps from `~/.local/share/torrent-box/arr/compose.yml` (their settings live next
to it):

- **Seerr** `http://<box>:5055/`: search any movie or series and request it, from a phone,
  a browser or the TV. Users sign in with their Jellyfin login.
- **Sonarr** `:8989` (series) and **Radarr** `:7878` (movies): keep the wanted list, search
  nCore through Prowlarr, pick a release by your quality/language rules, send it to
  Transmission, and when it's done hardlink it, renamed, into the library
  (`SERIES/HD/HU/<Show>/Season 01/...`, `MOVIES/HD/HU/<Movie (year)>/...`). The download
  itself stays in `downloads/series` or `downloads/movies` and keeps seeding; a hardlink takes
  no extra space. Requests nCore doesn't have yet stay open and are grabbed when they appear.
- **Prowlarr** `:9696`: logs in to nCore and serves its search to Sonarr and Radarr. Its own
  search page can also send anything (music, games...) straight to Transmission (into `MISC`).

Setup also does what you'd otherwise click through, with the Transmission login everywhere:

- **logins:** Prowlarr, Sonarr and Radarr get a login page, from every address (the apps
  reach each other with API keys). Seerr's admin is your Jellyfin admin account.
- **Transmission** as the download client in all three; torrents keep seeding after import.
- **Prowlarr ↔ Sonarr/Radarr**, so indexers are synced to them, and the HD series/movie
  folders from `[routes]` as their root folders (Seerr requests go to the Hungarian ones).
- **nCore** in Prowlarr, with your nCore login (asked once, kept in `secrets.ini`). No seed
  limits: Transmission keeps seeding, which meets nCore's rule (ratio 1.0, or 48 h + 0.4 h
  per GB).
- **HUN:** a custom format in Sonarr and Radarr matching `HUN` in the release name, scored
  +100 in every quality profile, so a Hungarian release wins over an equal English one.
- **Seerr:** signed in with Jellyfin, movie and show libraries enabled, Sonarr and Radarr
  added (HD-1080p profile if there is one). If that fails, setup warns and Seerr's own wizard
  does it.
- **Prowlarr's health alerts** (e.g. nCore not logging in) are pushed to your ntfy topic.
- **Radarr's profiles take any language** (its default, the original language only, turns
  down every Hungarian release of a foreign film); the HUN format still ranks Hungarian first.
  Set once, so a language you pick later stays.
- **Grabs** (a torrent Sonarr, Radarr or Prowlarr's search page sends to Transmission) are
  pushed too, at low priority: the "added" push for them. rss2transmission's "Downloaded"
  push covers them already, since it watches all of Transmission.
- **Requests in Seerr** are pushed to your ntfy topic: one waiting for your approval (users
  you import from Jellyfin may request, but you approve: Seerr → Requests, or the request's
  page), one approved by itself (yours, or a user you let auto-approve), one that failed to
  reach Sonarr/Radarr, and an issue someone reported. Seerr's application URL is set to
  `http://<box>:5055` (if it had none) so its pushes link back to it. Set up once: the
  types you pick later in Seerr (Settings → Notifications → ntfy) stay, and so does
  switching it off; setup only keeps the server and topic in step with `secrets.ini`.

**nCore 2FA:** Prowlarr can only store a 2FA code, not make new ones. Setup asks for a current
code when it adds nCore, and Prowlarr logs in with "stay logged in". When that session ends,
Prowlarr can't log in again and you get a push from ntfy. Then enter a fresh code on the
[start page](#start-page) (`http://<box>/`, the "nCore search" row), or on the box:

```sh
~/torrent-box/setup_helper.py ncore-code    # asks for a fresh code, logs in, syncs Sonarr/Radarr
```

(or in Prowlarr: Indexers → nCore → edit, a fresh code in the 2FA field, Save). Once nCore
has logged in, setup never re-saves the indexer, so it won't overwrite your code. If that
first login failed (e.g. the code had expired), the next setup run asks for a fresh code and
switches nCore on with it. Don't delete and re-add nCore in
Prowlarr's web page: a login made while *adding* an indexer isn't kept (Prowlarr only stores
the session of a saved indexer), so the first search would log in again with the old code.
Setup adds it switched off and then switches it on with the code for that reason.

Left to you: the quality profiles (Sonarr/Radarr → Settings → Profiles; the HUN score is
yours to change), importing existing series/movies (Library Import), and Seerr's users
(Users → Import Jellyfin users; request limits, approval). A setting setup made that you
change in the web pages is put back on the next run only for the connections above
(download client, applications, root folders); logins, HUN, nCore and Seerr are set up once.
A new Transmission password reaches all three apps (their login and download client) on the
next setup run: setup keeps a salted hash of the login it gave them in
`~/.local/share/torrent-box/arr/torrent-box-state.json`, since the apps don't show it back.

**Whisparr and Desireerr** (`[setup] whisparr = yes`, only with `arr = yes`): Whisparr
`:6969` does Radarr's job for StashDB scenes (v3, hotio's `ghcr.io/hotio/whisparr:v3` image;
there's no linuxserver one), and [Desireerr](https://github.com/vibesclauder/desireerr) `:5056` is
its Seerr: browse and search StashDB, request scenes, approve other users' requests, follow
performers and studios. Setup connects Whisparr like Radarr: the login, Transmission (with
its own folder under `downloads/`), Prowlarr (which syncs it the indexers for its
categories) and those categories' folders from `[routes]` as root folders (videos only).
Desireerr comes from its own repo: setup clones it to `~/desireerr`, fast-forwards it on
each run (not if it has local changes) and runs it as a user unit
(`journalctl --user -u desireerr`). Its admin is the login all the apps share (and follows
it when it changes). It browses StashDB with your API key (stashdb.org, your profile):
setup takes it from Whisparr's StashDB import list if there is one, otherwise asks once (or
`STASHDB_KEY`), and keeps it in `secrets.ini` as `stashdb_key`. Skipped, Desireerr asks for
it itself. With Jellyfin, setup makes it a Jellyfin API key named Desireerr (Dashboard, API
Keys), for the Watch button on downloaded scenes. Desireerr's pushes go to this box's ntfy
topic (it reads it from the config). Both get a tile on the start page. Set to `no` (and a setup run) removes
Whisparr's container and its Prowlarr application and stops Desireerr; Whisparr's settings
stay in `~/.local/share/torrent-box/arr/whisparr`, Desireerr's checkout and data stay too.

Using it (none of this is set up by torrent-box):

- **Scenes come from [StashDB](https://stashdb.org).** Request them in Desireerr, or let its
  saved searches request new ones. Favourites set in Desireerr are only a filter there.
  Leave Whisparr's StashDB import list (Settings → Import Lists) off: on, it adds every
  performer and studio you favourite to Whisparr, followed, and downloads their scenes.
- **Mind the "after" date when following in Whisparr** (Desireerr always sets it to
  today, and older scenes can still be requested one by one). Monitoring a performer or
  studio with no date adds its whole back catalogue, monitored, so all of it is wanted:
  thousands of scenes for a busy one. With the date set to today, older scenes aren't added
  at all and the page stays empty until new ones come out. Worse, each skipped scene goes on
  the exclusion list (Settings → Import Lists → List Exclusions, reason
  "performerAfterDate" / "studioAfterDate"), and excluded scenes are never added later, even
  with the date cleared.
  To browse the back catalogue but only want new scenes: monitor without a date, wait a
  minute for the page to fill, unmonitor those scenes, then set the date. If a page stayed
  empty, delete those exclusions first.
- **What gets downloaded:** a scene requested in Desireerr is searched for right away. Any
  other wanted scene is grabbed when it shows up in nCore's feed (checked every 30 minutes);
  in Whisparr, use the scene's search button (or the interactive search, where "Unknown
  Movie" rejections are just the other results nCore returned).
- **Files:** new downloads are filed as
  `<root folder>/scenes/<Studio>/<Series>/<date> - <title>`.
- **In Jellyfin:** a *Movies* library with Whisparr's root folders, with the TheMovieDb and
  Open Movie Database downloaders and image fetchers unticked (they'd match scene folders to
  real films). Titles, performers and posters come from Whisparr: Settings → Metadata →
  Kodi (XBMC) / Emby, with "Use Movie.nfo". To keep it private, untick the library in each
  other user's access (Users → Access) and leave it out of Seerr.

**On the TV (LG):** Seerr has no TV app; request from the phone (open Seerr, "Add to home
screen") and watch in Jellyfin as usual. To search and request on the TV itself, two things to
try: the [Jellyfin Enhanced](https://n00bcodr.github.io/Jellyfin-Enhanced/) plugin adds Seerr
search to Jellyfin's web interface, which the LG app shows (its docs don't list webOS, so test
it); or [Moonfin](https://github.com/Moonfin-Client/Smart-TV), a separate Jellyfin client for
LG and Samsung TVs (not in LG's store: the `.ipk` is sideloaded, e.g. with the webOS CLI's
`ares-install` or webOS Homebrew) with Seerr search built in, via its own server plugin.

**Updates:** every Monday around 04:30 `torrent-box-arr-update.timer` pulls the apps' newest
images and restarts the ones that got one, then pushes which (low priority), or that the
update failed. A box that was off then catches up when it starts. `[setup] arr_auto_update =
no` (and a setup run) switches it off; then update by hand:
`sudo docker compose -f ~/.local/share/torrent-box/arr/compose.yml pull` and the same with
`up -d`. Right away: `sudo systemctl start torrent-box-arr-update`; what it did: `journalctl
-u torrent-box-arr-update`. With `arr = no` setup stops the apps (their config stays for
switching back on).

rss2transmission keeps working next to them; bookmark only what Sonarr/Radarr don't handle
(or stop bookmarking series and movies), so the same release isn't downloaded twice. With
`arr = yes` it makes no show folders in the HD series folders (`hdser_hun`, `hdser`): those
are Sonarr's, and Sonarr takes over any file it finds in one of its show folders, deleting it
on an upgrade even while it's still seeding. Bookmarked HD series land in the category folder
itself, which Sonarr leaves alone. The same goes for Library Import: series folders still
seeding are better left out of it.

## rss2transmission

One run:
1. checks the storage disk is mounted (refuses to add anything otherwise),
2. fetches the feed; every torrent not seen before is downloaded, routed by its
   `[nCore][category]` filename prefix (see `[routes]` in `config.example.ini`), added to
   Transmission over RPC, and archived. Series get a folder per show inside their category's
   folder, named from the release name up to the season (`The.Last.of.Us.S02E03.1080p...` →
   `SERIES/HD/HU/The Last of Us/`); an existing folder for the show is reused even if you
   renamed it (case and punctuation don't matter, nor a missing `(year)`; another year is
   another show). See `show_folders`,
3. does the same for `.torrent` files dropped into the watch dir by hand,
4. asks Transmission which downloads finished since the last run,
5. checks free space on the storage disk,
6. pushes a summary to your phone via [ntfy](https://ntfy.sh): torrents added, downloads
   finished, and (at most daily) disk-full / disk-not-mounted warnings.

Torrents are only marked seen once Transmission accepted them, and finished downloads only
count as announced once ntfy accepted the push, so anything that failed is retried on the
next run. The first run only records what's already in the feed and already finished.

Python 3.10+, standard library only.

```sh
# config
mkdir -p ~/.config/rss2transmission
cp config.example.ini ~/.config/rss2transmission/config.ini
install -m 600 secrets.example.ini ~/.config/rss2transmission/secrets.ini   # then fill it in

# check notifications reach your phone (subscribe to your ntfy_topic in the ntfy app)
./rss2transmission.py --test-notify

# try it without changing anything
./rss2transmission.py --dry-run

# one real run (state: ~/.local/state/rss2transmission/state.sqlite)
./rss2transmission.py
```

Run every 5 minutes with systemd (user units, no root needed; `setup.sh` does this):

```sh
install -m 755 rss2transmission.py ~/.local/bin/rss2transmission
install -m 644 systemd/rss2transmission.{service,timer} ~/.config/systemd/user/
mkdir -p ~/.local/state/rss2transmission ~/torrents ~/old_torrents   # sandbox needs them
loginctl enable-linger "$USER"        # keep running while logged out
systemctl --user daemon-reload
systemctl --user enable --now rss2transmission.timer
journalctl --user -u rss2transmission -f
```

To trial it next to an existing setup first, also install `systemd/shadow.conf` as
`~/.config/systemd/user/rss2transmission.service.d/shadow.conf` (dry runs only).

Options: `--dry-run`, `--catch-up` (mark the current feed as seen and finished downloads as
announced), `--test-notify`, `-c CONFIG`, `--state PATH`.
Exit status 0 ok, 1 errors this run, 2 bad config.

## Tests

```sh
python3 -m unittest discover -s tests
```

No network needed: the tests run against a fake nCore + Transmission server on localhost.
`setup_helper.py` (the testable parts of `setup.sh`: settings.json merge, LAN detection,
unit/INI rewriting) is covered too.
