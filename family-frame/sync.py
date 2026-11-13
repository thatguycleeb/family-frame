#!/usr/bin/env python3
"""Refresh the family frame's data files from Home Assistant + the photos folder.

Writes into out_dir (served by HA at /local/frame/):
  photos/ic_*.jpg  photos pulled from the iCloud shared album (location data stripped)
  photos.json    list of image files in out_dir/photos (album photos + any dropped in via Samba)
  events.json    upcoming events from the configured HA calendars
  weather.json   current weather + 5-day forecast from the HA weather entity

  settings.json  slideshow defaults (idle / slide / black) for the frame page

Standard library only. Two ways to run it:
  * As the "Family Frame" Home Assistant add-on (options in /data/options.json, talks to Home
    Assistant through the Supervisor, loops forever with --daemon).
  * Standalone, e.g. from a shell_command: config.json next to this script (see
    config.example.json); pass --icloud to also download the iCloud album.
"""
import json, os, re, shutil, sys, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIONS = "/data/options.json"  # present only when running as an add-on
ADDON = os.path.exists(OPTIONS)


def load_config():
    if ADDON:
        o = json.load(open(OPTIONS))
        return {
            "out_dir": "/homeassistant/www/" + (o.get("frame_folder") or "frame").strip("/"),
            "icloud_album": (o.get("icloud_album") or "").strip(),
            "ha_url": "http://supervisor/core",
            "ha_token": os.environ.get("SUPERVISOR_TOKEN", ""),
            "calendars": [c for c in o.get("calendars", []) if c],
            "weather": (o.get("weather") or "").strip(),
            "days_ahead": o.get("days_ahead", 30),
            "refresh_minutes": o.get("refresh_minutes", 60),
            "icloud_hours": o.get("icloud_hours", 24),
            "idle": o.get("idle_seconds", 120),
            "slide": o.get("slide_seconds", 12),
            "black": o.get("black_minutes", 5),
            "frames": o.get("frames", []),
        }
    return json.load(open(os.path.join(HERE, "config.json")))


def frame_profiles():
    """The main frame (top-level options, at /local/frame/) plus one profile per extra frame
    (own sub-folder /local/frame/<name>/, own calendars/weather/timings; empty = same as main).
    Photos are shared: they stay in the main folder."""
    main = {"name": "main", "folder": OUT, "photos_base": "",
            "calendars": CFG.get("calendars", []), "weather": CFG.get("weather", ""),
            "idle": CFG.get("idle"), "slide": CFG.get("slide"), "black": CFG.get("black")}
    frames = [main]
    for f in CFG.get("frames", []):
        name = re.sub(r"[^a-z0-9_-]", "", (f.get("name") or "").lower())
        if not name:
            continue
        frames.append({
            "name": name, "folder": os.path.join(OUT, name), "photos_base": "../",
            "calendars": [c for c in f.get("calendars", []) if c] or main["calendars"],
            "weather": (f.get("weather") or "").strip() or main["weather"],
            "idle": f.get("idle_seconds") or main["idle"],
            "slide": f.get("slide_seconds") or main["slide"],
            "black": main["black"] if f.get("black_minutes") is None else f["black_minutes"]})
    return frames


CFG = load_config()
OUT = CFG["out_dir"]
PHOTOS = os.path.join(OUT, "photos")
os.makedirs(PHOTOS, exist_ok=True)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def write_json(name, data, folder=None):
    """Write atomically so the tablet never reads a half-written file."""
    path = os.path.join(folder or OUT, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def ha_request(path, body=None):
    req = urllib.request.Request(
        CFG["ha_url"].rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {CFG['ha_token']}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ── iCloud shared album (new-style photos.icloud.com/shared/album/<key> links) ──
# Files downloaded from the album are named "ic_<id>.jpg" so they can be told apart from
# photos dropped in manually; only ic_ files are ever deleted by the sync.
ICLOUD_PREFIX = "ic_"
CK = "/database/1/com.apple.photos.cloud/production"


def ck_post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "text/plain", "Origin": "https://photos.icloud.com", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def icloud_album(url):
    """Return {file_id: download_url} for every photo in the album."""
    key = url.rstrip("/").split("/")[-1]
    res = ck_post(f"https://ckdatabasews.icloud.com{CK}/public/records/resolve?remapEnums=true&sharing_url_key={key}",
                  {"shortGUIDs": [{"value": key}]})["results"][0]
    access = res.get("anonymousPublicAccess")
    if not access:
        raise RuntimeError("album link doesn't allow anonymous access (share it so the link offers a download button)")
    qs = urllib.parse.urlencode({"remapEnums": "true", "sharing_url_key": key, "publicAccessAuthToken": access["token"]})
    base = access["databasePartition"].replace(":443", "") + CK + "/shared/records/query?" + qs
    body = {"query": {"recordType": "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted",
                      "filterBy": [{"fieldName": "direction", "comparator": "EQUALS",
                                    "fieldValue": {"value": "ASCENDING", "type": "STRING"}}]},
            "zoneID": res["zoneID"], "resultsLimit": 200}
    photos = {}
    while True:
        page = ck_post(base, body)
        for r in page.get("records", []):
            if r.get("recordType") != "CPLMaster":
                continue
            f = r.get("fields", {})
            kind = f.get("itemType", {}).get("value", "")
            if "movie" in kind or "video" in kind or "mpeg" in kind:
                continue
            # Prefer a full-size JPEG; HEIC originals come with a JPEG rendition.
            orig_type = f.get("resOriginalFileType", {}).get("value", "")
            for field in ("resJPEGFullRes", "resOriginalRes" if orig_type in ("public.jpeg", "public.png") else None,
                          "resJPEGMedRes"):
                if field and field in f:
                    photos[re.sub(r"[^A-Za-z0-9_-]", "_", r["recordName"])] = f[field]["value"]["downloadURL"]
                    break
        if not page.get("continuationMarker"):
            return photos
        body["continuationMarker"] = page["continuationMarker"]


def strip_exif(data):
    """Drop EXIF/XMP (incl. GPS) from a JPEG, keeping only the orientation flag."""
    if data[:2] != b"\xff\xd8":
        return data
    out, i, orientation = [b"\xff\xd8"], 2, 1
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: copy the rest verbatim
            break
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        seg = data[i:i + 2 + seglen]
        if marker == 0xE1:  # APP1 = EXIF or XMP
            body = seg[4:]
            if body[:6] == b"Exif\x00\x00":
                orientation = exif_orientation(body[6:]) or orientation
        else:
            out.append(seg)
        i += 2 + seglen
    if orientation != 1:  # re-add a tiny EXIF block with just Orientation
        tiff = (b"MM\x00\x2a\x00\x00\x00\x08" + b"\x00\x01" + b"\x01\x12\x00\x03\x00\x00\x00\x01"
                + orientation.to_bytes(2, "big") + b"\x00\x00" + b"\x00\x00\x00\x00")
        app1 = b"Exif\x00\x00" + tiff
        out.insert(1, b"\xff\xe1" + (len(app1) + 2).to_bytes(2, "big") + app1)
    out.append(data[i:])
    return b"".join(out)


def exif_orientation(tiff):
    try:
        bo = "big" if tiff[:2] == b"MM" else "little"
        ifd = int.from_bytes(tiff[4:8], bo)
        for n in range(int.from_bytes(tiff[ifd:ifd + 2], bo)):
            e = ifd + 2 + n * 12
            if int.from_bytes(tiff[e:e + 2], bo) == 0x0112:
                return int.from_bytes(tiff[e + 8:e + 10], bo)
    except Exception:
        pass
    return None


def sync_icloud():
    if not CFG.get("icloud_album"):
        return
    album = icloud_album(CFG["icloud_album"])
    wanted = {f"{ICLOUD_PREFIX}{pid}.jpg": url for pid, url in album.items()}
    added = 0
    for name, url in wanted.items():
        path = os.path.join(PHOTOS, name)
        if os.path.exists(path):
            continue
        req = urllib.request.Request(url.replace("${f}", name), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = strip_exif(r.read())
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
        added += 1
    removed = 0
    for f in os.listdir(PHOTOS):  # photos taken out of the album
        if f.startswith(ICLOUD_PREFIX) and f not in wanted:
            os.remove(os.path.join(PHOTOS, f))
            removed += 1
    print(f"icloud: {len(wanted)} in album, {added} new, {removed} removed")


def sync_photos():
    names = sorted(f for f in os.listdir(PHOTOS)
                   if f.lower().endswith(IMAGE_EXT) and not f.startswith("."))
    write_json("photos.json", names)
    skipped = [f for f in os.listdir(PHOTOS) if f.lower().endswith((".heic", ".heif"))]
    print(f"photos: {len(names)}" + (f" (skipped {len(skipped)} HEIC - browsers can't show those)" if skipped else ""))


def sync_events(fr):
    if not fr["calendars"]:
        write_json("events.json", {"updated": datetime.now(timezone.utc).isoformat(), "events": []}, fr["folder"])
        print(f"events[{fr['name']}]: no calendars configured")
        return
    now = datetime.now(timezone.utc)
    qs = urllib.parse.urlencode({
        "start": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": (now + timedelta(days=CFG.get("days_ahead", 30))).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    events = []
    for entity in fr["calendars"]:
        for e in ha_request(f"/api/calendars/{entity}?{qs}"):
            events.append({
                "title": e.get("summary", ""),
                "start": e["start"].get("dateTime") or e["start"].get("date"),
                "end": e["end"].get("dateTime") or e["end"].get("date"),
                "allDay": "date" in e["start"],
            })
    events.sort(key=lambda e: e["start"])
    write_json("events.json", {"updated": now.isoformat(), "events": events}, fr["folder"])
    print(f"events[{fr['name']}]: {len(events)}")


def sync_weather(fr):
    entity = fr["weather"]
    if not entity:
        write_json("weather.json", {}, fr["folder"])
        print(f"weather[{fr['name']}]: no entity configured")
        return
    st = ha_request(f"/api/states/{entity}")
    fc = ha_request("/api/services/weather/get_forecasts?return_response",
                    {"entity_id": entity, "type": "daily"})["service_response"][entity]["forecast"]
    write_json("weather.json", {
        "condition": st["state"],
        "temperature": st["attributes"].get("temperature"),
        "forecast": [{"datetime": f["datetime"], "condition": f["condition"],
                      "temperature": f.get("temperature")} for f in fc[:5]],
    }, fr["folder"])
    print(f"weather[{fr['name']}]: ok")


def sync_settings():
    """Add-on only: slideshow defaults for each frame page (URL options still win), and the
    page itself, kept in step with the add-on version."""
    if not ADDON:
        return
    for fr in frame_profiles():
        os.makedirs(fr["folder"], exist_ok=True)
        write_json("settings.json", {"idle": fr["idle"], "slide": fr["slide"], "black": fr["black"],
                                     "photos_base": fr["photos_base"]}, fr["folder"])
        shutil.copyfile(os.path.join(HERE, "index.html"), os.path.join(fr["folder"], "index.html"))
        rel = os.path.relpath(fr["folder"], os.path.dirname(OUT))
        print(f"page[{fr['name']}]: /local/{rel}/index.html")


def run_once(icloud):
    ok = True
    steps = [(sync_icloud, ()), ] if icloud else []
    steps.append((sync_photos, ()))
    for fr in (frame_profiles() if ADDON else [{"name": "main", "folder": OUT,
               "calendars": CFG.get("calendars", []), "weather": CFG.get("weather", "")}]):
        steps += [(sync_events, (fr,)), (sync_weather, (fr,))]
    for fn, args in steps:
        try:
            fn(*args)
        except Exception as ex:  # keep going: one failing shouldn't block the others
            ok = False
            print(f"{fn.__name__} failed: {ex!r}", file=sys.stderr, flush=True)
    return ok


def daemon():
    """Add-on loop: data refresh every refresh_minutes, iCloud download every icloud_hours."""
    refresh = max(5, int(CFG["refresh_minutes"])) * 60
    icloud_every = max(1, int(CFG["icloud_hours"])) * 3600
    last_icloud = 0.0
    while True:
        now = time.time()
        due = bool(CFG.get("icloud_album")) and now - last_icloud >= icloud_every
        if due:
            last_icloud = now
        print(time.strftime("%F %T"), "refresh" + (" (+iCloud)" if due else ""), flush=True)
        run_once(due)
        time.sleep(refresh)


if __name__ == "__main__":
    sync_settings()
    if "--daemon" in sys.argv:
        daemon()
    # One-off run. iCloud only with --icloud (weekly automation / dashboard button); the hourly
    # run just refreshes the photo list, calendar and weather.
    sys.exit(0 if run_once("--icloud" in sys.argv) else 1)
