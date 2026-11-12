# Family Frame

Keeps a photo frame page up to date inside Home Assistant. It downloads photos from an iCloud
shared album (location data stripped), and writes the calendar, weather and photo list the page
needs. The page is published at **`/local/frame/index.html`** (add `?view=slideshow` for photos only).

No access token needed: the add-on talks to Home Assistant through the Supervisor.

## Options

| Option | |
| --- | --- |
| `icloud_album` | iCloud shared album link (must open on `photos.icloud.com` with a download button). Empty = only photos you copy into `/config/www/frame/photos/` |
| `calendars` | Calendar entity IDs to list |
| `weather` | Weather entity (empty = no weather) |
| `days_ahead` | How many days of events to show |
| `refresh_minutes` | Calendar/weather/photo-list refresh (default 60) |
| `icloud_hours` | How often to check the album (default 24) |
| `idle_seconds`, `slide_seconds`, `black_minutes` | Slideshow defaults; `?idle=`, `?slide=`, `?black=` in the URL override them |
| `frame_folder` | Folder under `/config/www` (default `frame`) |

## Refresh now

Restart the add-on: it refreshes everything, including iCloud, on start. A script button:

```yaml
refresh_family_frame:
  alias: Refresh family frame
  sequence:
  - action: hassio.addon_restart
    data:
      addon: <the add-on's slug, e.g. xxxxxxxx_family_frame>
```

## Notes

- Everything in `/config/www` is reachable without logging in by anyone who knows the URL.
- Only photos named `ic_*.jpg` (from iCloud) are ever deleted by the add-on.
- The iCloud interface is undocumented and may change.
- The first start copies the page to `/config/www/frame/index.html`; every start overwrites it
  with the add-on's version.
