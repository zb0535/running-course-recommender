"""등재된 코스의 `safety_score`를 실제 치안 데이터로 채운다.

지금까지는 모든 코스가 0.75 고정값이라 스코어링의 안전 가중치가 아무 코스도 구분하지
못했다. 코스마다 따로 조회하면 Overpass 속도 제한에 걸리므로, 지역별로 한 번에 받아서
로컬에서 경로별로 계산한다(26개 코스에 질의 2회).

사용:
    python -m src.data_collection.backfill_safety            # 실제 반영
    python -m src.data_collection.backfill_safety --dry-run  # 확인만
"""
import argparse
import json
import os
import time

import requests

from ..recommend.safety import safety_index, score_components
from .osm_overpass import HEADERS, OVERPASS_URL

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "courses.json")
BBOX_PAD_DEG = 0.02


def region_bbox(courses: list) -> tuple:
    lats = [p[0] for c in courses for p in c["path"]]
    lngs = [p[1] for c in courses for p in c["path"]]
    return (min(lats) - BBOX_PAD_DEG, min(lngs) - BBOX_PAD_DEG,
            max(lats) + BBOX_PAD_DEG, max(lngs) + BBOX_PAD_DEG)


def fetch_safety_features(bbox: tuple, retries: int = 4) -> dict:
    south, west, north, east = bbox
    area = f"{south},{west},{north},{east}"
    query = f"""
    [out:json][timeout:90];
    (node({area})["man_made"="surveillance"];
     node({area})["shop"="convenience"];
     node({area})["amenity"="police"];
     way({area})["amenity"="police"];);
    out center tags;
    """
    for attempt in range(retries):
        resp = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=120)
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            break
        time.sleep(20 * (attempt + 1))
    else:
        raise RuntimeError(f"Overpass 조회 실패 (bbox={area})")

    buckets = {"cctv": [], "convenience": [], "police": []}
    for el in resp.json().get("elements", []):
        tags = el.get("tags", {})
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lng = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        point = {"lat": lat, "lng": lng}
        if tags.get("man_made") == "surveillance":
            buckets["cctv"].append(point)
        elif tags.get("shop") == "convenience":
            buckets["convenience"].append(point)
        elif tags.get("amenity") == "police":
            buckets["police"].append(point)
    return buckets


def backfill(db_path: str = DB_PATH, dry_run: bool = False) -> list:
    with open(db_path, encoding="utf-8") as f:
        courses = json.load(f)

    by_region = {}
    for course in courses:
        by_region.setdefault(course.get("region", "기타"), []).append(course)

    results = []
    for region, group in by_region.items():
        features = fetch_safety_features(region_bbox(group))
        print(f"  [{region}] CCTV {len(features['cctv'])} · 편의점 {len(features['convenience'])} · 경찰 {len(features['police'])}")
        for course in group:
            parts = score_components(course["path"], **features)
            score = safety_index(course["path"], **features)
            results.append((course["id"], course.get("safety_score"), score, parts))
            if not dry_run:
                course["safety_score"] = score
                course["safety_breakdown"] = parts
        time.sleep(5)

    if not dry_run:
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(courses, f, ensure_ascii=False, indent=2)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for cid, before, after, parts in backfill(args.db, dry_run=args.dry_run):
        print(f"  {cid:<20} {before} -> {after}   (CCTV {parts['cctv']} · 유동 {parts['nightlife']} · 경찰 {parts['police']})")
    if args.dry_run:
        print("\n(--dry-run 이므로 DB는 변경되지 않았습니다)")


if __name__ == "__main__":
    main()
