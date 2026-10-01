"""등재된 코스의 풍경 태그를 실제 지형 측정값으로 다시 붙인다.

예전 태그는 "경로 근처에 녹지가 조금이라도 있으면 숲길"이라 26개 중 22개가 숲길이었다.
경로의 몇 %가 실제로 그 지형을 지나는지 재서(`scenery.py`), 기준을 넘는 태그만 남긴다.
측정값은 `scenery`에 같이 저장해 추천 점수에서 "얼마나 그런 풍경인가"로 쓴다.

사용:
    python -m src.data_collection.backfill_scenery            # 실제 반영
    python -m src.data_collection.backfill_scenery --dry-run  # 확인만
    python -m src.data_collection.backfill_scenery --refresh  # 지형 데이터를 새로 받아서
"""
import argparse
import json
import os

from .scenery import build_indexes, measure, scenery_tags
from .scenery_osm import fetch_region, region_bbox

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "courses.json")


def backfill(db_path: str = DB_PATH, dry_run: bool = False, refresh: bool = False) -> list:
    with open(db_path, encoding="utf-8") as f:
        courses = json.load(f)

    by_region = {}
    for course in courses:
        by_region.setdefault(course.get("region", "기타"), []).append(course)

    changes = []
    for region, group in by_region.items():
        indexes = build_indexes(fetch_region(region, region_bbox(group), refresh=refresh))
        for course in group:
            measures = measure(course["path"], indexes)
            tags = scenery_tags(measures, course)
            changes.append((course["id"], course.get("tags", []), tags))
            if not dry_run:
                course["scenery"] = measures
                course["tags"] = tags
                course.pop("scenery_pending", None)

    if not dry_run:
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(courses, f, ensure_ascii=False, indent=2)
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="캐시를 무시하고 지형 데이터를 새로 받는다")
    args = parser.parse_args()

    for cid, before, after in backfill(args.db, dry_run=args.dry_run, refresh=args.refresh):
        print(f"  {cid:<22} {before} -> {after}")
    if args.dry_run:
        print("\n(--dry-run 이므로 DB는 변경되지 않았습니다)")


if __name__ == "__main__":
    main()
