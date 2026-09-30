"""수작업으로 좌표만 찍은 코스 후보를 받아 OSM 데이터로 자동 보강한 뒤 DB(JSON)에 등재.

입력 파일 예시 (raw course, API 키 불필요 — 카카오/네이버맵에서 좌표만 눈으로 복사):
{
  "id": "yeosu-coastal-02",
  "name": "여수 오동도 진입로",
  "region": "여수",
  "distance_km": 2.8,
  "elevation_gain_m": 25,
  "surface": "paved",
  "safety_score": 0.7,
  "path": [[34.7419, 127.7549], [34.7455, 127.7601]]
}

사용:
    python -m src.data_collection.add_course --input new_course.json --db data/courses.json
"""
import argparse
import json
import os

from .enrich import enrich_course
from .osm_overpass import fetch_osm_features

PADDING_DEG = 0.01  # 경로 주변 여유 범위 (~1km)


def bbox_from_path(path: list) -> str:
    lats = [p[0] for p in path]
    lngs = [p[1] for p in path]
    south, north = min(lats) - PADDING_DEG, max(lats) + PADDING_DEG
    west, east = min(lngs) - PADDING_DEG, max(lngs) + PADDING_DEG
    return f"{south},{west},{north},{east}"


def load_db(db_path: str) -> list:
    if os.path.exists(db_path):
        with open(db_path, encoding="utf-8") as f:
            return json.load(f)
    return []


def add_course(raw_course: dict, db_path: str) -> dict:
    path = raw_course.get("path", [])
    if not path:
        raise ValueError("path(좌표 리스트)가 비어 있습니다. 최소 2개 지점이 필요합니다.")

    bbox = bbox_from_path(path)
    osm_features = fetch_osm_features(bbox)
    enriched = enrich_course(raw_course, osm_features)
    enriched.setdefault("source", "manual_curation+osm")

    db = load_db(db_path)
    db = [c for c in db if c["id"] != enriched["id"]]  # 동일 id 있으면 갱신
    db.append(enriched)

    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    return enriched


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="원본 코스 JSON 경로")
    parser.add_argument("--db", required=True, help="등재할 코스 DB JSON 경로")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        raw_course = json.load(f)

    enriched = add_course(raw_course, args.db)
    print(f"등재 완료: {enriched['id']} -> green_ratio={enriched['green_ratio']}, "
          f"coastline_proximity={enriched['coastline_proximity']}, "
          f"traffic_signal_count={enriched['traffic_signal_count']}, tags={enriched['tags']}")


if __name__ == "__main__":
    main()
