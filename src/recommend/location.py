"""사용자 현위치와 코스 사이 거리 계산 (코스 경로상 가장 가까운 지점 기준)."""
from ..data_collection.enrich import haversine_m


def distance_to_course_km(user_lat: float, user_lng: float, course: dict) -> float:
    path = course.get("path") or []
    if not path:
        return float("inf")
    nearest_m = min(haversine_m((user_lat, user_lng), (p[0], p[1])) for p in path)
    return nearest_m / 1000


def filter_nearby(courses: list, user_lat: float, user_lng: float, max_distance_km: float) -> list:
    return [c for c in courses if distance_to_course_km(user_lat, user_lng, c) <= max_distance_km]
