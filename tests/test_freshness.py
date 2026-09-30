"""캐시된 코스가 오래돼서 실제와 다른 길을 안내하는 상황을 막는다.

실시간 생성 결과를 DB에 쌓으면 다음 요청이 빨라지는 대신, 공사·도로 변경이 있어도
예전에 저장해둔 경로를 계속 주게 된다. 응답은 항상 즉시 주되(사용자를 기다리게 하지 않는다),
오래된 경로는 뒤에서 갱신해 다음 사람부터 최신 경로를 받게 한다.
"""
from datetime import datetime, timedelta, timezone

from src.recommend.freshness import ROUTE_TTL_DAYS, is_stale, stale_courses, stamp_verified

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def _course(cid, days_ago=None):
    c = {"id": cid, "path": [[34.7, 127.7], [34.71, 127.7]], "distance_km": 1.0}
    if days_ago is not None:
        c["route_verified_at"] = (NOW - timedelta(days=days_ago)).isoformat()
    return c


def test_recently_verified_course_is_fresh():
    assert not is_stale(_course("a", days_ago=1), now=NOW)


def test_course_past_ttl_is_stale():
    assert is_stale(_course("a", days_ago=ROUTE_TTL_DAYS + 1), now=NOW)


def test_course_never_verified_is_stale():
    """확인한 적이 없으면 최신인지 알 수 없으므로 갱신 대상으로 본다."""
    assert is_stale(_course("a"), now=NOW)


def test_broken_timestamp_is_treated_as_stale():
    course = _course("a")
    course["route_verified_at"] = "언젠가"
    assert is_stale(course, now=NOW)


def test_stale_courses_respects_limit():
    courses = [_course(f"c{i}") for i in range(5)]
    assert len(stale_courses(courses, limit=2, now=NOW)) == 2


def test_stale_courses_skips_fresh_ones():
    courses = [_course("fresh", days_ago=1), _course("old", days_ago=999)]
    assert [c["id"] for c in stale_courses(courses, limit=5, now=NOW)] == ["old"]


def test_stamp_makes_course_fresh_again():
    course = stamp_verified(_course("a"), now=NOW)
    assert not is_stale(course, now=NOW)
