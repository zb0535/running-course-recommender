"""추천 API에서 사용하는 GraphML Loop 어댑터.

기존 Tmap 생성 경로를 대체하지 않는다. 명시적으로 graphml 엔진을 요청한 순환 코스에만
사용하며, 거리 허용오차를 만족하지 못한 후보는 추천 결과로 반환하지 않는다.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .graph_loop_engine import LoopEngine, RouteFailure, Settings, load_light_graph


def load_engine(map_path: str, *, timeout_s: float = 5.0, max_candidates: int = 32) -> LoopEngine:
    settings = Settings(map_path=Path(map_path), timeout_s=timeout_s, max_candidates=max_candidates)
    return LoopEngine(load_light_graph(settings), settings)


def graph_loop_course(engine: LoopEngine, lat: float, lng: float, target_km: float,
                      tolerance: float = 0.15) -> dict:
    """GraphML Loop 결과를 기존 recommend 응답의 course 스키마로 변환한다."""
    result = engine.find_loop(lat, lng, target_km * 1000, tolerance)
    # 거리 불일치 결과를 "추천"으로 내보내면 10km에 6km 코스가 선택되는 문제가 재발한다.
    if not result["within_tolerance"]:
        raise RouteFailure("distance_mismatch", "요청 거리 허용 오차 안의 GraphML 순환 코스를 찾지 못했습니다.")

    edge_fingerprint = ",".join(map(str, result["edge_ids"])).encode()
    course_id = "osm-loop-" + hashlib.sha1(edge_fingerprint).hexdigest()[:12]
    place = engine.graph.graph.get("place") or engine.graph.graph.get("resolved_place") or "GraphML 지역"
    return {
        "id": course_id,
        "name": f"{place} 순환 러닝 코스",
        "region": place,
        "distance_km": round(result["distance_m"] / 1000, 2),
        "elevation_gain_m": 0,
        "surface": "unknown",
        "safety_score": 0.5,
        "traffic_signal_count": result["known_crossing_node_count"],
        "path": result["path"],
        # 현재 GraphML에는 Tmap turnType/도로명 데이터가 없으므로 음성 턴바이턴을 꾸며내지 않는다.
        "steps": [],
        "route_type": "loop",
        "tags": [],
        "scenery_pending": True,
        "source": "osm_graph",
        "graph_diagnostics": {
            "within_tolerance": result["within_tolerance"],
            "distance_error_ratio": result["distance_error_ratio"],
            "reused_edge_count": result["reused_edge_count"],
            "candidates_checked": result["candidates_checked"],
            "search_truncated": result["search_truncated"],
            "elapsed_ms": result["elapsed_ms"],
        },
    }
