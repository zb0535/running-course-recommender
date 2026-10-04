"""GraphML에서 미리 변환한 GeoJSON의 안전한 제공·bbox 필터.

사용자 지도에는 선택한 코스만 표시한다. 이 모듈은 운영자/검증용 토글을
켰을 때만 지역 GraphML의 도로 edge와 횡단보도 노드를 전달한다.
"""
import json
import os
from typing import Optional

_cache = {"key": None, "data": None}


def load_feature_collection(path: str) -> dict:
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    key = (path, os.path.getmtime(path), os.path.getsize(path))
    if _cache["key"] != key:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("type") != "FeatureCollection":
            raise ValueError("Graph GeoJSON must be a FeatureCollection")
        _cache.update(key=key, data=data)
    return _cache["data"]


def _intersects_bbox(feature: dict, bbox: Optional[tuple[float, float, float, float]]) -> bool:
    if bbox is None:
        return True
    west, south, east, north = bbox
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "Point":
        coords = [coords]
    elif geometry.get("type") == "LineString":
        coords = coords
    else:
        return False
    return any(west <= lng <= east and south <= lat <= north for lng, lat in coords)


def public_graph(path: str, bbox: Optional[tuple[float, float, float, float]] = None,
                 include_crossings: bool = True) -> dict:
    """지도 현재 영역만 반환한다. 전체 그래프는 운영자 토글에만 사용한다."""
    collection = load_feature_collection(path)
    features = []
    for feature in collection.get("features", []):
        kind = (feature.get("properties") or {}).get("feature_type")
        if kind not in {"road_edge", "crossing"}:
            continue
        if kind == "crossing" and not include_crossings:
            continue
        if _intersects_bbox(feature, bbox):
            features.append(feature)
    return {"type": "FeatureCollection", "name": collection.get("name", "러닝 그래프"), "features": features}


def parse_bbox(value: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    if not value:
        return None
    try:
        west, south, east, north = (float(part) for part in value.split(","))
    except ValueError as error:
        raise ValueError("bbox는 west,south,east,north 형식이어야 합니다") from error
    if not west < east or not south < north:
        raise ValueError("bbox 범위가 올바르지 않습니다")
    return west, south, east, north
