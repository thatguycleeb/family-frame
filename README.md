# Family Frame

An old Android tablet as a family wall display, driven by Home Assistant:

- **Home Assistant dashboard** full screen, in a small kiosk app.
- After a couple of minutes idle, a **photo slideshow** (with clock) from an **iCloud shared album**.
- After a few more minutes, the screen **goes black** and the backlight drops to minimum.
- While the slideshow is up, the tablet's **front camera detects motion**: it keeps the photos on and wakes them from black. A tap returns to the dashboard. (Motion never starts the slideshow early or delays it; only touches count on the dashboard.)
- A separate **calendar + weather page** for a relative's frame (same page, without `?view=slideshow`).

Built and tested on a Vodacom Power Tab 10 (Android 5.1, MediaTek MT8321, 1 GB RAM), but the
kiosk app runs on Android 5.1 and newer.

```
iCloud shared album ──(weekly / button)──┐
HA calendars + weather ──(hourly)────────┤ sync.py (in HAOS)
Photos dropped in via Samba ─────────────┘    │
                                              ▼
                     /config/www/frame/  →  https://<your-ha>/local/frame/
                        index.html, photos/, photos.json, events.json, weather.json
                                              ▲
             Tablet: kiosk app (HA dashboard → screensaver = frame page, camera motion)
```

## Pieces

| Path | What it is |
| --- | --- |
| `frame/index.html` | The frame page. Plain ES5 + flexbox so it runs on Android 5's old browser. Calendar/weather view, photo slideshow, black-out, wake on touch/motion. |
| `sync.py` | Runs inside Home Assistant OS (stdlib only). Downloads the iCloud album, strips EXIF/GPS, writes `photos.json`, `events.json`, `weather.json`. |
| `config.example.json` | Copy to `/config/frame-sync/config.json` and fill in. |
| `ha/` | `shell_command`, script (refresh button) and automations for Home Assistant. |
| `kiosk/` | Full-screen WebView kiosk app, adapted from [wesbos/photo-frame-dashboard](https://github.com/wesbos/photo-frame-dashboard) (MIT, see `kiosk/LICENSE-wesbos`): rebuilt for Android 5.1, plus a built-in screensaver and camera motion detection. |

## Frame page options

`https://<your-ha>/local/frame/index.html` plus:

| Option | Default | |
| --- | --- | --- |
| `view=slideshow` | – | Photos only (use as the screensaver URL) |
| `idle=120` | 120 | Seconds without touch before the calendar view switches to photos |
| `slide=12` | 12 | Seconds per photo |
| `black=5` | 5 | Minutes without touch/motion before the screen goes black (`0` = never) |

## Setup

### 1. Home Assistant (HAOS)

You need file access to `/config` (Samba share or SSH add-on).

1. Copy `frame/index.html` to `/config/www/frame/index.html` and create `/config/www/frame/photos/`.
   If `/config/www` did not exist before, restart Home Assistant once so `/local/` is served.
2. Copy `sync.py` to `/config/frame-sync/sync.py` and `config.example.json` to
   `/config/frame-sync/config.json`. Fill in:
   - `ha_token`: a long-lived access token (ideally from a dedicated, non-admin user).
   - `calendars`, `weather`: your entity IDs.
   - `icloud_album`: the album link (see below), or remove it to only use photos dropped in via Samba.
3. Add the snippets from `ha/` and restart Home Assistant.
4. Run the **Refresh family frame** script once.

Everything in `/config/www` is reachable **without logging in** by anyone who knows the URL.
That's why `sync.py` strips location data from photos, and why the token stays in
`config.json` (outside `www`).

### 2. iCloud shared album

Use a link from Photos that opens on `photos.icloud.com/shared/album/<key>` **with a download
button**; those links come with anonymous access, which `sync.py` uses (the same calls the iCloud
web page makes). Links without the download button don't allow anonymous listing.

This is an undocumented Apple API: it can change without notice. Photos you drop into
`/config/www/frame/photos/` (JPEG/PNG; not HEIC) are always shown too; only files the sync
downloaded (`ic_*.jpg`) are ever deleted by it.

Check who can add to the album: these links may allow anyone with the link to contribute.

### 3. Kiosk app on the tablet

Build (no Gradle; JDK 17 + Android build-tools 34 + platform 34):

```sh
cd kiosk
ANDROID_HOME=~/.local/share/android-sdk bash build.sh
adb install -r build/homehq.apk
```

Configure (saved on the tablet, survives reboots):

```sh
adb shell am start -n com.wesbos.homehq/.MainActivity \
  --es url "https://<your-ha>/" \
  --es screensaver_url "https://<your-ha>/local/frame/index.html?view=slideshow\&black=5" \
  --ei screensaver_after 120 \
  --ez motion true --ei motion_sensitivity 60 \
  --ez boot true
```

- `motion_sensitivity` 1–100: higher reacts to smaller movement.
- **Adaptive brightness** (the tablet has no light sensor, so the front camera is used as a light
  meter): `--ez calibrate_light true` sets the current room light as 100%; screen brightness then
  follows the room between `--ei light_min 15` (%) and 100%. `--ez light_adaptive false` turns it
  off. The camera's exposure is locked for this, so motion detection is less sensitive in a dark room.
- `--ez motion false` turns the camera off; `--es screensaver_url ""` turns the screensaver off.
- Hold two fingers on the screen for 1.5 s for the kiosk menu (settings, Wi-Fi, reload).
- Log in to Home Assistant once in the app (it keeps its own cookies) and tick *Keep me logged in*.

On the tablet: Developer options → **Stay awake** (while charging), and keep Wi-Fi on during sleep.

### Old Android notes

- Android 5.1's stock Chrome/WebView (v43) is too old for Home Assistant's frontend. Sideloading
  **Chrome 95** and **Android System WebView 95** (the last versions for Android 5, `armeabi-v7a`)
  makes the HA dashboard work.
- No Google account is needed; install APKs with `adb install`.
- Network ADB (`adb tcpip 5555`) needs USB again after every reboot on an unrooted device.

## Credits

Kiosk app: [wesbos/photo-frame-dashboard](https://github.com/wesbos/photo-frame-dashboard) by
Wes Bos, MIT licensed. Changes here: Android 5.1 support, screensaver overlay, camera motion
detection (`MotionDetector.java`), backlight dimming bridge (`HomeHQ.dim()`).
