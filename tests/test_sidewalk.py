"""달리기 코스는 인도로 간다. 인도 없는 차도는 지나가지 않고, 골목은 불가피할 때만 지나간다."""
import pytest

from src.api_clients.tmap_pedestrian import extract_path, extract_steps
from src.data_collection.scenery import build_indexes
from src.recommend import road_graph as rg
from src.recommend import sidewalk as sw

LAT, LNG = 35.0, 127.0
D = 0.001


def _way(wid, coords, **tags):
    return {"type": "way", "id": wid, "geometry": [{"lat": a, "lon": b} for a, b in coords], "tags": tags}


def _line(points, road_type=None, facility="11"):
    return {"geometry": {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in points]},
            "properties": {"roadType": road_type, "facilityType": facility}}


def _point(point, description="직진", turn=11):
    return {"geometry": {"type": "Point", "coordinates": [point[1], point[0]]},
            "properties": {"description": description, "turnType": turn}}


@pytest.fixture(autouse=True)
def facts_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "FACTS_PATH", str(tmp_path / "facts.sqlite"))


@pytest.fixture
def town():
    """A → B로 가는 길 셋: 큰길(가장 짧음), 골목, 멀리 도는 산책로."""
    a, b = (LAT, LNG), (LAT, LNG + 4 * D)
    ways = [
        _way(1, [a, (LAT, LNG + 2 * D), b], highway="secondary", name="대로"),
        _way(2, [a, (LAT + 2 * D, LNG), (LAT + 2 * D, LNG + 4 * D), b], highway="residential", name="골목"),
        _way(3, [a, (LAT - 8 * D, LNG), (LAT - 8 * D, LNG + 4 * D), b], highway="footway", name="산책로"),
    ]
    return rg.RoadGraph(ways, build_indexes({"all": ways}), rg.PRIOR, facts={}), a, b


class TestKinds:
    def test_tmap_segment_kinds(self):
        assert sw.kind_of({"roadType": 21, "facilityType": "11"}) == "sidewalk"
        assert sw.kind_of({"roadType": 23, "facilityType": "11"}) == "car_free"
        assert sw.kind_of({"roadType": 21, "facilityType": "15"}) == "crossing"
        assert sw.kind_of({"roadType": 22, "facilityType": "11"}) == "alley"
        assert sw.kind_of({"roadType": 0, "facilityType": "11"}) == "unknown"

    def test_unseparated_segment_on_a_main_road_is_a_car_lane(self, town):
        graph, a, b = town
        on_main_road = {"features": [_line([a, (LAT, LNG + 2 * D), b], 22)]}
        on_alley = {"features": [_line([(LAT + 2 * D, LNG), (LAT + 2 * D, LNG + 4 * D)], 22)]}
        assert sw.resolve(on_main_road, graph)[0][0] == "car_lane"
        assert sw.resolve(on_alley, graph)[0][0] == "alley"

    def test_untyped_segment_on_a_walkway_counts_as_car_free(self, town):
        """강변 산책로·공원 길은 Tmap이 종류를 주지 않는 경우가 많다. 도로망에서 보행 전용길이면 인정한다."""
        graph, _, _ = town
        route = {"features": [_line([(LAT - 8 * D, LNG), (LAT - 8 * D, LNG + 4 * D)], 0)]}
        assert sw.resolve(route, graph)[0][0] == "car_free"


class TestConfirm:
    def _tmap(self, monkeypatch, reply):
        calls = []

        def fake(start, end, start_name="", end_name="", waypoints=None):
            calls.append(waypoints)
            return reply

        monkeypatch.setattr(sw, "get_route", fake)
        return calls

    def _main_road(self, a, b):
        return [a, (LAT, LNG + 2 * D), b]

    def test_main_road_with_a_confirmed_sidewalk_is_used_as_planned(self, town, monkeypatch):
        graph, a, b = town
        calls = self._tmap(monkeypatch, {"features": [_line(self._main_road(a, b), 21)]})
        plan = lambda: rg.route(graph, a, b)
        result = sw.confirm(graph, a, b, [], plan(), plan)
        assert len(calls) == 1
        assert result["no_car_lanes"] is True and result["sidewalk_only"] is True
        assert result["road_mix"]["sidewalk"] == 1.0
        assert [LAT, LNG + 2 * D] in extract_path(result)[0], "직접 짠 경로를 그대로 내보낸다"

    def test_main_road_without_a_sidewalk_is_blocked_and_planned_around(self, town, monkeypatch):
        graph, a, b = town
        calls = self._tmap(monkeypatch, {"features": [_line(self._main_road(a, b), 22)]})
        plan = lambda: rg.route(graph, a, b)
        first = plan()
        assert [LAT, LNG + 2 * D] in extract_path(first)[0], "처음엔 가장 짧은 큰길로 간다"

        result = sw.confirm(graph, a, b, [], first, plan)
        assert len(calls) == 1, "다시 짠 경로에 모르는 큰길이 없으면 더 묻지 않는다"
        assert graph.surfaces[0] == rg.BLOCKED
        assert result["no_car_lanes"] is True
        assert [LAT, LNG + 2 * D] not in extract_path(result)[0], "인도 없는 큰길을 지나갔다"
        assert result["road_mix"]["alley"] == 1.0 and result["sidewalk_only"] is False   # 골목으로 돌아간다

    def test_what_was_learned_is_remembered_for_the_next_request(self, town, monkeypatch):
        graph, a, b = town
        self._tmap(monkeypatch, {"features": [_line(self._main_road(a, b), 22)]})
        plan = lambda: rg.route(graph, a, b)
        sw.confirm(graph, a, b, [], plan(), plan)
        fresh = rg.RoadGraph([_way(1, self._main_road(a, b), highway="secondary")], {}, rg.PRIOR)
        assert fresh.surfaces[0] == rg.BLOCKED

    def test_known_roads_need_no_tmap_call(self, town, monkeypatch):
        graph, a, b = town
        graph.surfaces[0] = "sidewalk"   # 이전 요청에서 확인해 둔 길
        calls = self._tmap(monkeypatch, {"features": []})
        plan = lambda: rg.route(graph, a, b)
        assert sw.confirm(graph, a, b, [], plan(), plan)["no_car_lanes"] is True
        assert calls == []

    def test_course_that_cannot_avoid_a_car_lane_is_marked_unusable(self, monkeypatch):
        a, b = (LAT, LNG), (LAT, LNG + 4 * D)
        ways = [_way(1, self._main_road(a, b), highway="secondary")]
        graph = rg.RoadGraph(ways, build_indexes({"all": ways}), rg.PRIOR, facts={})
        self._tmap(monkeypatch, {"features": [_line(self._main_road(a, b), 22)]})
        plan = lambda: rg.route(graph, a, b)
        assert sw.confirm(graph, a, b, [], plan(), plan)["no_car_lanes"] is False

    def test_alley_is_avoided_once_known_when_a_walkway_exists(self, town):
        """골목으로 확인된 길은 크게 불리해진다 — 조금 더 돌아도 산책로가 있으면 그쪽으로 간다."""
        graph, a, b = town
        graph.surfaces[0] = rg.BLOCKED
        graph.surfaces[1] = "alley"
        path, _ = extract_path(rg.route(graph, a, b))
        assert all(p[0] <= LAT for p in path), "산책로가 있는데 골목으로 갔다"

    def test_without_tmap_an_unverified_main_road_is_not_used(self, town, monkeypatch):
        """Tmap을 쓸 수 없으면 확인할 수 없다. 모르는 큰길은 쓰지 않고 다른 길로 간다."""
        graph, a, b = town

        def no_key(*args, **kwargs):
            raise RuntimeError("TMAP_APP_KEY 환경변수가 설정되지 않았습니다.")

        monkeypatch.setattr(sw, "get_route", no_key)
        plan = lambda: rg.route(graph, a, b)
        result = sw.confirm(graph, a, b, [], plan(), plan)
        assert result["no_car_lanes"] is True
        assert [LAT, LNG + 2 * D] not in extract_path(result)[0]

    def test_start_far_from_any_road_reports_the_walk_to_the_course(self, town, monkeypatch):
        graph, a, b = town
        graph.surfaces[0] = "sidewalk"
        off_road = (LAT, LNG - 0.001)   # 길에서 약 91m
        plan = lambda: rg.route(graph, off_road, b)
        assert sw.confirm(graph, off_road, b, [], plan(), plan)["access_m"] == pytest.approx(91, abs=3)
