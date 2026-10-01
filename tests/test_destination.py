"""목적지를 직접 고르는 코스.

"오동도까지 뛰고 싶다"는 거리·태그로 표현되지 않는 요구다. 목적지를 주면 그 지점까지
실제 보행 경로를 만들고, 왕복이면 돌아오는 길까지 붙인다.
"""
import pytest
from fastapi import BackgroundTasks

from src.api import main
from src.recommend import destination

ROUTE = {
    "features": [
        {"geometry": {"type": "Point", "coordinates": [127.0, 34.0]},
         "properties": {"totalDistance": 4200}},
        {"geometry": {"type": "LineString", "coordinates": [[127.0, 34.0], [127.01, 34.01], [127.02, 34.02]]}},
    ]
}


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(destination, "geocode", lambda name: (34.02, 127.02))
    monkeypatch.setattr(destination, "get_route", lambda *a, **k: ROUTE)
    monkeypatch.setattr(destination, "sample_elevation_gain", lambda path: 12.0)


def test_course_runs_to_the_requested_destination(stub):
    course = destination.course_to_destination(34.0, 127.0, "오동도, 여수", route_type="oneway")
    assert course["path"][-1] == [34.02, 127.02]
    assert course["distance_km"] == 4.2
    assert "오동도" in course["name"]


def test_destination_can_be_given_as_coordinates(monkeypatch, stub):
    monkeypatch.setattr(destination, "geocode", lambda name: pytest.fail("좌표를 줬는데 검색했다"))
    course = destination.course_to_destination(34.0, 127.0, None, 34.02, 127.02, route_type="oneway")
    assert course["path"][-1] == [34.02, 127.02]


def test_unknown_place_says_so(monkeypatch, stub):
    monkeypatch.setattr(destination, "geocode", lambda name: (_ for _ in ()).throw(ValueError("찾지 못했습니다")))
    with pytest.raises(ValueError):
        destination.course_to_destination(34.0, 127.0, "없는장소", route_type="oneway")


def test_api_returns_a_course_to_the_destination(monkeypatch, stub):
    monkeypatch.setattr(main, "course_to_destination",
                        lambda *a, **k: {"id": "to-dest", "name": "목적지 코스", "region": "실시간 생성",
                                         "path": [[34.0, 127.0], [34.02, 127.02]], "distance_km": 4.2,
                                         "elevation_gain_m": 12, "tags": [], "traffic_signal_count": 0,
                                         "safety_score": 0.7, "route_type": "oneway", "source": "live_generated",
                                         "steps": []})
    response = main.post_recommend(
        main.RecommendRequest(current_lat=34.0, current_lng=127.0, destination="오동도, 여수",
                              route_type="oneway", use_live_environment=False),
        BackgroundTasks(),
    )
    assert response["source"] == "destination"
    assert response["results"][0]["course"]["id"] == "to-dest"


def test_destination_needs_a_starting_point():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        main.post_recommend(
            main.RecommendRequest(destination="오동도, 여수", use_live_environment=False),
            BackgroundTasks(),
        )
    assert error.value.status_code == 400
    assert "현위치" in error.value.detail
