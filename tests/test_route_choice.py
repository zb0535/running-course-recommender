"""왕복·순환·편도는 서로 다른 코스다. 사용자가 고른 대로 나와야 한다.

- 왕복(roundtrip): 갔던 길로 되돌아온다. 저장된 편도 코스를 두 배로 쓴다.
- 순환(loop): 한 바퀴 돌아 제자리로 온다. 경유지를 잡아 새로 만든다.
- 편도(oneway): 목적지에서 끝난다.
"""
import pytest
from fastapi import BackgroundTasks

from src.api import main

DB_COURSE = {
    "id": "stored", "name": "저장 코스", "region": "여수",
    "path": [[34.0, 127.0], [34.01, 127.0], [34.02, 127.0]],
    "distance_km": 2.0, "elevation_gain_m": 10, "tags": [], "traffic_signal_count": 0,
    "safety_score": 0.7, "steps": [{"lat": 34.0, "lng": 127.0, "description": "출발", "turn_type": 200}],
}
GENERATED_LOOP = {
    "id": "fresh-loop", "name": "순환 코스", "region": "실시간 생성",
    "path": [[34.0, 127.0], [34.005, 127.005], [34.0, 127.0]],
    "distance_km": 5.0, "elevation_gain_m": 20, "tags": [], "traffic_signal_count": 0,
    "safety_score": 0.7, "route_type": "loop", "source": "live_generated",
    "steps": [{"lat": 34.0, "lng": 127.0, "description": "출발", "turn_type": 200}],
}


@pytest.fixture
def stub(monkeypatch):
    calls = {"loop": [], "oneway": []}
    monkeypatch.setattr(main, "load_courses", lambda: [DB_COURSE])
    monkeypatch.setattr(main, "filter_nearby", lambda courses, lat, lng, radius: courses)

    def generate(lat, lng, km, tags, route_type="loop", night=False):
        calls[route_type].append(route_type)
        return [GENERATED_LOOP]

    monkeypatch.setattr(main, "generate_loop_candidates", generate)
    monkeypatch.setattr(main, "attach_actual_return_path", lambda course: course)
    return calls


def _ask(route_type, **extra):
    request = main.RecommendRequest(
        current_lat=34.0, current_lng=127.0, route_type=route_type,
        preferred_distance_km=5.0, use_live_environment=False, **extra
    )
    return main.post_recommend(request, BackgroundTasks())


def test_loop_generates_a_closed_course(stub):
    response = _ask("loop")
    assert stub["loop"], "순환을 골랐는데 순환 코스를 만들지 않았다"
    course = response["results"][0]["course"]
    assert course["path"][0] == course["path"][-1]


def test_roundtrip_does_not_turn_into_a_loop(stub):
    """왕복을 골랐는데 순환 코스가 나오면 사용자가 고른 의미가 없다."""
    response = _ask("roundtrip")
    assert not stub["loop"], "왕복을 골랐는데 순환 생성을 호출했다"
    course = response["results"][0]["course"]
    assert course["route_type"] == "roundtrip"
    assert course["distance_km"] == DB_COURSE["distance_km"] * 2


def test_oneway_keeps_the_stored_course_as_is(stub):
    course = _ask("oneway")["results"][0]["course"]
    assert course["route_type"] == "oneway"
    assert course["distance_km"] == DB_COURSE["distance_km"]


def test_loop_without_location_is_rejected_with_a_reason(stub):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        main.post_recommend(
            main.RecommendRequest(route_type="loop", preferred_distance_km=5.0, use_live_environment=False),
            BackgroundTasks(),
        )
    assert error.value.status_code == 400
    assert "현위치" in error.value.detail


def test_unknown_route_type_is_rejected():
    with pytest.raises(ValueError):
        main.RecommendRequest(route_type="circle")
