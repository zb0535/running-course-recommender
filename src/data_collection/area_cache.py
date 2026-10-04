"""출발점 주변의 길과 지형을 지역당 한 번만 받아 두고 쓴다.

실시간 코스 생성에서 가장 느리고 불안정한 건 Tmap이 아니라 Overpass다(건당 5~20초, 504가 잦다).
예전에는 요청마다 Overpass를 불러 경유지를 길에 붙였고, 받은 데이터는 버렸다. 풍경을 재려면
또 따로 불러야 했다.

여기서는 출발점이 속한 격자 주변을 한 번에 받는다 — 달릴 수 있는 길과 풍경 지형(해안선·강·공원·숲
등)을 한 질의로. 실측 5~6초, 1.6~2.7MB다. 그 뒤로 그 동네의 요청은 Overpass 호출이 0건이고,
경유지를 길에 붙이는 것, 풍경 쪽으로 경유지를 잡는 것, 만든 코스의 풍경을 재는 것이 전부 로컬에서 된다.

로컬 지형 DB(local_osm)를 만들어 두었으면 Overpass를 아예 부르지 않고 거기서 읽는다.
"""
import json
import math
import os
from collections import Counter

import requests

from . import local_osm, scenery_osm
from .osm_overpass import HEADERS, OverpassUnavailable, post_overpass_query
from .scenery import FeatureIndex, build_indexes

CELL_DEG = 0.02  # 캐시 격자 (약 2.2km). 같은 칸에 선 사람들은 같은 데이터를 쓴다
# (받아 두는 반폭, 덮을 수 있는 순환 반경) — 긴 순환은 더 넓게 받는다
TIERS = [(0.03, 1700), (0.06, 4500)]
M_PER_DEG = 111_320
MIRROR_URL = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"  # 본 서버가 실패했을 때만 쓴다

RUNNABLE = "footway|path|pedestrian|living_street|residential|service|cycleway|track|unclassified|tertiary"
# 경유지를 붙일 길. 주차장 통로·진입로(service)와 농로(track)는 들어갔다 되나오기 쉬워 뺀다
SNAP_HIGHWAYS = {"footway", "path", "pedestrian", "living_street", "residential", "cycleway",
                 "unclassified", "tertiary"}

SELECTORS = [
    f'way["highway"~"^({RUNNABLE})$"]',
    'way["bridge"="yes"]["highway"]',
    'way["natural"="coastline"]',
    'nwr["natural"="beach"]',
    'way["waterway"~"^(river|canal|stream)$"]',
    'nwr["natural"="water"]',
    'nwr["leisure"~"^(park|garden)$"]',
    'nwr["natural"="wood"]',
    'nwr["landuse"="forest"]',
    'nwr["amenity"~"^(university|college)$"]',
    'nwr["leisure"~"^(track|stadium|sports_centre)$"]',
    'nwr["historic"]',
    'nwr["tourism"~"^(museum|attraction|viewpoint)$"]',
    'node["shop"]',
]

_memory = {}


def through_ways(elements: list) -> list:
    """경유지를 붙여도 되는 길: 달릴 수 있는 종류이면서 막다른 길이 아닌 것.

    막다른 길에 경유지를 찍으면 Tmap이 끝까지 갔다가 같은 길로 되돌아 나온다. 예전에는 그렇게
    만들어진 뒤에 잘라내고 Tmap에 다시 요청했는데, 도로망을 갖고 있으면 처음부터 피할 수 있다.
    길의 끝 노드가 다른 어떤 길과도 이어지지 않으면 막다른 길이다.
    """
    roads = [e for e in elements
             if e.get("type") == "way" and e.get("nodes") and (e.get("tags") or {}).get("highway")]
    degree = Counter(node for road in roads for node in road["nodes"])
    kept = []
    for road in roads:
        if road["tags"]["highway"] not in SNAP_HIGHWAYS:
            continue
        first, last = road["nodes"][0], road["nodes"][-1]
        if first != last and (degree[first] < 2 or degree[last] < 2):
            continue
        kept.append(road)
    # 로컬 DB에서 읽은 길은 만들 때 전국 도로망으로 막다른 길 여부를 이미 판정해 두었다
    kept += [e for e in elements
             if e.get("dead_end") is False and (e.get("tags") or {}).get("highway") in SNAP_HIGHWAYS]
    return kept


class Area:
    """한 격자 주변의 길·지형. 색인은 처음 쓸 때 만든다."""

    def __init__(self, bbox: tuple, elements: list, local: bool = False):
        self.bbox = bbox
        self.elements = elements
        self.local = local   # 로컬 지형 DB에서 읽었는가 — 그래야 길이 빠짐없이 이어져 있어 경로를 직접 짤 수 있다
        self._graph = None
        self._safety = None
        self._roads = None
        self._indexes = None
        self._water = None

    @property
    def roads(self) -> FeatureIndex:
        if self._roads is None:
            self._roads = FeatureIndex(through_ways(self.elements))
        return self._roads

    @property
    def indexes(self) -> dict:
        if self._indexes is None:
            self._indexes = build_indexes({"all": self.elements})
        return self._indexes

    @property
    def water(self) -> FeatureIndex:
        """건너려면 다리가 필요한 물: 해안선과 면으로 그려진 강·호수 (선으로만 그려진 개울은 뺀다)."""
        if self._water is None:
            self._water = FeatureIndex([
                e for e in self.elements
                if (e.get("tags") or {}).get("natural") in ("coastline", "water")
            ])
        return self._water

    @property
    def safety_points(self) -> dict:
        """안전 점수에 쓰는 시설들 {cctv, convenience, police, signals: [{lat, lng}]}."""
        if self._safety is None:
            found = {"cctv": [], "convenience": [], "police": [], "signals": []}
            for element in self.elements:
                tags = element.get("tags") or {}
                if element.get("type") == "node":
                    point = {"lat": element["lat"], "lng": element["lon"]}
                elif element.get("geometry"):
                    first = element["geometry"][0]
                    point = {"lat": first["lat"], "lng": first["lon"]}
                else:
                    continue
                if tags.get("man_made") == "surveillance":
                    found["cctv"].append(point)
                elif tags.get("shop") == "convenience":
                    found["convenience"].append(point)
                elif tags.get("amenity") == "police":
                    found["police"].append(point)
                elif tags.get("highway") == "traffic_signals" and element.get("type") == "node":
                    found["signals"].append(point)
            self._safety = found
        return self._safety

    @property
    def graph(self):
        """경로를 직접 짜는 도로망. Overpass로 받은 데이터는 큰길·통로가 빠져 있어 쓰지 않는다."""
        if not self.local:
            return None
        if self._graph is None:
            from ..recommend.road_graph import RoadGraph
            self._graph = RoadGraph(self.elements, self.indexes)
        return self._graph

    def covers(self, lat: float, lng: float, radius_m: float = 0) -> bool:
        south, west, north, east = self.bbox
        dlat = radius_m / M_PER_DEG
        dlng = radius_m / (M_PER_DEG * math.cos(math.radians(lat)))
        return south <= lat - dlat and lat + dlat <= north and west <= lng - dlng and lng + dlng <= east

    def covers_path(self, path: list) -> bool:
        south, west, north, east = self.bbox
        return all(south <= p[0] <= north and west <= p[1] <= east for p in path)


def _cell(lat: float, lng: float) -> tuple:
    return (round(round(lat / CELL_DEG) * CELL_DEG, 4), round(round(lng / CELL_DEG) * CELL_DEG, 4))


def _tier_for(radius_m: float) -> int:
    for index, (_, reach) in enumerate(TIERS):
        if radius_m <= reach:
            return index
    return len(TIERS) - 1


def _dir() -> str:
    return os.path.join(scenery_osm.CACHE_DIR, "areas")


def _path(key: str) -> str:
    return os.path.join(_dir(), f"{key}.json")


def _fetch(bbox: tuple) -> list:
    south, west, north, east = bbox
    area = f"({south},{west},{north},{east})"
    query = "[out:json][timeout:60];(" + "".join(f"{s}{area};" for s in SELECTORS) + ");out geom;"
    try:
        return post_overpass_query(query, timeout=60).json().get("elements", [])
    except (OverpassUnavailable, requests.RequestException, ValueError) as error:
        first_error = error
    # 본 서버는 504·429가 잦다. 한 번 실패로 그 동네의 풍경 유도가 통째로 빠지지 않게 예비 서버에 묻는다
    try:
        response = requests.post(MIRROR_URL, data={"data": query}, headers=HEADERS, timeout=75)
        response.raise_for_status()
        return response.json().get("elements", [])
    except (requests.RequestException, ValueError) as error:
        raise OverpassUnavailable(f"{first_error} / 예비 서버: {error}") from error


def load_area(lat: float, lng: float, radius_m: float = 900, allow_fetch: bool = True):
    """이 지점 주변의 Area. 받아 둔 게 없고 받을 수도 없으면 None."""
    clat, clng = _cell(lat, lng)
    if local_osm.available():
        # 파일에서 읽으니 필요한 만큼 넓혀도 된다 (0.01° 단위로 맞춰 같은 동네는 같은 것을 쓴다)
        half = max(TIERS[0][0], math.ceil((radius_m / M_PER_DEG + 0.015) * 100) / 100)
        key = f"local_{clat:.4f}_{clng:.4f}_{half:.2f}"
        if key not in _memory:
            bbox = (clat - half, clng - half, clat + half, clng + half)
            _memory[key] = Area(bbox, local_osm.load_bbox(bbox), local=True)
        return _memory[key]
    for tier in range(_tier_for(radius_m), len(TIERS)):
        half = TIERS[tier][0]
        key = f"{clat:.4f}_{clng:.4f}_t{tier}"
        if key in _memory:
            return _memory[key]
        if os.path.exists(_path(key)):
            with open(_path(key), encoding="utf-8") as f:
                stored = json.load(f)
            _memory[key] = Area(tuple(stored["bbox"]), stored["elements"])
            return _memory[key]

    if not allow_fetch:
        return None
    tier = _tier_for(radius_m)
    half = TIERS[tier][0]
    key = f"{clat:.4f}_{clng:.4f}_t{tier}"
    bbox = (clat - half, clng - half, clat + half, clng + half)
    try:
        elements = _fetch(bbox)
    except OverpassUnavailable:
        return None  # 실패는 저장하지 않는다 — 다음 요청에서 다시 시도한다
    os.makedirs(_dir(), exist_ok=True)
    with open(_path(key), "w", encoding="utf-8") as f:
        json.dump({"bbox": list(bbox), "elements": elements}, f, ensure_ascii=False)
    _memory[key] = Area(bbox, elements)
    return _memory[key]


def cached_area_for_path(path: list):
    """경로 전체를 덮는 받아 둔 Area. 새로 받지는 않는다."""
    if not path:
        return None
    mid = path[len(path) // 2]
    if local_osm.available():
        lats, lngs = [p[0] for p in path], [p[1] for p in path]
        span_m = max(max(lats) - min(lats), max(lngs) - min(lngs)) * M_PER_DEG
        area = load_area(mid[0], mid[1], radius_m=span_m)
        return area if area.covers_path(path) else None
    for radius in (reach for _, reach in TIERS):
        area = load_area(mid[0], mid[1], radius_m=radius, allow_fetch=False)
        if area is not None and area.covers_path(path):
            return area
    return None
