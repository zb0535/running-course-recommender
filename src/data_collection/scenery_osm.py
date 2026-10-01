"""풍경 판정에 쓸 OSM 지형 데이터를 지역 단위로 받아 캐시한다.

코스마다 Overpass에 물으면 속도 제한에 걸리고(실제로 504가 잦다), 개발 중 반복 실행할 때마다
같은 데이터를 다시 받게 된다. 지역(bbox) 단위로 한 번 받아 data/osm_cache/에 저장해 두고 쓴다.
캐시는 .gitignore 대상이다.
"""
import json
import os
import time

import requests

from .osm_overpass import HEADERS, OVERPASS_URL

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "osm_cache")

# 한 번에 다 물으면 Overpass가 시간 초과로 끊으므로 성격별로 나눠 묻는다
QUERY_GROUPS = {
    "water": [
        'way["natural"="coastline"]',
        'nwr["natural"="beach"]',
        'way["waterway"~"^(river|canal|stream)$"]',
        'nwr["natural"="water"]',
    ],
    "green": [
        'nwr["leisure"~"^(park|garden)$"]',
        'nwr["natural"="wood"]',
        'nwr["landuse"="forest"]',
    ],
    "paths": [
        'way["highway"~"^(footway|path|pedestrian|cycleway)$"]',
        'way["bridge"="yes"]["highway"]',
    ],
    "places": [
        'nwr["amenity"~"^(university|college)$"]',
        'nwr["leisure"~"^(track|stadium|sports_centre)$"]',
        'nwr["historic"]',
        'nwr["tourism"~"^(museum|attraction|viewpoint)$"]',
    ],
    "shops": [
        'node["shop"]',
    ],
}


def _cache_path(region: str, group: str) -> str:
    return os.path.join(CACHE_DIR, f"{region}_{group}.json")


def _query(selectors: list, bbox: tuple) -> str:
    south, west, north, east = bbox
    area = f"({south},{west},{north},{east})"
    body = "".join(f"{s}{area};" for s in selectors)
    return f"[out:json][timeout:120];({body});out geom;"


def fetch_group(region: str, group: str, bbox: tuple, retries: int = 5, refresh: bool = False) -> list:
    path = _cache_path(region, group)
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    query = _query(QUERY_GROUPS[group], bbox)
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=180)
            if resp.status_code == 200 and resp.text.lstrip().startswith("{"):
                elements = resp.json().get("elements", [])
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(elements, f, ensure_ascii=False)
                return elements
            last_error = f"HTTP {resp.status_code}"
        except requests.RequestException as error:
            last_error = str(error)
        time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"Overpass에서 {region}/{group}을 받지 못했습니다: {last_error}")


def fetch_region(region: str, bbox: tuple, refresh: bool = False) -> dict:
    """{그룹명: [OSM 요소, ...]}. 이미 받은 그룹은 캐시에서 읽는다."""
    result = {}
    for group in QUERY_GROUPS:
        cached = not refresh and os.path.exists(_cache_path(region, group))
        result[group] = fetch_group(region, group, bbox, refresh=refresh)
        if not cached:
            time.sleep(3)
    _remember_region(region, bbox)
    return result


def _regions_path() -> str:
    return os.path.join(CACHE_DIR, "regions.json")


def cached_regions() -> dict:
    """{지역명: [south, west, north, east]} — 지형 데이터를 받아 둔 범위."""
    if not os.path.exists(_regions_path()):
        return {}
    with open(_regions_path(), encoding="utf-8") as f:
        return json.load(f)


def _remember_region(region: str, bbox: tuple) -> None:
    regions = cached_regions()
    if regions.get(region) == list(bbox):
        return
    regions[region] = list(bbox)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_regions_path(), "w", encoding="utf-8") as f:
        json.dump(regions, f, ensure_ascii=False, indent=2)


def load_cached_region(region: str) -> dict:
    """네트워크 없이 캐시에서만 읽는다. 그룹이 하나라도 없으면 None."""
    result = {}
    for group in QUERY_GROUPS:
        path = _cache_path(region, group)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            result[group] = json.load(f)
    return result


def region_bbox(courses: list, pad: float = 0.01) -> tuple:
    lats = [p[0] for c in courses for p in c["path"]]
    lngs = [p[1] for c in courses for p in c["path"]]
    return (min(lats) - pad, min(lngs) - pad, max(lats) + pad, max(lngs) + pad)
