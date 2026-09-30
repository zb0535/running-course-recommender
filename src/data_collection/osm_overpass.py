"""OSM Overpass API로 신호등/녹지/해안선 데이터를 수집한다 (API 키 불필요).

사용:
    python -m src.data_collection.osm_overpass --bbox 34.73,127.65,34.78,127.75 --out data/osm_features.json
"""
import argparse
import json
import logging
import threading
import time

import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "running-course-recommender/0.1 (school project)"}
OVERPASS_COOLDOWN_SECONDS = 300
_retry_after = 0.0
_request_lock = threading.Lock()
logger = logging.getLogger(__name__)


class OverpassUnavailable(RuntimeError):
    pass


def post_overpass_query(query: str, timeout: int = 30):
    """Send a shared Overpass request and pause all callers after a transient failure."""
    global _retry_after
    with _request_lock:
        if time.monotonic() < _retry_after:
            raise OverpassUnavailable("Overpass cooldown is active")
        try:
            response = requests.post(
                OVERPASS_URL,
                data={"data": query},
                headers=HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status is None or status == 429 or status >= 500:
                _retry_after = time.monotonic() + OVERPASS_COOLDOWN_SECONDS
            raise OverpassUnavailable(str(error)) from error
        _retry_after = 0.0
        return response


def build_query(bbox: str) -> str:
    south, west, north, east = bbox.split(",")
    box = f"({south},{west},{north},{east})"
    return f"""
    [out:json][timeout:60];
    (
      node["highway"="traffic_signals"]{box};
      way["natural"="wood"]{box};
      way["landuse"="forest"]{box};
      way["leisure"="park"]{box};
      way["natural"="coastline"]{box};
    );
    out geom;
    """


def fetch_osm_features(bbox: str, timeout: int = 60) -> dict:
    query = build_query(bbox)
    try:
        resp = post_overpass_query(query, timeout=timeout)
    except OverpassUnavailable as error:
        logger.debug("OSM 보강을 보류하고 미보강 상태로 코스를 저장합니다: %s", error)
        return {
            "bbox": bbox,
            "traffic_signal_count": 0,
            "green_way_count": 0,
            "coastline_way_count": 0,
            "traffic_signals": [],
            "green_areas": [],
            "coastline": [],
            "osm_available": False,
        }
    elements = resp.json().get("elements", [])

    traffic_signals = [e for e in elements if e.get("tags", {}).get("highway") == "traffic_signals"]
    green = [
        e for e in elements
        if e.get("tags", {}).get("natural") == "wood"
        or e.get("tags", {}).get("landuse") == "forest"
        or e.get("tags", {}).get("leisure") == "park"
    ]
    coastline = [e for e in elements if e.get("tags", {}).get("natural") == "coastline"]

    return {
        "bbox": bbox,
        "traffic_signal_count": len(traffic_signals),
        "green_way_count": len(green),
        "coastline_way_count": len(coastline),
        "traffic_signals": traffic_signals,
        "green_areas": green,
        "coastline": coastline,
        "osm_available": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbox", required=True, help="south,west,north,east (예: 34.73,127.65,34.78,127.75)")
    parser.add_argument("--out", required=True, help="출력 JSON 경로")
    args = parser.parse_args()

    features = fetch_osm_features(args.bbox)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(features, f, ensure_ascii=False, indent=2)

    print(f"신호등 {features['traffic_signal_count']}개, 녹지 {features['green_way_count']}개, "
          f"해안선 {features['coastline_way_count']}개 -> {args.out}")


if __name__ == "__main__":
    main()
