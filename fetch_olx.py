#!/usr/bin/env python3
# ===========================================================================
# Kwadrat PL — сборщик реальных объявлений из двух источников:
#   • OLX.pl  — публичный JSON API (квартиры, комнаты, посуточно)
#   • Otodom  — парсинг встроенного __NEXT_DATA__ страницы выдачи (квартиры,
#               комнаты; посуточного у Otodom нет)
# Только stdlib. Запускается в GitHub Actions (fetcher-репо), POST-ит на
# бэкенд VPS. Локально: python3 tools/fetch-olx.py [/path.json]
#
# Схема элемента listings[] совпадает с ожиданиями webapp/js/core.js:
#   id "<src>-<id>", city (слаг), district|null, type long|short|room,
#   rooms|null, area|null, price, oldPrice|null, floor|null, pets|null,
#   parking|null, balcony|null, photo|null, url, title, descr,
#   source "OLX"|"Otodom", agency (true=агентство), ts (epoch ms)
#   ownerId|ownerName|ownerSince — только OLX (landlord trust pack, см. webapp/js/price.js
#   landlordBadge); Otodom/Morizon на выдаче поиска личность продавца не отдают
# ===========================================================================
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone

API = "https://www.olx.pl/api/v1/offers/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# city_id OLX → слаг города в приложении (webapp/js/core.js CITIES)
CITIES = {
    "warszawa": 17871,
    "krakow": 8959,
    "wroclaw": 19701,
    "gdansk": 5659,
    "poznan": 13983,
    "lodz": 10609,
    "zakopane": 59535,
    "bialystok": 1079,
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


def parse_owner(offer):
    # OLX отдаёт реальную личность продавца прямо в выдаче поиска (в отличие
    # от Otodom/Morizon, где на странице выдачи такого нет) — используем для
    # landlord trust pack: повторные объявления того же ownerId + возраст
    # аккаунта (ownerSince) как мягкий сигнал доверия.
    user = offer.get("user") or {}
    oid = user.get("id")
    name = WS_RE.sub(" ", str(user.get("name") or "")).strip()[:60] or None
    since = None
    created = user.get("created")
    if created:
        try:
            since = datetime.fromisoformat(created).year
        except (TypeError, ValueError):
            since = None
    return (oid, name, since)


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
    owner_id, owner_name, owner_since = parse_owner(offer)
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
        "ownerId": owner_id,
        "ownerName": owner_name,
        "ownerSince": owner_since,
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
    "zakopane": "malopolskie/tatrzanski/zakopane/zakopane",
    "bialystok": "podlaskie/bialystok/bialystok/bialystok",
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


# ── Morizon: парсинг ld+json Product.offers страницы выдачи ─────────────────
# Вторая экосистема польского рынка (группа Ringier), преимущественно
# агентские объявления. Города — те же слаги, что у нас.
MORIZON = "https://www.morizon.pl/do-wynajecia/%s/%s/"
MORIZON_CAT = {"long": "mieszkania", "room": "pokoje"}   # посуточного нет
MZN_ID_RE = re.compile(r"mzn(\d+)")
MZN_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")


def normalize_morizon(offer, city_slug, rent_type):
    url = offer.get("url") or ""
    mid = MZN_ID_RE.search(url)
    price = to_int(offer.get("price"))
    if not mid or not price or price <= 0:
        return None
    name = WS_RE.sub(" ", offer.get("name") or "").strip()
    am = MZN_AREA_RE.search(name)
    area = to_int(am.group(1).replace(",", ".")) if am else None
    # район: текст после "m²", первый фрагмент до запятой
    district = None
    if am:
        tail = name[am.end():].strip(" ,")
        if tail:
            district = tail.split(",")[0].strip() or None
    rooms = 1 if name.lower().startswith("kawalerka") else None
    photo = offer.get("image") if str(offer.get("image", "")).startswith("https") else None
    return {
        "id": "morizon-%s" % mid.group(1),
        "url": url,
        "title": name,
        "descr": "",
        "city": city_slug,
        "district": district,
        "type": rent_type,
        "rooms": rooms,
        "area": area,
        "price": price,
        "oldPrice": None,
        "floor": None,
        "pets": None,
        "parking": None,
        "balcony": parse_balcony(name),
        "photo": photo,
        "source": "Morizon",
        "agency": None,            # на выдаче Morizon тип продавца не размечен
        "ts": int(time.time() * 1000),
    }


def fetch_morizon(city_slug, rent_type):
    url = MORIZON % (MORIZON_CAT[rent_type], city_slug)
    html = fetch_html(url)
    offers = []
    for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            b = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(b, dict) and b.get("@type") == "Product":
            o = b.get("offers") or {}
            offers = o.get("offers") if isinstance(o, dict) else []
            break
    return offers or []


# ── дедупликация между площадками: одна квартира на нескольких порталах ─────
# Приоритет представителя: источник (OLX/Otodom богаче полями), затем фото,
# затем свежесть. Так кросс-портальный дубль схлопывается в лучшую карточку.
SOURCE_RANK = {"OLX": 3, "Otodom": 2, "Morizon": 1}


AREA_TOL = 2.0   # м²: 47.5 на Otodom и 48 на OLX — почти наверняка та же квартира


def _rep_score(l):
    return (SOURCE_RANK.get(l.get("source"), 0), bool(l.get("photo")), l.get("ts", 0))


def _fold(s):
    # ascii-фолд для сравнения названий: łódź -> lodz, śródmieście -> srodmiescie
    s = str(s).strip().lower().replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _norm_district(d, city=None):
    """Нормализованный район для сравнения; None = неизвестен.
    Morizon иногда кладёт в район сам ГОРОД («Łódź») — это не информация."""
    if not d:
        return None
    x = _fold(d)
    if city and x == _fold(city):
        return None
    return x


# Улица из заголовка — доп. улика для пограничных случаев, когда один портал
# и тот же дом относит к разным районам (у OLX/Otodom/Morizon это расходится
# на границах районов). "ul./al./pl. Name" — 1-3 слова с заглавной буквы;
# заголовки на русском/украинском такого не дают (None), это ожидаемо.
STREET_RE = re.compile(
    r"\b(?:ul\.?|al\.?|pl\.?)\s+"
    r"([A-ZŁŚŻŹĆŃÓĄĘ][\wąćęłńóśźż\-]*"
    r"(?:\s+[A-ZŁŚŻŹĆŃÓĄĘ][\wąćęłńóśźż\-]*){0,2})"
)


def _extract_street(title):
    m = STREET_RE.search(title or "")
    return _fold(m.group(1)) if m else None


def _same_flat(a, b):
    """Совместимы ли две записи (город/тип/цена уже совпали по корзине):
    площадь в допуске, а вторичные сигналы — комнаты, район, улица — не
    расходятся. Неизвестное значение (None) не дисквалифицирует: Morizon не
    всегда даёт комнаты, район у части объявлений пуст. Районы-префиксы
    («Piasta» и «Piasta II» — разная детализация у порталов) считаем
    совместимыми. Точное совпадение улицы из заголовка перекрывает несовпадение
    района — на границе районов порталы называют его по-разному, а адрес
    не врёт. При НЕточной площади (47.5 vs 48) совпадение цены может быть
    случайным — сливаем только с подтверждающей уликой: комнаты, район, улица."""
    diff = abs(a["area"] - b["area"])
    if diff > AREA_TOL:
        return False
    ra, rb = a.get("rooms"), b.get("rooms")
    if ra is not None and rb is not None and ra != rb:
        return False
    da = _norm_district(a.get("district"), a.get("city"))
    db = _norm_district(b.get("district"), b.get("city"))
    district_compat = bool(da and db and (da == db or da.startswith(db) or db.startswith(da)))
    sa, sb = _extract_street(a.get("title")), _extract_street(b.get("title"))
    street_compat = bool(sa and sb and sa == sb)
    if da and db and not district_compat and not street_compat:
        return False
    if diff > 0.01:   # площадь не совпала точно — нужна улика
        return (ra is not None and ra == rb) or district_compat or street_compat
    return True


def dedup(listings):
    # Корзина: (город, тип, цена) — точные (цена на порталах совпадает у того
    # же лота). Внутри корзины кластеры по площади ±AREA_TOL, слияние
    # отменяется, если расходятся комнаты или район (false merge дороже
    # false split: пользователь «не видит всё»).
    buckets, singles = {}, []
    for l in listings:
        if l.get("price") is None or l.get("area") is None:
            singles.append(l)               # без цены/площади не дедупим
        else:
            buckets.setdefault((l.get("city"), l.get("type"), l.get("price")), []).append(l)

    out = list(singles)
    for grp in buckets.values():
        if len(grp) == 1:
            out.append(grp[0])
            continue
        # кластеризация от якоря (первый по площади): кандидат сравнивается
        # с ЯКОРЕМ кластера, не с последним членом — исключает транзитивную
        # склейку цепочки 46→48→50
        clusters = []
        for l in sorted(grp, key=lambda x: x["area"]):
            for c in clusters:
                if _same_flat(c[0], l):
                    c.append(l)
                    break
            else:
                clusters.append([l])
        for c in clusters:
            by_src = {}
            for l in c:
                by_src.setdefault(l.get("source"), []).append(l)
            if len(by_src) == 1:
                # все из одного источника — id разные => это РАЗНЫЕ квартиры,
                # случайно совпавшие ценой+площадью; оставляем все
                out.extend(c)
            else:
                # кросс-портальный дубль. Число реальных квартир ≈ макс. записей
                # от одного источника; оставляем столько лучших представителей
                n = max(len(v) for v in by_src.values())
                c.sort(key=_rep_score, reverse=True)
                out.extend(c[:n])
    return out


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

    # ── Morizon (квартиры и комнаты) ──
    for city_slug in CITIES:
        for rent_type in MORIZON_CAT:
            try:
                offers = fetch_morizon(city_slug, rent_type)
            except Exception as e:
                print("WARN morizon %s/%s: %s" % (city_slug, rent_type, e))
                errors += 1
                continue
            got = 0
            for offer in offers:
                row = normalize_morizon(offer, city_slug, rent_type)
                if row and row["id"] not in seen:
                    seen.add(row["id"])
                    listings.append(row)
                    got += 1
            print("morizon %s %s: %d" % (city_slug, rent_type, got))
            time.sleep(float(os.environ.get("MORIZON_PAUSE", "0.5")))

    if not listings:
        print("FATAL: 0 listings, keeping previous file")
        sys.exit(1)

    # вклад источников ДО дедупа
    def by_src(rows):
        d = {}
        for l in rows:
            d[l.get("source")] = d.get(l.get("source"), 0) + 1
        return d
    before = len(listings)
    raw = by_src(listings)
    listings = dedup(listings)
    kept = by_src(listings)
    print("dedup: %d -> %d (-%d cross-portal duplicates)" % (
        before, len(listings), before - len(listings)))
    print("  by source raw:", raw, "| kept:", kept)

    listings.sort(key=lambda l: l["ts"], reverse=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "OLX + Otodom + Morizon",
        "count": len(listings),
        "listings": listings,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)  # атомарно: Caddy не отдаст недописанный файл
    src = {}
    for l in listings:
        src[l.get("source")] = src.get(l.get("source"), 0) + 1
    print("OK: %d listings %s -> %s" % (len(listings), src, out_path))


if __name__ == "__main__":
    main()
