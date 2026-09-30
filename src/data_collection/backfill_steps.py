"""이미 등재된 코스에 빠져 있는 턴바이턴 안내(steps)를 채워 넣는다.

초기 코스들은 턴바이턴 기능이 생기기 전에 등재돼서 path만 있고 steps가 없다. 그 상태로는
지도에 경로는 그려지는데 "우회전 후 OO로를 따라 213m" 같은 안내가 하나도 뜨지 않는다.

코스의 시작·끝 좌표로 Tmap 보행자 경로를 다시 요청해 steps를 가져온다. 다시 받은 경로가
저장된 경로와 다르면(수작업으로 좌표를 찍었거나 도로가 바뀐 경우) 코스 자체가 달라지는
것이므로 건너뛰고 보고만 한다 — 조용히 덮어쓰지 않는다.

사용:
    python -m src.data_collection.backfill_steps               # 실제 반영
    python -m src.data_collection.backfill_steps --dry-run     # 확인만
"""
import argparse
import json
import os
import time

from ..api_clients.tmap_pedestrian import extract_path, extract_steps, get_route

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "courses.json")
DISTANCE_TOLERANCE = 0.02  # 재경로 거리가 2% 넘게 다르면 다른 코스로 간주


def fetch_steps_for(course: dict) -> tuple:
    """(steps, 재경로_거리_km) 반환."""
    path = course["path"]
    start, end = path[0], path[-1]
    route = get_route((start[1], start[0]), (end[1], end[0]), course["name"], course["name"])
    _, distance_m = extract_path(route)
    return extract_steps(route), round(distance_m / 1000, 2)


def fetch_return_leg_for(course: dict) -> tuple:
    """왕복으로 뛸 때 쓸 돌아오는 구간. (return_path, return_steps) 반환.

    저장된 건 A->B 편도뿐이라, 왕복을 고르면 돌아오는 길 안내가 없다. 좌표를 뒤집어
    만들어낼 수는 없다 — 일방통행·횡단보도 때문에 갈 때와 올 때 안내가 실제로 다르다.
    그래서 B->A 경로를 따로 받아둔다.
    """
    path = course["path"]
    start, end = path[0], path[-1]
    route = get_route((end[1], end[0]), (start[1], start[0]), course["name"], course["name"])
    return_path, _ = extract_path(route)
    return return_path, extract_steps(route)


def backfill(db_path: str = DB_PATH, dry_run: bool = False, delay_s: float = 0.5) -> dict:
    with open(db_path, encoding="utf-8") as f:
        courses = json.load(f)

    filled, skipped, already = [], [], []
    for course in courses:
        if course.get("steps"):
            # 편도 안내는 있는데 왕복용 복귀 구간만 없는 코스는 그것만 채운다
            if not course.get("return_steps"):
                course["return_path"], course["return_steps"] = fetch_return_leg_for(course)
                filled.append((course["id"], f"복귀 {len(course['return_steps'])}"))
                time.sleep(delay_s)
            else:
                already.append(course["id"])
            continue

        steps, new_km = fetch_steps_for(course)
        old_km = course.get("distance_km", 0)
        drift = abs(new_km - old_km) / old_km if old_km else 1.0

        if not steps:
            skipped.append((course["id"], "Tmap이 안내 지점을 주지 않음"))
        elif drift > DISTANCE_TOLERANCE:
            skipped.append((course["id"], f"경로 불일치 {old_km}km -> {new_km}km"))
        else:
            course["steps"] = steps
            course.setdefault("route_type", "oneway")
            course["return_path"], course["return_steps"] = fetch_return_leg_for(course)
            filled.append((course["id"], f"{len(steps)} + 복귀 {len(course['return_steps'])}"))
        time.sleep(delay_s)

    if filled and not dry_run:
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(courses, f, ensure_ascii=False, indent=2)

    return {"filled": filled, "skipped": skipped, "already": already}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true", help="DB를 쓰지 않고 결과만 출력")
    args = parser.parse_args()

    result = backfill(args.db, dry_run=args.dry_run)
    for cid, n in result["filled"]:
        print(f"  채움   {cid:<28} 안내 {n}개")
    for cid, reason in result["skipped"]:
        print(f"  건너뜀 {cid:<28} {reason}")
    print(f"\n채움 {len(result['filled'])} / 건너뜀 {len(result['skipped'])} / 이미있음 {len(result['already'])}")
    if args.dry_run:
        print("(--dry-run 이므로 DB는 변경되지 않았습니다)")


if __name__ == "__main__":
    main()
