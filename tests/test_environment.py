import threading
import time

from src.recommend import environment
from src.recommend.environment import environment_score
from src.recommend.score import score_course

SAMPLE_COURSE = {
    "id": "test-course",
    "distance_km": 4,
    "elevation_gain_m": 20,
    "safety_score": 0.8,
    "traffic_signal_count": 0,
    "tags": ["바다뷰"],
}
SAMPLE_USER = {"preferred_distance_km": 4, "elevation_preference": "low", "environment_tags": {"바다뷰"}}
FORECAST = {"response": {"body": {"items": {"item": [{"category": "POP", "fcstValue": "10"}]}}}}


def _course(lat, lng):
    return {"id": f"c{lat}{lng}", "path": [[lat, lng], [lat + 0.001, lng]]}


def test_weather_is_fetched_once_per_forecast_grid(monkeypatch):
    """기상청 호출이 건당 2초라, 같은 격자를 요청마다 다시 부르면 추천 하나에 수십 초가 걸린다."""
    calls = []
    monkeypatch.setattr(environment, "get_short_term_forecast", lambda lat, lng: calls.append(1) or FORECAST)
    environment._cache.clear()
    environment.fetch_weather(34.7419, 127.7549)
    environment.fetch_weather(34.7420, 127.7550)  # 같은 5km 격자
    assert len(calls) == 1


def test_cached_weather_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(environment, "get_short_term_forecast", lambda lat, lng: calls.append(1) or FORECAST)
    environment._cache.clear()
    now = [1000.0]
    monkeypatch.setattr(environment.time, "time", lambda: now[0])
    environment.fetch_weather(34.74, 127.75)
    now[0] += environment.CACHE_TTL_S + 1
    environment.fetch_weather(34.74, 127.75)
    assert len(calls) == 2


def test_context_map_fetches_distinct_grids_in_parallel(monkeypatch):
    """코스 26개가 격자 11개에 걸쳐 있어 순서대로 받으면 첫 요청에 27초가 걸렸다."""
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow_forecast(lat, lng):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with lock:
            active[0] -= 1
        return FORECAST

    monkeypatch.setattr(environment, "get_short_term_forecast", slow_forecast)
    environment._cache.clear()
    courses = [_course(34.70 + i * 0.1, 127.70) for i in range(5)]  # 서로 다른 격자 5개
    started = time.time()
    context_map = environment.environment_context_map(courses)
    assert time.time() - started < 0.6  # 순차라면 1초
    assert peak[0] > 1
    assert all(ctx.get("pop") == "10" for ctx in context_map.values())


def test_every_course_gets_weather_from_its_own_location(monkeypatch):
    """일부 코스만 날씨가 반영되면 맑은 날 그 코스들만 점수가 오르는 편향이 생긴다."""
    monkeypatch.setattr(environment, "get_short_term_forecast", lambda lat, lng: FORECAST)
    environment._cache.clear()
    assert environment.get_environment_context(_course(35.15, 126.85)).get("pop") == "10"


def test_environment_score_no_data_is_neutral():
    assert environment_score({}) == 0.5


def test_environment_score_penalizes_rain_and_heat():
    good = environment_score({"pop": 0, "tmp": 20})
    rainy = environment_score({"pop": 80, "tmp": 20})
    scorching = environment_score({"pop": 0, "tmp": 35})
    assert good > rainy
    assert good > scorching


def test_score_course_uses_environment_weight_when_context_given():
    with_bad_env = score_course(SAMPLE_COURSE, SAMPLE_USER, env_context={"pop": 90})
    with_good_env = score_course(SAMPLE_COURSE, SAMPLE_USER, env_context={"pop": 0})
    assert with_good_env > with_bad_env
