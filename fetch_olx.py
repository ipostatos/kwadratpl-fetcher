#!/usr/bin/env python3
# ===========================================================================
# Kwadrat PL — сборщик реальных объявлений с публичного API OLX.pl.
# Только stdlib (python3 на VPS без зависимостей). Запуск:
#   python3 tools/fetch-olx.py            # пишет webapp/data/listings.json
#   python3 tools/fetch-olx.py /path.json # явный путь вывода
#
# Cron на VPS (раз в 15 минут):
#   */15 * * * * python3 /opt/kwadratpl/tools/fetch-olx.py /opt/kwadratpl/webapp/data/listings.json
#
# Схема элемента listings[] совпадает с ожиданиями webapp/app.js:
#   id "olx-<id>", city (слаг), district|null, type long|short|room, rooms|null,
#   area|null, price, oldPrice|null, floor|null, pets|null, parking|null,
#   balcony|null, photo|null, url, title, descr, source "OLX",
#   agency (true = бизнес-аккаунт/агентство), ts (epoch ms)
# ===========================================================================
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

API = "https://www.olx.pl/api/v1/offers/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# city_id OLX → слаг города в приложении (webapp/app.js CITIES)
CITIES = {
    "warszawa": 17871,
    "krakow": 8959,
    "wroclaw": 19701,
    "gdansk": 5659,
    "poznan": 13983,
    "lodz": 10609,
}
# категория OLX → тип аренды в приложении
# 15 mieszkania/wynajem, 1816 noclegi, 11 stancje i pokoje
CATEGORIES = {15: "long", 1816: "short", 11: "room"}
LIMIT = {"long": 40, "short": 10, "room": 25}

ROOMS = {"one": 1, "two": 2, "three": 3, "four": 4}
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def fetch(url, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def param(offer, key):
    for p in offer.get("params") or []:
        if p.get("key") == key:
            return p.get("value") or {}
    return None


def to_int(v):
    try:
        return int(round(float(str(v).replace(",", ".").replace(" ", ""))))
    except (TypeError, ValueError):
        return None


def parse_floor(offer):
    v = param(offer, "floor_select")
    if not v:
        return None
    m = re.search(r"floor_(\d+)", str(v.get("key", "")))
    return int(m.group(1)) if m else None


def parse_parking(offer):
    v = param(offer, "parking")
    if not v:
        return None
    keys = v.get("key")
    keys = keys if isinstance(keys, list) else [keys]
    real = [k for k in keys if k and k != "brak"]
    return bool(real) if keys else None


def parse_pets(offer):
    v = param(offer, "pets")
    if not v:
        return None
    key = str(v.get("key", "")).lower()
    return True if key == "tak" else False if key == "nie" else None


def parse_balcony(text):
    t = text.lower()
    if "bez balkonu" in t:
        return False
    if "balkon" in t or "loggi" in t or "taras" in t:
        return True
    return None


def clean_text(html_text, limit=240):
    s = TAG_RE.sub(" ", html_text or "")
    s = WS_RE.sub(" ", s).strip()
    return s[:limit].rsplit(" ", 1)[0] + "…" if len(s) > limit else s


def normalize(offer, city_slug, rent_type):
    price_v = param(offer, "price") or {}
    price = to_int(price_v.get("value"))
    if not price or price <= 0:
        return None
    old = to_int(price_v.get("previous_value"))
    rooms_v = param(offer, "rooms")
    area_v = param(offer, "m")
    photos = offer.get("photos") or []
    photo = None
    if photos and photos[0].get("link"):
        photo = photos[0]["link"].replace("{width}x{height}", "640x480")
    created = offer.get("created_time") or offer.get("last_refresh_time")
    try:
        ts = int(datetime.fromisoformat(created).timestamp() * 1000)
    except (TypeError, ValueError):
        ts = int(time.time() * 1000)
    title = WS_RE.sub(" ", offer.get("title") or "").strip()
    descr = clean_text(offer.get("description"))
    district = (offer.get("location") or {}).get("district") or {}
    return {
        "id": "olx-%s" % offer["id"],
        "url": offer.get("url"),
        "title": title,
        "descr": descr,
        "city": city_slug,
        "district": district.get("name"),
        "type": rent_type,
        "rooms": ROOMS.get(str((rooms_v or {}).get("key", ""))),
        "area": to_int((area_v or {}).get("key")),
        "price": price,
        "oldPrice": old if old and old > price else None,
        "floor": parse_floor(offer),
        "pets": parse_pets(offer),
        "parking": parse_parking(offer),
        "balcony": parse_balcony(title + " " + (offer.get("description") or "")),
        "photo": photo,
        "source": "OLX",
        "agency": bool(offer.get("business")),
        "ts": ts,
    }


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "webapp", "data", "listings.json")

    listings, seen, errors = [], set(), 0
    for city_slug, city_id in CITIES.items():
        for cat_id, rent_type in CATEGORIES.items():
            url = "%s?category_id=%d&city_id=%d&limit=%d&sort_by=created_at:desc" % (
                API, cat_id, city_id, LIMIT[rent_type])
            try:
                data = fetch(url)
            except Exception as e:
                print("WARN %s/%s: %s" % (city_slug, rent_type, e))
                errors += 1
                continue
            got = 0
            for offer in data.get("data") or []:
                if offer.get("id") in seen:
                    continue
                seen.add(offer.get("id"))
                row = normalize(offer, city_slug, rent_type)
                if row:
                    listings.append(row)
                    got += 1
            print("%s %s: %d" % (city_slug, rent_type, got))
            # вежливая пауза между запросами; в CI можно ужать через env
            time.sleep(float(os.environ.get("OLX_PAUSE", "0.7")))

    if not listings:
        print("FATAL: 0 listings, keeping previous file")
        sys.exit(1)

    listings.sort(key=lambda l: l["ts"], reverse=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "olx.pl public API",
        "count": len(listings),
        "listings": listings,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)  # атомарно: Caddy не отдаст недописанный файл
    print("OK: %d listings -> %s" % (len(listings), out_path))


if __name__ == "__main__":
    main()
