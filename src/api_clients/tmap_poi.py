"""장소 이름 → 좌표. Tmap 장소(POI) 검색을 쓴다.

예전에는 Nominatim(OSM)으로 찾았는데 한국 장소는 잘 못 찾는다 — "스타벅스"를 치면 오키나와가
나오고, "오동도"는 섬 한가운데 바다 쪽 좌표를 줬다. Tmap은 국내 상호·명소가 다 있고, 현위치를 주면
가까운 곳을 우선하며, 걸어서 들어갈 수 있는 입구 좌표를 준다.

후보를 여러 개 돌려준다. 같은 이름의 장소가 여러 곳일 때 서버가 하나를 찍어서 정하지 않고,
사용자가 고를 수 있게 하기 위해서다.
"""
import math
import os

import requests

POI_URL = "https://apis.openapi.sk.com/tmap/pois"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {"User-Agent": "running-course-recommender/0.1 (school project)"}
FAR_KM = 100       # 가까운 후보가 있는데 이보다 먼 후보는 같은 이름의 다른 지역이다
NEAR_KM = 30


def _distance_km(lat1, lng1, lat2, lng2) -> float:
    k = math.cos(math.radians(lat1))
    return math.hypot(lat2 - lat1, (lng2 - lng1) * k) * 111.32


def _address(poi: dict) -> str:
    parts = [poi.get("upperAddrName"), poi.get("middleAddrName"), poi.get("lowerAddrName")]
    road = " ".join(p for p in (poi.get("roadName"), poi.get("firstBuildNo")) if p)
    return " ".join(p for p in parts if p) + (f" ({road})" if road else "")


def tidy(candidates: list) -> list:
    """같은 자리의 중복(주차장·정문·충전소)을 걷어내고, 엉뚱하게 먼 동명 장소를 뺀다."""
    kept, seen = [], set()
    for place in candidates:
        spot = (round(place["lat"], 3), round(place["lng"], 3))   # 약 100m 안이면 같은 곳
        if spot in seen:
            continue
        seen.add(spot)
        kept.append(place)
    distances = [p["distance_km"] for p in kept if p["distance_km"] is not None]
    if distances and min(distances) <= NEAR_KM:
        kept = [p for p in kept if p["distance_km"] is None or p["distance_km"] <= FAR_KM]
    return kept


def _tmap(query: str, lat, lng, count: int) -> list:
    params = {"version": 1, "searchKeyword": query, "count": count * 3, "searchtypCd": "A",
              "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO"}
    if lat is not None and lng is not None:
        params.update(centerLat=lat, centerLon=lng)
    resp = requests.get(POI_URL, params=params, timeout=10,
                        headers={"appKey": os.environ["TMAP_APP_KEY"], "Accept": "application/json"})
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.text:
        return []
    places = []
    for poi in resp.json().get("searchPoiInfo", {}).get("pois", {}).get("poi", []):
        # front* = 걸어서 들어가는 입구. 없으면 중심 좌표
        plat = float(poi.get("frontLat") or poi["noorLat"])
        plng = float(poi.get("frontLon") or poi["noorLon"])
        places.append({
            "name": poi["name"], "address": _address(poi), "lat": plat, "lng": plng,
            "category": poi.get("middleBizName") or poi.get("upperBizName") or "",
            "distance_km": round(_distance_km(lat, lng, plat, plng), 2) if lat is not None else None,
        })
    return places


def _nominatim(query: str, lat, lng, count: int) -> list:
    params = {"q": query, "format": "json", "limit": count, "countrycodes": "kr", "accept-language": "ko"}
    if lat is not None and lng is not None:
        # 현위치 주변을 우선한다 (범위를 강제하지는 않는다)
        params["viewbox"] = f"{lng - 0.5},{lat + 0.5},{lng + 0.5},{lat - 0.5}"
    resp = requests.get(NOMINATIM_URL, headers=NOMINATIM_HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    places = []
    for doc in resp.json():
        plat, plng = float(doc["lat"]), float(doc["lon"])
        name, _, rest = doc.get("display_name", query).partition(", ")
        places.append({
            "name": name, "address": rest, "lat": plat, "lng": plng, "category": doc.get("type", ""),
            "distance_km": round(_distance_km(lat, lng, plat, plng), 2) if lat is not None else None,
        })
    return places


def search_places(query: str, lat: float = None, lng: float = None, count: int = 6) -> list:
    """장소 후보들 [{name, address, lat, lng, category, distance_km}] — 맞을 가능성이 높은 순서.

    Tmap 키가 없거나 Tmap이 실패하면 Nominatim(국내로 한정)으로 찾는다.
    """
    query = (query or "").strip()
    if not query:
        return []
    places = []
    if os.environ.get("TMAP_APP_KEY"):
        try:
            places = _tmap(query, lat, lng, count)
        except (requests.RequestException, ValueError, KeyError):
            places = []
    if not places:
        try:
            places = _nominatim(query, lat, lng, count)
        except (requests.RequestException, ValueError, KeyError):
            places = []
    return tidy(places)[:count]
