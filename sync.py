#!/usr/bin/env python3
"""Refresh the family frame's data files from Home Assistant + the photos folder.

Writes into out_dir (served by HA at /local/frame/):
  photos/ic_*.jpg  photos pulled from the iCloud shared album (location data stripped)
  photos.json    list of image files in out_dir/photos (album photos + any dropped in via Samba)
  events.json    upcoming events from the configured HA calendars
  weather.json   current weather + 5-day forecast from the HA weather entity

Standard library only, so it runs inside the HAOS core container via shell_command.
Config: config.json next to this script (see config.example.json).
"""
import json, os, re, sys, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))
OUT = CFG["out_dir"]
PHOTOS = os.path.join(OUT, "photos")
os.makedirs(PHOTOS, exist_ok=True)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def write_json(name, data):
    """Write atomically so the tablet never reads a half-written file."""
    path = os.path.join(OUT, name)
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


def sync_events():
    now = datetime.now(timezone.utc)
    qs = urllib.parse.urlencode({
        "start": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": (now + timedelta(days=CFG.get("days_ahead", 30))).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    events = []
    for entity in CFG["calendars"]:
        for e in ha_request(f"/api/calendars/{entity}?{qs}"):
            events.append({
                "title": e.get("summary", ""),
                "start": e["start"].get("dateTime") or e["start"].get("date"),
                "end": e["end"].get("dateTime") or e["end"].get("date"),
                "allDay": "date" in e["start"],
            })
    events.sort(key=lambda e: e["start"])
    write_json("events.json", {"updated": now.isoformat(), "events": events})
    print(f"events: {len(events)}")


def sync_weather():
    entity = CFG["weather"]
    st = ha_request(f"/api/states/{entity}")
    fc = ha_request("/api/services/weather/get_forecasts?return_response",
                    {"entity_id": entity, "type": "daily"})["service_response"][entity]["forecast"]
    write_json("weather.json", {
        "condition": st["state"],
        "temperature": st["attributes"].get("temperature"),
        "forecast": [{"datetime": f["datetime"], "condition": f["condition"],
                      "temperature": f.get("temperature")} for f in fc[:5]],
    })
    print("weather: ok")


if __name__ == "__main__":
    ok = True
    # The iCloud download only runs with --icloud (weekly automation + the dashboard button);
    # the hourly run just refreshes the photo list, calendar and weather.
    steps = (sync_icloud,) if "--icloud" in sys.argv else ()
    for fn in steps + (sync_photos, sync_events, sync_weather):
        try:
            fn()
        except Exception as ex:  # keep going: one failing shouldn't block the others
            ok = False
            print(f"{fn.__name__} failed: {ex!r}", file=sys.stderr)
    sys.exit(0 if ok else 1)
