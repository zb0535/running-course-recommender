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
    monkeypatch.setattr(destination, "search_places", lambda name, lat, lng: [{"name": "오동도", "address": "전남 여수시 수정동", "lat": 34.02, "lng": 127.02, "category": "관광명소", "distance_km": 2.9}, {"name": "오동도펜션", "address": "전남 여수시 종화동", "lat": 34.01, "lng": 127.01, "category": "숙박", "distance_km": 1.0}])
    monkeypatch.setattr(destination, "get_route", lambda *a, **k: ROUTE)
    monkeypatch.setattr(destination, "sample_elevation_gain", lambda path: 12.0)


def test_course_runs_to_the_requested_destination(stub):
    course = destination.course_to_destination(34.0, 127.0, "오동도, 여수", route_type="oneway")
    assert course["path"][-1] == [34.02, 127.02]
    assert course["distance_km"] == 4.2
    assert "오동도" in course["name"]


def test_destination_can_be_given_as_coordinates(monkeypatch, stub):
    monkeypatch.setattr(destination, "search_places", lambda *a: pytest.fail("좌표를 줬는데 검색했다"))
    course = destination.course_to_destination(34.0, 127.0, None, 34.02, 127.02, route_type="oneway")
    assert course["path"][-1] == [34.02, 127.02]


def test_unknown_place_says_so(monkeypatch, stub):
    monkeypatch.setattr(destination, "search_places", lambda *a: [])
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


def test_course_says_which_place_it_chose_and_offers_the_others(stub):
    """같은 이름의 장소가 여러 곳일 수 있다. 어디로 정했는지와 다른 후보를 같이 준다."""
    course = destination.course_to_destination(34.0, 127.0, "오동도", route_type="oneway")
    assert course["destination"]["name"] == "오동도"
    assert course["name"] == "오동도까지"
    assert [p["name"] for p in course["destination_alternatives"]] == ["오동도펜션"]


class TestPlaceSearch:
    def _reply(self, monkeypatch, pois, status=200):
        from src.api_clients import tmap_poi

        class Reply:
            status_code = status
            text = "x" if pois is not None else ""

            def raise_for_status(self):
                pass

            def json(self):
                return {"searchPoiInfo": {"pois": {"poi": pois}}}

        calls = []
        monkeypatch.setenv("TMAP_APP_KEY", "test-key")
        monkeypatch.setattr(tmap_poi.requests, "get", lambda url, **k: calls.append((url, k)) or Reply())
        return tmap_poi, calls

    def _poi(self, name, lat, lng, **extra):
        return {"name": name, "noorLat": str(lat), "noorLon": str(lng), "frontLat": str(lat), "frontLon": str(lng),
                "upperAddrName": "전남", "middleAddrName": "여수시", "lowerAddrName": "수정동", **extra}

    def test_search_is_centred_on_where_the_runner_is(self, monkeypatch):
        tmap_poi, calls = self._reply(monkeypatch, [self._poi("오동도", 34.741, 127.7545, middleBizName="관광명소")])
        places = tmap_poi.search_places("오동도", 34.7393, 127.7359)
        assert calls[0][1]["params"]["centerLat"] == 34.7393
        assert places[0]["name"] == "오동도" and places[0]["category"] == "관광명소"
        assert places[0]["address"] == "전남 여수시 수정동"
        assert places[0]["distance_km"] == pytest.approx(1.7, abs=0.1)

    def test_entrance_coordinates_are_used(self, monkeypatch):
        """중심 좌표는 건물 한가운데나 섬 한복판이다. 걸어서 들어가는 입구 좌표를 쓴다."""
        poi = self._poi("오동도", 34.745, 127.767)
        poi.update(frontLat="34.741", frontLon="127.7545")
        tmap_poi, _ = self._reply(monkeypatch, [poi])
        place = tmap_poi.search_places("오동도", 34.7393, 127.7359)[0]
        assert (place["lat"], place["lng"]) == (34.741, 127.7545)

    def test_car_parks_and_gates_at_the_same_spot_are_not_listed_twice(self, monkeypatch):
        tmap_poi, _ = self._reply(monkeypatch, [
            self._poi("이순신광장", 34.7395, 127.7366), self._poi("이순신광장 지하주차장", 34.7395, 127.7366),
            self._poi("이순신광장 뒤 공영주차장", 34.7372, 127.7392)])
        names = [p["name"] for p in tmap_poi.search_places("이순신광장", 34.7393, 127.7359)]
        assert names == ["이순신광장", "이순신광장 뒤 공영주차장"]

    def test_same_name_in_a_far_away_city_is_dropped_when_a_near_one_exists(self, monkeypatch):
        tmap_poi, _ = self._reply(monkeypatch, [
            self._poi("광주시청", 35.159, 126.8531), self._poi("광주시청", 37.4296, 127.2546)])   # 광주광역시 / 경기 광주시
        places = tmap_poi.search_places("광주시청", 35.1565, 126.8386)
        assert len(places) == 1 and places[0]["lat"] == 35.159

    def test_far_results_are_kept_when_nothing_is_near(self, monkeypatch):
        tmap_poi, _ = self._reply(monkeypatch, [self._poi("해운대해수욕장", 35.1594, 129.1601)])
        assert tmap_poi.search_places("해운대해수욕장", 34.7393, 127.7359)

    def test_no_match_gives_an_empty_list(self, monkeypatch):
        tmap_poi, _ = self._reply(monkeypatch, None, status=204)
        monkeypatch.setattr(tmap_poi, "_nominatim", lambda *a: [])
        assert tmap_poi.search_places("없는장소", 34.7393, 127.7359) == []

    def test_without_a_tmap_key_the_fallback_search_is_used(self, monkeypatch):
        from src.api_clients import tmap_poi

        monkeypatch.delenv("TMAP_APP_KEY", raising=False)
        monkeypatch.setattr(tmap_poi, "_tmap", lambda *a: pytest.fail("키가 없는데 Tmap을 불렀다"))
        monkeypatch.setattr(tmap_poi, "_nominatim", lambda q, lat, lng, n: [
            {"name": "오동도", "address": "여수시", "lat": 34.74, "lng": 127.76, "category": "", "distance_km": 2.0}])
        assert tmap_poi.search_places("오동도", 34.7393, 127.7359)[0]["name"] == "오동도"

    def test_places_endpoint(self, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setattr(main, "search_places", lambda q, lat, lng: [{"name": q, "lat": lat, "lng": lng}])
        body = TestClient(main.app).get("/places", params={"q": "오동도", "lat": 34.7, "lng": 127.7}).json()
        assert body == {"places": [{"name": "오동도", "lat": 34.7, "lng": 127.7}]}


class TestOwnRouteToDestination:
    """목적지까지도 추천 코스와 같은 규칙으로 간다: 인도 없는 차도는 피한다."""

    def _area(self, monkeypatch, ways):
        from src.data_collection import area_cache, local_osm

        area = area_cache.Area((33.9, 126.9, 34.1, 127.1), ways, local=True)
        monkeypatch.setattr(local_osm, "available", lambda: True)
        monkeypatch.setattr(area_cache, "load_area", lambda *a, **k: area)
        monkeypatch.setattr(area_cache, "cached_area_for_path", lambda path: None)
        monkeypatch.setattr(destination, "sample_elevation_gain", lambda path: 3.0)

    def _way(self, wid, coords, **tags):
        return {"type": "way", "id": wid, "geometry": [{"lat": a, "lon": b} for a, b in coords], "tags": tags,
                "dead_end": False}

    def test_walkway_is_taken_and_reported(self, monkeypatch):
        a, b = (34.0, 127.0), (34.0, 127.004)
        self._area(monkeypatch, [
            self._way(1, [a, b], highway="secondary", sidewalk="no"),                       # 인도 없는 차도 (가장 짧다)
            self._way(2, [a, (34.001, 127.0), (34.001, 127.004), b], highway="footway"),    # 돌아가는 보행로
        ])
        monkeypatch.setattr(destination, "get_route", lambda *args, **k: pytest.fail("직접 갈 수 있는데 Tmap을 불렀다"))
        course = destination.course_to_destination(34.0, 127.0, None, 34.0, 127.004)
        assert course["no_car_lanes"] is True and course["road_mix"]["car_free"] == 1.0
        assert [34.001, 127.004] in [[round(p[0], 6), round(p[1], 6)] for p in course["path"]]
        assert course["steps"][-1]["description"] == "도착"

    def test_falls_back_to_tmap_and_says_so_when_no_safe_way_exists(self, monkeypatch, stub):
        a, b = (34.0, 127.0), (34.02, 127.02)
        self._area(monkeypatch, [self._way(1, [a, b], highway="secondary", sidewalk="no")])
        course = destination.course_to_destination(34.0, 127.0, None, 34.02, 127.02)
        assert course["no_car_lanes"] is None, "확인하지 못한 경로를 확인된 것처럼 내보냈다"
        assert "router" not in course
