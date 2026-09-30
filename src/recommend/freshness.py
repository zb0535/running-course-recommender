"""저장된 코스 경로가 아직 실제 길과 같은지 관리한다.

실시간 생성 결과를 DB에 쌓아두면 같은 동네 요청이 즉시 처리되지만, 그대로 두면
공사나 도로 변경이 있어도 예전 경로를 계속 안내하게 된다. 그렇다고 요청 때마다
확인하면 실시간 생성과 다를 게 없어진다.

그래서 응답은 저장된 경로로 즉시 주고, 오래된 코스는 뒤에서 다시 받아 갱신한다.
기다리는 사람은 없고, 다음 요청부터 최신 경로가 나간다.
"""
from datetime import datetime, timedelta, timezone

ROUTE_TTL_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_stale(course: dict, now: datetime = None, ttl_days: int = ROUTE_TTL_DAYS) -> bool:
    """마지막 확인 시점이 ttl_days보다 오래됐으면 True. 확인 기록이 없거나 깨졌어도 True."""
    stamped = course.get("route_verified_at")
    if not stamped:
        return True
    try:
        verified = datetime.fromisoformat(stamped)
    except ValueError:
        return True
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    return (now or _now()) - verified > timedelta(days=ttl_days)


def stale_courses(courses: list, limit: int, now: datetime = None, ttl_days: int = ROUTE_TTL_DAYS) -> list:
    """갱신이 필요한 코스를 limit개까지. 한 요청이 Tmap 할당량을 몰아 쓰지 않도록 제한한다."""
    found = []
    for course in courses:
        if len(found) >= limit:
            break
        if is_stale(course, now=now, ttl_days=ttl_days):
            found.append(course)
    return found


def stamp_verified(course: dict, now: datetime = None) -> dict:
    course["route_verified_at"] = (now or _now()).isoformat()
    return course
