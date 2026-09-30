"""코스 DB 자체의 무결성 검사.

추천 로직이 아무리 정확해도 코스 데이터에 구멍이 있으면 실사용에서 바로 드러난다.
특히 steps(턴바이턴 안내)는 빠져 있어도 추천 응답은 200으로 잘 나가기 때문에
테스트로 잡지 않으면 "지도는 그려지는데 안내만 안 나오는" 상태를 놓치게 된다.
"""
import json
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "courses.json")

with open(DB_PATH, encoding="utf-8") as f:
    COURSES = json.load(f)


def test_db_is_not_empty():
    assert len(COURSES) > 0


def test_every_course_has_turn_by_turn_steps():
    missing = [c["id"] for c in COURSES if not c.get("steps")]
    assert not missing, f"턴바이턴 안내가 없는 코스: {missing}"


def test_steps_stay_on_the_course_path():
    """안내 지점이 경로 밖에 있으면 내비게이션이 엉뚱한 곳에서 안내한다."""
    from src.data_collection.enrich import haversine_m

    for c in COURSES:
        path = c["path"]
        for step in c.get("steps", []):
            nearest = min(haversine_m((step["lat"], step["lng"]), (p[0], p[1])) for p in path)
            assert nearest < 50, f"{c['id']}: 안내 지점이 경로에서 {nearest:.0f}m 떨어져 있음"


def test_every_course_declares_route_type():
    missing = [c["id"] for c in COURSES if c.get("route_type") not in ("oneway", "roundtrip")]
    assert not missing, f"route_type이 없는 코스: {missing}"
