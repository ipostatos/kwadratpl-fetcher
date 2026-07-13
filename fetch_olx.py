#!/usr/bin/env python3
# ===========================================================================
# Kwadrat PL — сборщик реальных объявлений из двух источников:
#   • OLX.pl  — публичный JSON API (квартиры, комнаты, посуточно)
#   • Otodom  — парсинг встроенного __NEXT_DATA__ страницы выдачи (квартиры,
#               комнаты; посуточного у Otodom нет)
# Только stdlib. Запускается в GitHub Actions (fetcher-репо), POST-ит на
# бэкенд VPS. Локально: python3 tools/fetch-olx.py [/path.json]
#
# Схема элемента listings[] совпадает с ожиданиями webapp/app.js:
#   id "<src>-<id>", city (слаг), district|null, type long|short|room,
#   rooms|null, area|null, price, oldPrice|null, floor|null, pets|null,
#   parking|null, balcony|null, photo|null, url, title, descr,
#   source "OLX"|"Otodom", agency (true=агентство), ts (epoch ms)
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
    # авторы объявлений — посторонние люди: срезаем HTML и из заголовка тоже
    title = WS_RE.sub(" ", TAG_RE.sub(" ", offer.get("title") or "")).strip()
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


# ── Otodom: парсинг __NEXT_DATA__ страницы выдачи ──────────────────────────
OTODOM = "https://www.otodom.pl/pl/wyniki/wynajem/%s/%s?limit=36&by=LATEST&direction=DESC&viewType=listing"
# слаг города приложения → путь локации Otodom (voivodeship/city/city/city)
OTODOM_CITY = {
    "warszawa": "mazowieckie/warszawa/warszawa/warszawa",
    "krakow": "malopolskie/krakow/krakow/krakow",
    "wroclaw": "dolnoslaskie/wroclaw/wroclaw/wroclaw",
    "gdansk": "pomorskie/gdansk/gdansk/gdansk",
    "poznan": "wielkopolskie/poznan/poznan/poznan",
    "lodz": "lodzkie/lodz/lodz/lodz",
}
OTODOM_CAT = {"long": "mieszkanie", "room": "pokoj"}   # посуточного у Otodom нет
OTODOM_ROOMS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 4,
                "SIX": 4, "SEVEN": 4, "EIGHT": 4, "NINE": 4, "TEN": 4, "MORE": 4}
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def fetch_html(url, retries=2):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html", "Accept-Language": "pl,en"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def otodom_district(item):
    for loc in (((item.get("location") or {}).get("reverseGeocoding") or {}).get("locations") or []):
        if loc.get("locationLevel") == "district":
            return loc.get("name")
    return None


def normalize_otodom(item, city_slug, rent_type):
    if not item.get("id"):
        return None
    price = (item.get("totalPrice") or {}).get("value")
    price = to_int(price)
    if not price or price <= 0:
        return None
    href = item.get("href") or ""
    url = "https://www.otodom.pl/" + href.replace("[lang]", "pl").lstrip("/")
    imgs = item.get("images") or []
    photo = imgs[0].get("large") or imgs[0].get("medium") if imgs else None
    created = item.get("dateCreated") or item.get("createdAtFirst")
    try:
        ts = int(datetime.fromisoformat(created).timestamp() * 1000)
    except (TypeError, ValueError):
        ts = int(time.time() * 1000)
    title = WS_RE.sub(" ", TAG_RE.sub(" ", item.get("title") or "")).strip()
    return {
        "id": "otodom-%s" % item["id"],
        "url": url,
        "title": title,
        "descr": WS_RE.sub(" ", TAG_RE.sub(" ", item.get("shortDescription") or "")).strip(),
        "city": city_slug,
        "district": otodom_district(item),
        "type": rent_type,
        "rooms": OTODOM_ROOMS.get(str(item.get("rooms") or "")),
        "area": to_int(item.get("areaInSquareMeters")),
        "price": price,
        "oldPrice": None,
        "floor": None,
        "pets": None,
        "parking": None,
        "balcony": parse_balcony(title + " " + (item.get("shortDescription") or "")),
        "photo": photo,
        "source": "Otodom",
        "agency": item.get("isPrivateOwner") is False,
        "ts": ts,
    }


def fetch_otodom(city_slug, rent_type):
    url = OTODOM % (OTODOM_CAT[rent_type], OTODOM_CITY[city_slug])
    html = fetch_html(url)
    m = NEXT_RE.search(html)
    if not m:
        raise ValueError("no __NEXT_DATA__")
    data = json.loads(m.group(1))
    items = (((data.get("props") or {}).get("pageProps") or {})
             .get("data") or {}).get("searchAds") or {}
    return items.get("items") or []


# ── дедупликация OLX ↔ Otodom: одна квартира на двух площадках ──────────────
def dedup_key(l):
    # эвристика: город+тип+цена+площадь+комнаты. Совпало на обеих площадках —
    # почти наверняка тот же объект; оставляем один (приоритет — с фото и раньше)
    return (l.get("city"), l.get("type"), l.get("price"),
            l.get("area"), l.get("rooms"))


def dedup(listings):
    best = {}
    for l in listings:
        k = dedup_key(l)
        if None in (k[2], k[3]):        # без цены/площади не дедупим (риск ложных слияний)
            best[id(l)] = l
            continue
        cur = best.get(k)
        if cur is None:
            best[k] = l
        else:
            # предпочитаем объявление с фото, при равенстве — более свежее
            better = (bool(l.get("photo")), l.get("ts", 0)) > \
                     (bool(cur.get("photo")), cur.get("ts", 0))
            if better:
                best[k] = l
    return list(best.values())


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "webapp", "data", "listings.json")

    listings, seen, errors = [], set(), 0
    # ── OLX ──
    for city_slug, city_id in CITIES.items():
        for cat_id, rent_type in CATEGORIES.items():
            url = "%s?category_id=%d&city_id=%d&limit=%d&sort_by=created_at:desc" % (
                API, cat_id, city_id, LIMIT[rent_type])
            try:
                data = fetch(url)
            except Exception as e:
                print("WARN olx %s/%s: %s" % (city_slug, rent_type, e))
                errors += 1
                continue
            got = 0
            for offer in data.get("data") or []:
                if not offer.get("id") or offer["id"] in seen:
                    continue  # без id или дубль — иначе KeyError валил бы прогон
                seen.add(offer["id"])
                row = normalize(offer, city_slug, rent_type)
                if row:
                    listings.append(row)
                    got += 1
            print("olx %s %s: %d" % (city_slug, rent_type, got))
            time.sleep(float(os.environ.get("OLX_PAUSE", "0.7")))

    # ── Otodom (квартиры и комнаты) ──
    for city_slug in CITIES:
        for rent_type in OTODOM_CAT:
            try:
                items = fetch_otodom(city_slug, rent_type)
            except Exception as e:
                print("WARN otodom %s/%s: %s" % (city_slug, rent_type, e))
                errors += 1
                continue
            got = 0
            for item in items:
                row = normalize_otodom(item, city_slug, rent_type)
                if row and row["id"] not in seen:
                    seen.add(row["id"])
                    listings.append(row)
                    got += 1
            print("otodom %s %s: %d" % (city_slug, rent_type, got))
            time.sleep(float(os.environ.get("OTODOM_PAUSE", "0.5")))

    if not listings:
        print("FATAL: 0 listings, keeping previous file")
        sys.exit(1)

    before = len(listings)
    listings = dedup(listings)
    print("dedup: %d -> %d (removed %d cross-portal duplicates)" % (
        before, len(listings), before - len(listings)))

    listings.sort(key=lambda l: l["ts"], reverse=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "OLX + Otodom",
        "count": len(listings),
        "listings": listings,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)  # атомарно: Caddy не отдаст недописанный файл
    n_ot = sum(1 for l in listings if l.get("source") == "Otodom")
    print("OK: %d listings (%d OLX + %d Otodom) -> %s" % (
        len(listings), len(listings) - n_ot, n_ot, out_path))


if __name__ == "__main__":
    main()
