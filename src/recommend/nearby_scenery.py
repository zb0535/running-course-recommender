"""지금 있는 곳 주변에 실제로 있는 풍경과 거기까지의 거리.

풍경 선택지를 전국 공통으로 보여주면 내륙에서 '바다뷰'를 고를 수 있고, 고르면 결과가 없다.
현위치 반경(기본 10km) 안의 지형을 로컬 지형 DB에서 읽어, 있는 풍경만 가까운 순서로 준다.

거리는 지형의 가장 가까운 경계까지의 직선거리다.
"""
import math

from ..data_collection import local_osm
from ..data_collection.scenery import SELECTORS

M_PER_DEG = 111_320
ROAD_REACH_KM = 2.0      # 길 종류(보행자길·자전거길·다리)는 가까이 있어야 의미가 있다. 멀리까지 읽으면 느리다
DOWNTOWN_REACH_KM = 1.0
DOWNTOWN_MIN_SHOPS = 15

# 태그 -> (지형 종류, 읽을 요소 종류)
TERRAIN = {"바다뷰": "coast", "해변": "beach", "강변": "river", "호수": "lake", "공원": "park", "숲길": "forest",
           "캠퍼스": "campus", "운동장": "sports", "역사문화": "heritage", "전망": "viewpoint"}
ROADS = {"보행자길": "footpath", "자전거길": "cycleway", "차없는길": "carfree", "다리": "bridge"}

_cache = {}


def _bbox(lat: float, lng: float, radius_km: float) -> tuple:
    dlat = radius_km * 1000 / M_PER_DEG
    dlng = dlat / math.cos(math.radians(lat))
    return (lat - dlat, lng - dlng, lat + dlat, lng + dlng)


def _lines(element: dict):
    """요소를 이루는 꺾은선들 [(lat, lng), ...]. 점 하나짜리는 길이 1인 선으로 준다."""
    if element.get("type") == "node":
        yield [(element["lat"], element["lon"])]
    for member in element.get("members", [element]):
        points = [(g["lat"], g["lon"]) for g in member.get("geometry", [])]
        if points:
            yield points


def _distance_sq(lat: float, lng: float, k: float, line: list) -> float:
    """점에서 꺾은선까지 거리의 제곱(도 단위). 꼭짓점이 아니라 선분까지 잰다 — 곧게 뻗은 강이나
    해안선은 꼭짓점이 수 km씩 떨어져 있어, 꼭짓점으로 재면 바로 옆에 있어도 멀다고 나온다."""
    best = float("inf")
    previous = None
    for plat, plng in line:
        x, y = (plng - lng) * k, plat - lat
        if previous is None:
            best = min(best, x * x + y * y)
        else:
            px, py = previous
            dx, dy = x - px, y - py
            length = dx * dx + dy * dy
            f = 0.0 if length == 0 else max(0.0, min(1.0, -(px * dx + py * dy) / length))
            cx, cy = px + f * dx, py + f * dy
            best = min(best, cx * cx + cy * cy)
        previous = (x, y)
    return best


def _nearest_km(lat: float, lng: float, elements: list, selectors: dict) -> dict:
    """{지형 종류: 가장 가까운 것까지 km}."""
    k = math.cos(math.radians(lat))
    best = {}
    for element in elements:
        names = [name for name, select in selectors.items() if select(element)]
        if not names:
            continue
        nearest = min((_distance_sq(lat, lng, k, line) for line in _lines(element)), default=None)
        if nearest is None:
            continue
        for name in names:
            if nearest < best.get(name, float("inf")):
                best[name] = nearest
    return {name: math.sqrt(value) * M_PER_DEG / 1000 for name, value in best.items()}


def nearby(lat: float, lng: float, radius_km: float = 10.0):
    """{태그: 거리 km} — 반경 안에 있는 풍경만. 로컬 지형 DB가 없으면 None."""
    if not local_osm.available():
        return None
    key = (round(lat, 3), round(lng, 3), radius_km)   # 약 100m 안에서는 같은 답
    if key in _cache:
        return _cache[key]

    found = {}
    terrain = _nearest_km(lat, lng, local_osm.load_bbox(_bbox(lat, lng, radius_km), kinds="s"),
                          {name: SELECTORS[name] for name in TERRAIN.values()})
    for tag, name in TERRAIN.items():
        if terrain.get(name, float("inf")) <= radius_km:
            found[tag] = terrain[name]

    reach = min(ROAD_REACH_KM, radius_km)
    roads = _nearest_km(lat, lng, local_osm.load_bbox(_bbox(lat, lng, reach), kinds="r"),
                        {name: SELECTORS[name] for name in ROADS.values()})
    for tag, name in ROADS.items():
        if roads.get(name, float("inf")) <= reach:
            found[tag] = roads[name]

    shops = local_osm.load_bbox(_bbox(lat, lng, DOWNTOWN_REACH_KM), kinds="p")
    k = math.cos(math.radians(lat))
    close = sum(1 for s in shops
                if math.hypot(s["lat"] - lat, (s["lon"] - lng) * k) * M_PER_DEG <= DOWNTOWN_REACH_KM * 1000)
    if close >= DOWNTOWN_MIN_SHOPS:
        found["도심"] = 0.0

    _cache[key] = {tag: round(distance, 2) for tag, distance in found.items()}
    return _cache[key]
