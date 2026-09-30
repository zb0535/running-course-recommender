"""실시간 날씨를 코스 추천 점수에 반영.

- 모든 코스가 자기 경로 좌표로 날씨를 받는다. 일부 코스만 날씨가 반영되면, 맑은 날엔
  그 코스들만 점수가 올라가는 편향이 생긴다.
- 기상청 호출은 건당 2초 안팎이다. 예보 격자(5km) 단위로 30분 캐시하고, 서로 다른 격자는
  병렬로 받는다 — 순서대로 받으면 코스 26개(격자 11개)에 첫 요청이 27초 걸렸다.
"""
import time
from concurrent.futures import ThreadPoolExecutor

from ..api_clients.kma_weather import get_short_term_forecast, latlon_to_grid

CACHE_TTL_S = 30 * 60
MAX_PARALLEL = 12

_cache = {}


def _cached(key, fetch):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    value = fetch()
    if value:  # 실패(빈 값)는 저장하지 않는다 — 일시 오류가 30분간 굳지 않도록
        _cache[key] = (time.time(), value)
    return value


def _nearest_forecast_item(items: list, category: str):
    for it in items:
        if it.get("category") == category:
            return it.get("fcstValue")
    return None


def fetch_weather(lat: float, lng: float) -> dict:
    """POP(강수확률%), TMP(기온), SKY(하늘상태코드) 반환. 실패 시 빈 dict."""
    def fetch():
        try:
            resp = get_short_term_forecast(lat, lng)
            items = resp.get("response", {}).get("body", {}).get("items", {}).get("item", [])
            return {
                "pop": _nearest_forecast_item(items, "POP"),
                "tmp": _nearest_forecast_item(items, "TMP"),
                "sky": _nearest_forecast_item(items, "SKY"),
            }
        except Exception:
            return {}

    return _cached(("weather", latlon_to_grid(lat, lng)), fetch)


def _course_point(course: dict):
    path = course.get("path") or []
    return tuple(path[len(path) // 2]) if path else None


def get_environment_context(course: dict) -> dict:
    """코스 -> {pop, tmp, sky}. 조회에 실패하면 빈 dict(점수는 중립으로 처리됨)."""
    point = _course_point(course)
    return fetch_weather(*point) if point else {}


def environment_context_map(courses: list) -> dict:
    """{course_id: context}. 격자마다 한 번씩, 병렬로 받은 뒤 캐시에서 채운다."""
    by_grid = {}
    for course in courses:
        point = _course_point(course)
        if point:
            by_grid.setdefault(latlon_to_grid(*point), point)
    if by_grid:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(by_grid))) as pool:
            list(pool.map(lambda p: fetch_weather(*p), by_grid.values()))
    return {c["id"]: get_environment_context(c) for c in courses}


def environment_score(context: dict) -> float:
    """날씨가 지금 러닝하기 얼마나 적합한지 0~1. 데이터 없으면 중립값 0.5."""
    if not context:
        return 0.5

    score = 1.0

    pop = context.get("pop")
    if pop is not None:
        pop = int(pop)
        if pop >= 70:
            score -= 0.4
        elif pop >= 40:
            score -= 0.2
        elif pop >= 20:
            score -= 0.05

    tmp = context.get("tmp")
    if tmp is not None:
        tmp = float(tmp)
        if tmp >= 33 or tmp <= -5:
            score -= 0.15

    return max(0.0, min(1.0, score))
