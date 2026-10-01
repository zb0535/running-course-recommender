"""코스 경로(path)와 OSM 원시 피처(신호등/녹지/해안선)를 결합해 스키마 필드를 자동 계산.

문서의 "초기 코스 후보는 수작업 큐레이션 후 DB 등재" 방침에 따라, 팀원이 카카오/네이버맵에서
눈으로 보고 좌표만 수동으로 복사해도(API 키 불필요) 이 모듈이 나머지 지표를 채워준다.
"""
import math

GREEN_BUFFER_M = 150
COASTLINE_MAX_DISTANCE_M = 1000
SIGNAL_BUFFER_M = 30


def haversine_m(a: tuple, b: tuple) -> float:
    """a, b = (lat, lng). 두 지점 간 거리(m)."""
    R = 6371000
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _way_points(way: dict) -> list:
    return [(pt["lat"], pt["lon"]) for pt in way.get("geometry", [])]


def _min_distance_to_ways(path: list, ways: list) -> float:
    if not path or not ways:
        return float("inf")
    best = float("inf")
    for p in path:
        for way in ways:
            for wp in _way_points(way):
                d = haversine_m(p, wp)
                if d < best:
                    best = d
    return best


def green_ratio(path: list, green_ways: list, buffer_m: float = GREEN_BUFFER_M) -> float:
    if not path:
        return 0.0
    near = sum(
        1 for p in path
        if any(haversine_m(p, wp) <= buffer_m for way in green_ways for wp in _way_points(way))
    )
    return round(near / len(path), 3)


def coastline_proximity(path: list, coastline_ways: list, max_distance_m: float = COASTLINE_MAX_DISTANCE_M) -> float:
    dist = _min_distance_to_ways(path, coastline_ways)
    if dist == float("inf"):
        return 0.0
    return round(max(0.0, 1 - dist / max_distance_m), 3)


def traffic_signal_count(path: list, signal_nodes: list, buffer_m: float = SIGNAL_BUFFER_M) -> int:
    if not path or not signal_nodes:
        return 0
    count = 0
    for node in signal_nodes:
        node_pt = (node["lat"], node["lon"])
        if any(haversine_m(p, node_pt) <= buffer_m for p in path):
            count += 1
    return count


def enrich_course(course: dict, osm_features: dict) -> dict:
    """course에 path가 있어야 함. osm_features는 osm_overpass.fetch_osm_features() 반환값."""
    if osm_features.get("osm_available") is False:
        enriched = dict(course)
        enriched["osm_enrichment_pending"] = True
        return enriched

    path = course.get("path", [])
    green = green_ratio(path, osm_features.get("green_areas", []))
    coastal = coastline_proximity(path, osm_features.get("coastline", []))
    signals = traffic_signal_count(path, osm_features.get("traffic_signals", []))

    enriched = dict(course)
    enriched["green_ratio"] = green
    enriched["coastline_proximity"] = coastal
    enriched["traffic_signal_count"] = signals

    # 풍경 태그는 scenery.py의 측정으로만 붙인다. 예전에는 여기서 "녹지비율 0.4 이상이면 숲길"로
    # 붙였는데, 그 기준으로는 26개 코스 중 22개가 숲길이었다.
    if "scenery" not in enriched and not enriched.get("scenery_pending"):
        from . import scenery

        measured = scenery.tags_for_path(path, enriched)
        if measured is None:
            enriched["tags"] = []
            enriched["scenery_pending"] = True  # 지형 데이터를 받은 뒤 backfill_scenery로 채운다
        else:
            enriched["scenery"], enriched["tags"] = measured
    return enriched
