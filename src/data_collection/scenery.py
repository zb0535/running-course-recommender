"""코스가 실제로 어떤 풍경을 지나는지 재서 태그를 붙인다.

예전 태그는 "경로 근처에 녹지가 조금이라도 있으면 숲길"이라 26개 코스 중 22개가 숲길이었다.
골라도 걸러지는 게 없으니 선택지로 의미가 없었다. 여기서는 경로를 일정 간격으로 다시 찍고,
그 점들 중 몇 %가 해당 지형 안이나 바로 옆에 있는지를 잰다. 기준을 넘을 때만 태그를 붙인다.

거리 계산에서 두 가지를 지킨다:
- 선(해안선·강)은 꼭짓점이 아니라 선분까지의 거리로 잰다. 해안선은 꼭짓점이 수백 m씩 떨어져
  있어서 꼭짓점 기준이면 바로 옆을 달려도 멀다고 나온다.
- 면(숲·공원)은 안에 있으면 경계에서 멀어도 지나는 것으로 본다.
"""
import itertools
import math
from collections import defaultdict

from .enrich import haversine_m

CELL_DEG = 0.003       # 공간 색인 격자 (약 300m)
SAMPLE_SPACING_M = 25  # 경로를 이 간격으로 다시 찍어 잰다
M_PER_DEG_LAT = 111_320


def resample(path: list, spacing_m: float = SAMPLE_SPACING_M) -> list:
    """경로를 일정 간격의 점으로 다시 찍는다. 원래 점 간격이 들쭉날쭉하면 비율이 왜곡된다."""
    if len(path) < 2:
        return [list(p) for p in path]
    points = [list(path[0])]
    carried = 0.0
    for a, b in zip(path, path[1:]):
        seg = haversine_m(a, b)
        if seg <= 0:
            continue
        at = spacing_m - carried
        while at <= seg:
            t = at / seg
            points.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
            at += spacing_m
        carried = seg - (at - spacing_m)
    return points


def _segment_distance_m(lat: float, lng: float, a: tuple, b: tuple) -> float:
    k = M_PER_DEG_LAT * math.cos(math.radians(lat))
    ax, ay = (a[1] - lng) * k, (a[0] - lat) * M_PER_DEG_LAT
    bx, by = (b[1] - lng) * k, (b[0] - lat) * M_PER_DEG_LAT
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
    return math.hypot(ax + t * dx, ay + t * dy)


def _inside(lat: float, lng: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _side(a: tuple, b: tuple, c: tuple) -> float:
    return (b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])


class FeatureIndex:
    """OSM 요소(점·선·면)를 격자에 색인해 '이 지점 근처에 있는가'를 빠르게 답한다."""

    def __init__(self, elements: list):
        self._segments = []                 # (a, b, 요소번호)
        self._points = []                   # (lat, lng, 요소번호)
        self._rings = []                    # (ring, bbox, 요소번호)
        self._holes = defaultdict(list)     # 요소번호 -> 그 면에 뚫린 구멍들
        self._ring_grid = defaultdict(list)  # 격자 칸 -> 그 칸에 걸친 면들 (면이 많은 지역에서 전부 훑지 않게)
        self._wide_rings = []                # 너무 넓어 칸마다 적어 두지 않는 면
        self._seg_grid = defaultdict(list)
        self._pt_grid = defaultdict(list)
        self.size = 0
        for element in elements:
            self._add(element, self.size)
            self.size += 1

    @staticmethod
    def _cell(lat: float, lng: float) -> tuple:
        return int(math.floor(lat / CELL_DEG)), int(math.floor(lng / CELL_DEG))

    def _add_way(self, geometry: list, eid: int, hole: bool = False) -> None:
        pts = [(g["lat"], g["lon"]) for g in geometry if g]
        for a, b in zip(pts, pts[1:]):
            idx = len(self._segments)
            self._segments.append((a, b, eid))
            (r1, c1), (r2, c2) = self._cell(*a), self._cell(*b)
            for r in range(min(r1, r2), max(r1, r2) + 1):
                for c in range(min(c1, c2), max(c1, c2) + 1):
                    self._seg_grid[(r, c)].append(idx)
        if len(pts) >= 4 and pts[0] == pts[-1]:
            if hole:
                # 구멍 안은 그 지형이 아니다. 섬을 두르는 강변 공원은 바깥 고리가 섬 전체를 감싸므로,
                # 구멍을 빼지 않으면 섬 안 시가지가 통째로 공원으로 잡힌다(여의도에서 공원 100%).
                self._holes[eid].append(pts)
                return
            lats, lngs = [p[0] for p in pts], [p[1] for p in pts]
            box = (min(lats), min(lngs), max(lats), max(lngs))
            index = len(self._rings)
            self._rings.append((pts, box, eid))
            (r1, c1), (r2, c2) = self._cell(box[0], box[1]), self._cell(box[2], box[3])
            if (r2 - r1 + 1) * (c2 - c1 + 1) > 400:
                self._wide_rings.append(index)
            else:
                for r in range(r1, r2 + 1):
                    for c in range(c1, c2 + 1):
                        self._ring_grid[(r, c)].append(index)

    def _add(self, element: dict, eid: int) -> None:
        kind = element.get("type")
        if kind == "node" and "lat" in element:
            self._points.append((element["lat"], element["lon"], eid))
            self._pt_grid[self._cell(element["lat"], element["lon"])].append(len(self._points) - 1)
        elif kind == "relation":
            for member in element.get("members", []):
                if member.get("geometry"):
                    self._add_way(member["geometry"], eid, hole=member.get("role") == "inner")
        elif element.get("geometry"):
            self._add_way(element["geometry"], eid)

    def _cells_around(self, lat: float, lng: float, radius_m: float):
        dlat = radius_m / M_PER_DEG_LAT
        dlng = radius_m / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
        r1, c1 = self._cell(lat - dlat, lng - dlng)
        r2, c2 = self._cell(lat + dlat, lng + dlng)
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                yield (r, c)

    def features_near(self, lat: float, lng: float, radius_m: float) -> set:
        """반경 안에 있거나, 그 지점을 품고 있는 요소들의 번호."""
        found = set()
        seen = set()
        for cell in self._cells_around(lat, lng, radius_m):
            for idx in self._seg_grid.get(cell, ()):
                if idx in seen:
                    continue
                seen.add(idx)
                a, b, eid = self._segments[idx]
                if eid not in found and _segment_distance_m(lat, lng, a, b) <= radius_m:
                    found.add(eid)
            for idx in self._pt_grid.get(cell, ()):
                plat, plng, eid = self._points[idx]
                if eid not in found and haversine_m((lat, lng), (plat, plng)) <= radius_m:
                    found.add(eid)
        for index in itertools.chain(self._ring_grid.get(self._cell(lat, lng), ()), self._wide_rings):
            ring, (s, w, n, e), eid = self._rings[index]
            if (eid not in found and s <= lat <= n and w <= lng <= e and _inside(lat, lng, ring)
                    and not any(_inside(lat, lng, hole) for hole in self._holes.get(eid, ()))):
                found.add(eid)
        return found

    def near(self, lat: float, lng: float, radius_m: float) -> bool:
        return bool(self.features_near(lat, lng, radius_m))

    def crosses(self, a: tuple, b: tuple) -> bool:
        """a에서 b로 그은 직선이 이 지형의 선을 가로지르는가. 해안선이면 그 사이에 바다가 있다는 뜻이다."""
        (r1, c1), (r2, c2) = self._cell(*a), self._cell(*b)
        seen = set()
        for r in range(min(r1, r2), max(r1, r2) + 1):
            for c in range(min(c1, c2), max(c1, c2) + 1):
                for idx in self._seg_grid.get((r, c), ()):
                    if idx in seen:
                        continue
                    seen.add(idx)
                    p, q, _ = self._segments[idx]
                    if _side(a, b, p) * _side(a, b, q) < 0 and _side(p, q, a) * _side(p, q, b) < 0:
                        return True
        return False

    def nearest_point(self, lat: float, lng: float, radius_m: float):
        """반경 안에서 가장 가까운 선 위의 지점 (lat, lng). 없으면 None. 경유지를 길 위에 붙일 때 쓴다."""
        k = M_PER_DEG_LAT * math.cos(math.radians(lat))
        best, best_d = None, radius_m
        seen = set()
        for cell in self._cells_around(lat, lng, radius_m):
            for idx in self._seg_grid.get(cell, ()):
                if idx in seen:
                    continue
                seen.add(idx)
                a, b, _ = self._segments[idx]
                ax, ay = (a[1] - lng) * k, (a[0] - lat) * M_PER_DEG_LAT
                bx, by = (b[1] - lng) * k, (b[0] - lat) * M_PER_DEG_LAT
                dx, dy = bx - ax, by - ay
                length_sq = dx * dx + dy * dy
                t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
                px, py = ax + t * dx, ay + t * dy
                d = math.hypot(px, py)
                if d <= best_d:
                    best, best_d = (lat + py / M_PER_DEG_LAT, lng + px / k), d
        return best


def coverage(path: list, index: FeatureIndex, radius_m: float) -> float:
    """경로 중 이 지형 안이나 radius_m 이내를 지나는 비율 (0~1)."""
    points = resample(path)
    if not points or index.size == 0:
        return 0.0
    return round(sum(1 for lat, lng in points if index.near(lat, lng, radius_m)) / len(points), 3)


def count_near(path: list, index: FeatureIndex, radius_m: float) -> int:
    """경로에서 radius_m 이내에 있는 요소의 개수 (같은 요소는 한 번만 센다)."""
    found = set()
    for lat, lng in resample(path):
        found |= index.features_near(lat, lng, radius_m)
    return len(found)


def length_on(path: list, index: FeatureIndex, radius_m: float) -> float:
    """경로 중 이 지형 위를 지나는 길이(m)."""
    points = resample(path)
    return sum(1 for lat, lng in points if index.near(lat, lng, radius_m)) * SAMPLE_SPACING_M


def longest_run_m(path: list, index: FeatureIndex, radius_m: float) -> float:
    """이 지형 위를 끊기지 않고 지나는 가장 긴 구간(m).

    다리는 총 길이로 재면 안 된다. 천변길은 다리 밑을 여러 번 지나는데, 그걸 합치면 수백 m가
    되어 '다리를 건너는 코스'로 잘못 잡힌다. 실제로 건너면 한 번에 길게 이어진다.
    """
    best = run = 0
    for lat, lng in resample(path):
        run = run + 1 if index.near(lat, lng, radius_m) else 0
        best = max(best, run)
    return best * SAMPLE_SPACING_M


# ── 풍경 종류: (측정값 이름, OSM 요소를 고르는 조건) ──
def _tag(element: dict, key: str):
    return (element.get("tags") or {}).get(key)


SELECTORS = {
    "coast": lambda e: _tag(e, "natural") == "coastline",
    "beach": lambda e: _tag(e, "natural") == "beach",
    "river": lambda e: _tag(e, "waterway") in ("river", "canal", "stream")
    or (_tag(e, "natural") == "water" and _tag(e, "water") in ("river", "canal", "stream")),
    "lake": lambda e: _tag(e, "natural") == "water" and _tag(e, "water") in ("lake", "reservoir", "pond"),
    "park": lambda e: _tag(e, "leisure") in ("park", "garden"),
    "forest": lambda e: _tag(e, "natural") == "wood" or _tag(e, "landuse") == "forest",
    "footpath": lambda e: _tag(e, "highway") in ("footway", "path", "pedestrian"),
    "cycleway": lambda e: _tag(e, "highway") == "cycleway",
    "carfree": lambda e: _tag(e, "highway") in ("footway", "path", "pedestrian", "cycleway"),
    "bridge": lambda e: _tag(e, "bridge") == "yes",
    "campus": lambda e: _tag(e, "amenity") in ("university", "college"),
    "sports": lambda e: _tag(e, "leisure") in ("track", "stadium", "sports_centre"),
    "heritage": lambda e: _tag(e, "historic") is not None or _tag(e, "tourism") in ("museum", "attraction"),
    "viewpoint": lambda e: _tag(e, "tourism") == "viewpoint",
    "shops": lambda e: _tag(e, "shop") is not None,
}


def build_indexes(region_features: dict) -> dict:
    """{측정값 이름: FeatureIndex}. region_features는 scenery_osm.fetch_region의 결과."""
    elements = [e for group in region_features.values() for e in group]
    return {name: FeatureIndex([e for e in elements if select(e)]) for name, select in SELECTORS.items()}


# 경로가 이 거리 안에 있으면 그 지형을 지나는 것으로 본다 (m)
MEASURE_RADIUS = {
    "coast": 200,     # 바다가 보일 만한 거리
    "beach": 100,
    "river": 80,
    "lake": 100,
    "park": 20,       # 공원 안이거나 바로 옆
    "forest": 15,     # 숲 안을 지나는 구간만
    "footpath": 8,    # 그 길 위를 달리는 구간만
    "cycleway": 8,
    "carfree": 8,     # 차가 못 다니는 길 위를 달리는 비율
    "campus": 20,
    "sports": 40,
}


def measure(path: list, indexes: dict) -> dict:
    """코스 경로의 풍경 측정값. 비율은 0~1, *_per_km는 km당 개수, *_m는 미터."""
    km = max(sum(haversine_m(a, b) for a, b in zip(path, path[1:])) / 1000, 0.1)
    measures = {name: coverage(path, indexes[name], radius) for name, radius in MEASURE_RADIUS.items()}
    measures.update({
        "bridge_run_m": longest_run_m(path, indexes["bridge"], 8),
        "heritage_per_km": round(count_near(path, indexes["heritage"], 120) / km, 2),
        "viewpoints": count_near(path, indexes["viewpoint"], 150),
        "shops_per_km": round(count_near(path, indexes["shops"], 50) / km, 2),
    })
    return measures


# 태그를 붙이는 기준: (측정값, 최소값, 앱에 보여줄 설명).
# 26개 실제 코스의 측정값을 코스 이름과 대조해서 정했다. 기준을 못 넘는 풍경은 태그가 붙지 않고,
# 해당 코스가 하나도 없는 태그는 선택지로도 나가지 않는다 — 골라도 결과가 없는 선택지를 만들지 않는다.
THRESHOLDS = {
    "바다뷰": ("coast", 0.4, "경로의 40% 이상이 해안선 200m 이내"),
    "해변": ("beach", 0.15, "경로의 15% 이상이 해변 옆"),
    "강변": ("river", 0.3, "경로의 30% 이상이 강·하천 옆"),
    "호수": ("lake", 0.1, "경로의 10% 이상이 호수·저수지 옆"),
    "공원": ("park", 0.15, "경로의 15% 이상이 공원 안이나 바로 옆"),
    "숲길": ("forest", 0.2, "경로의 20% 이상이 숲 안"),
    "보행자길": ("footpath", 0.3, "경로의 30% 이상이 보행자 전용길"),
    "자전거길": ("cycleway", 0.15, "경로의 15% 이상이 자전거도로"),
    "차없는길": ("carfree", 0.5, "경로의 절반 이상이 차가 다니지 않는 길"),
    "캠퍼스": ("campus", 0.15, "경로의 15% 이상이 대학 캠퍼스 안"),
    "운동장": ("sports", 0.1, "경로의 10% 이상이 운동장·트랙 옆"),
    "다리": ("bridge_run_m", 200, "200m 이상 이어지는 다리를 건넘"),
    "역사문화": ("heritage_per_km", 2.0, "km당 유적·박물관·명소 2곳 이상"),
    "전망": ("viewpoints", 1, "경로 150m 이내에 전망 포인트"),
}
DOWNTOWN_SHOPS_PER_KM = 12     # 상점이 이만큼 촘촘하거나
DOWNTOWN_SIGNALS_PER_KM = 2.5  # 신호등이 이만큼 촘촘하면 도심 (지역마다 OSM 상점·신호등 데이터가
                               # 한쪽씩 비어 있어서, 둘 중 하나만 충족해도 인정한다)
MOUNTAIN_CLIMB_M_PER_KM = 25   # 숲길이면서 km당 이만큼 오르면 산길

DERIVED_TAGS = {
    "도심": "상점이나 신호등이 촘촘한 시내 구간",
    "산길": "숲길이면서 km당 25m 이상 오름",
}


def tag_descriptions() -> dict:
    """{태그: 붙는 기준 설명}. 앱이 선택지 옆에 근거를 보여줄 때 쓴다."""
    described = {tag: text for tag, (_, _, text) in THRESHOLDS.items()}
    described.update(DERIVED_TAGS)
    return described


def scenery_tags(measures: dict, course: dict) -> list:
    tags = [tag for tag, (name, minimum, _) in THRESHOLDS.items() if measures.get(name, 0) >= minimum]
    km = max(course.get("distance_km", 0), 0.1)
    if (measures.get("shops_per_km", 0) >= DOWNTOWN_SHOPS_PER_KM
            or course.get("traffic_signal_count", 0) / km >= DOWNTOWN_SIGNALS_PER_KM):
        tags.append("도심")
    if "숲길" in tags and course.get("elevation_gain_m", 0) / km >= MOUNTAIN_CLIMB_M_PER_KM:
        tags.append("산길")
    return tags


_index_cache = {}


def tags_for_path(path: list, course: dict = None):
    """받아 둔 지형 데이터가 이 경로를 덮고 있으면 (측정값, 태그)를, 아니면 None을 준다.

    실시간으로 만든 코스에 쓴다. 지형 데이터가 없는 곳의 코스에 "사용자가 바다뷰를 요청했으니
    바다뷰"라고 붙이지 않기 위해, 확인할 수 없으면 태그를 붙이지 않고 None을 돌려준다.
    여기서 새로 받지는 않는다 — 코스를 만들 때 받아 둔 주변 데이터나 지역 캐시만 본다.
    """
    from . import area_cache
    from .scenery_osm import cached_regions, load_cached_region

    if not path:
        return None

    area = area_cache.cached_area_for_path(path)
    if area is not None:
        measures = measure(path, area.indexes)
        return measures, scenery_tags(measures, course or {})

    lats, lngs = [p[0] for p in path], [p[1] for p in path]
    for region, (south, west, north, east) in cached_regions().items():
        if south <= min(lats) and max(lats) <= north and west <= min(lngs) and max(lngs) <= east:
            if region not in _index_cache:
                features = load_cached_region(region)
                if features is None:
                    continue
                _index_cache[region] = build_indexes(features)
            measures = measure(path, _index_cache[region])
            return measures, scenery_tags(measures, course or {})
    return None
