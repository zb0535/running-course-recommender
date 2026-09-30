"""저장된 코스의 경로를 Tmap에서 다시 받아 최신 상태로 갱신한다.

`backfill_steps`가 비어 있던 안내를 한 번 채우는 일회성 작업이라면, 이쪽은 이미 채워진
경로가 여전히 유효한지 주기적으로 확인하는 쪽이다. 공사나 도로 변경으로 길이 바뀌었다면
바뀐 경로를 그대로 받아들인다 — 오래된 경로를 계속 안내하지 않는 것이 목적이므로,
여기서는 변경을 건너뛰지 않고 반영하되 언제 바뀌었는지 기록해 추적할 수 있게 한다.
"""
import json
import os

from ..api_clients.tmap_pedestrian import extract_path, extract_steps, get_route
from ..recommend.freshness import stamp_verified

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "courses.json")
CHANGE_THRESHOLD = 0.02  # 거리가 2% 넘게 달라지면 길 자체가 바뀐 것으로 본다


def refresh_course_in_db(course_id: str, db_path: str = DB_PATH) -> dict:
    """해당 코스의 경로·안내를 다시 받아 DB에 반영하고 갱신된 코스를 반환. 없으면 None."""
    with open(db_path, encoding="utf-8") as f:
        courses = json.load(f)

    course = next((c for c in courses if c["id"] == course_id), None)
    if not course or not course.get("path"):
        return None

    start, end = course["path"][0], course["path"][-1]
    route = get_route((start[1], start[0]), (end[1], end[0]), course.get("name", ""), course.get("name", ""))
    new_path, distance_m = extract_path(route)
    if not new_path:
        return None

    new_km = round(distance_m / 1000, 2)
    old_km = course.get("distance_km") or new_km
    if abs(new_km - old_km) / old_km > CHANGE_THRESHOLD:
        course["route_changed_at"] = course.get("route_verified_at") or ""
        course["previous_distance_km"] = old_km

    course["path"] = new_path
    course["steps"] = extract_steps(route)
    course["distance_km"] = new_km
    stamp_verified(course)

    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(courses, f, ensure_ascii=False, indent=2)
    return course
