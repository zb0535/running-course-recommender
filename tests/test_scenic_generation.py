"""실시간 생성도 요청한 풍경을 실제로 지나게 만든다.

예전에는 주변 길로 고리를 만든 뒤 풍경을 재보고 아니면 버렸다. 강변을 요청해도 강 쪽으로
가지 않으니 결과가 없었다. 받아 둔 지형에서 요청한 풍경 위의 지점을 경유지로 잡는다.
"""
import math

import pytest

from src.data_collection import area_cache
from src.data_collection.area_cache import Area
from src.data_collection.enrich import haversine_m
from src.recommend import generate_live as g

LAT, LNG = 35.0, 127.0
D = 0.001  # 약 110m(위도)


def _way(wid, coords, **tags):
    nodes = [wid * 1000 + i for i in range(len(coords))]
    return {"type": "way", "id": wid, "nodes": nodes,
            "geometry": [{"lat": a, "lon": b} for a, b in coords], "tags": tags}


def _grid_roads(half=0.02, step=0.002):
    """출발점 주변 격자 도로망 (모든 길이 서로 이어져 막다른 길이 없다)."""
    roads, wid = [], 1
    n = int(half / step)
    lines = [LAT + i * step for i in range(-n, n + 1)]
    cols = [LNG + i * step for i in range(-n, n + 1)]
    node = lambda r, c: 100000 + (r + n) * 1000 + (c + n)
    for r, lat in enumerate(range(-n, n + 1)):
        way = _way(wid, [(LAT + lat * step, c) for c in cols], highway="residential")
        way["nodes"] = [node(lat, i) for i in range(-n, n + 1)]
        roads.append(way); wid += 1
    for c, lng in enumerate(range(-n, n + 1)):
        way = _way(wid, [(l, LNG + lng * step) for l in lines], highway="residential")
        way["nodes"] = [node(i, lng) for i in range(-n, n + 1)]
        roads.append(way); wid += 1
    return roads


def _area(extra):
    return Area((LAT - 0.03, LNG - 0.03, LAT + 0.03, LNG + 0.03), _grid_roads() + extra)


def _bearing_deg(point):
    return math.degrees(math.atan2(point[0] - LAT, (point[1] - LNG) * math.cos(math.radians(LAT)))) % 360


class TestScenicWaypoints:
    def test_waypoints_follow_a_river_running_through_the_start(self):
        river = _way(9001, [(LAT, LNG - 0.03), (LAT, LNG + 0.03)], waterway="river")
        waypoints = g.scenic_vertices(_area([river]), {"강변"}, LAT, LNG, 5.0)
        on_river = [w for w in waypoints if abs(w[0] - LAT) * 111_320 <= 80]
        assert len(on_river) >= 2, "강 위에 경유지를 잡지 않았다"
        sides = {"east" if w[1] > LNG else "west" for w in on_river}
        assert sides == {"east", "west"}, "강을 한쪽으로만 따라가면 같은 길을 되짚게 된다"
        assert len(waypoints) > len(on_river), "강 위 지점만으로는 고리가 안 된다 — 벗어났다 돌아오는 지점이 필요하다"

    def test_waypoints_follow_a_coast_to_one_side(self):
        coast = _way(9002, [(LAT - 0.006, LNG - 0.03), (LAT - 0.006, LNG + 0.03)], natural="coastline")
        waypoints = g.scenic_vertices(_area([coast]), {"바다뷰"}, LAT, LNG, 5.0)
        assert waypoints
        near_coast = [w for w in waypoints if abs(w[0] - (LAT - 0.006)) * 111_320 <= 200]
        assert len(near_coast) >= 2

    def test_waypoints_are_ordered_around_the_start(self):
        """경유지를 방향 순서대로 돌아야 고리가 된다. 순서가 엉키면 경로가 자기 자신과 교차한다."""
        river = _way(9001, [(LAT, LNG - 0.03), (LAT, LNG + 0.03)], waterway="river")
        bearings = [_bearing_deg(w) for w in g.scenic_vertices(_area([river]), {"강변"}, LAT, LNG, 5.0)]
        turns = [(b - a) % 360 for a, b in zip(bearings, bearings[1:])]
        assert all(t <= 180 for t in turns) or all(t >= 180 for t in turns)

    def test_no_such_scenery_nearby_gives_nothing(self):
        assert g.scenic_vertices(_area([]), {"강변"}, LAT, LNG, 5.0) == []

    def test_scenery_that_cannot_be_steered_towards_is_ignored(self):
        """'전망'·'도심' 같은 풍경은 따라갈 선이나 면이 아니다. 평소대로 만들고 측정으로만 확인한다."""
        assert g.scenic_vertices(_area([]), {"전망"}, LAT, LNG, 5.0) == []


class TestGenerationUsesTheArea:
    def _route_stub(self, monkeypatch, calls):
        def fake_route(start, end, start_name="", end_name="", waypoints=None):
            calls.append(waypoints)
            points = [start] + list(waypoints or []) + [end]
            dist = sum(haversine_m((a[1], a[0]), (b[1], b[0])) for a, b in zip(points, points[1:]))
            return {"features": [
                {"geometry": {"type": "Point", "coordinates": list(start)}, "properties": {"totalDistance": int(dist)}},
                {"geometry": {"type": "LineString", "coordinates": [list(p) for p in points]}},
            ]}

        monkeypatch.setattr(g, "get_route", fake_route)
        monkeypatch.setattr(g, "sample_elevation_gain", lambda path: 5.0)

    def test_no_overpass_call_when_the_area_is_already_stored(self, monkeypatch):
        calls = []
        self._route_stub(monkeypatch, calls)
        monkeypatch.setattr(area_cache, "load_area", lambda *a, **k: _area([]))
        monkeypatch.setattr(g, "post_overpass_query", lambda *a, **k: pytest.fail("Overpass를 불렀다"))
        assert g.generate_loop_candidates(LAT, LNG, 5.0, set())

    def test_requested_scenery_ends_up_on_the_generated_loop(self, monkeypatch):
        calls = []
        self._route_stub(monkeypatch, calls)
        river = _way(9001, [(LAT, LNG - 0.03), (LAT, LNG + 0.03)], waterway="river")
        monkeypatch.setattr(area_cache, "load_area", lambda *a, **k: _area([river]))
        courses = g.generate_loop_candidates(LAT, LNG, 5.0, {"강변"})
        assert any("강변" in c["tags"] for c in courses), [c["tags"] for c in courses]

    def test_one_way_course_is_as_long_as_requested(self, monkeypatch):
        """편도인데 순환용 반경 공식을 쓰면 5km 요청에 1km가 나온다."""
        calls = []
        self._route_stub(monkeypatch, calls)
        monkeypatch.setattr(area_cache, "load_area", lambda *a, **k: _area([]))
        course = g.generate_loop_candidates(LAT, LNG, 2.0, set(), route_type="oneway")[0]
        assert course["distance_km"] == pytest.approx(2.0, rel=0.35)
        assert course["route_type"] == "oneway"

    def test_round_trip_goes_half_the_distance_out(self, monkeypatch):
        """왕복 5km는 2.5km 나갔다 오는 것이다. 생성 코스는 편도 경로로 주고, 복귀는 호출 쪽이 붙인다."""
        calls = []
        self._route_stub(monkeypatch, calls)
        monkeypatch.setattr(area_cache, "load_area", lambda *a, **k: _area([]))
        course = g.generate_loop_candidates(LAT, LNG, 4.0, set(), route_type="roundtrip")[0]
        assert course["distance_km"] == pytest.approx(2.0, rel=0.35)
        assert course["route_type"] == "oneway"


class TestWater:
    def test_waypoints_are_not_placed_across_the_sea(self):
        """바다 건너편 길은 가까워 보여도 다리를 찾아 멀리 돌아야 한다. 경유지로 잡지 않는다."""
        coast = _way(9002, [(LAT - 0.004, LNG - 0.03), (LAT - 0.004, LNG + 0.03)], natural="coastline")
        area = _area([coast])
        for waypoints in (g.scenic_vertices(area, {"바다뷰"}, LAT, LNG, 5.0),
                          g.pick_land_vertices(LAT, LNG, 5.0, area.roads, area=area)):
            assert waypoints
            assert all(w[0] >= LAT - 0.004 for w in waypoints), waypoints


class TestSafetyOfGeneratedCourses:
    def test_safety_and_signals_are_measured_not_fixed(self, monkeypatch):
        """실시간 생성 코스의 안전 점수는 0.7 고정이었다. 그 자리의 CCTV·편의점·경찰·신호등으로 계산한다."""
        from src.recommend import sidewalk

        monkeypatch.setattr(g, "sample_elevation_gain", lambda path: 5.0)
        monkeypatch.setattr(sidewalk, "get_route", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no key")))
        node = lambda i, lat, lng, **tags: {"type": "node", "id": 5000 + i, "lat": lat, "lon": lng, "tags": tags}
        facilities = (
            [node(i, LAT + 0.002 * i, LNG, man_made="surveillance") for i in range(-4, 5)]
            + [node(20 + i, LAT, LNG + 0.002 * i, shop="convenience") for i in range(-4, 5)]
            + [node(40, LAT + 0.001, LNG + 0.001, amenity="police")]
            + [node(50 + i, LAT + 0.002 * i, LNG + 0.002 * i, highway="traffic_signals") for i in range(-3, 4)]
        )
        roads = [dict(way, dead_end=False) for way in _grid_roads()]
        area = Area((LAT - 0.03, LNG - 0.03, LAT + 0.03, LNG + 0.03), roads + facilities, local=True)
        monkeypatch.setattr(area_cache, "load_area", lambda *a, **k: area)
        monkeypatch.setattr(area_cache, "cached_area_for_path", lambda path: area)

        course = g.generate_loop_candidates(LAT, LNG, 4.0, set())[0]
        assert course["safety_score"] != 0.7
        assert set(course["safety_breakdown"]) == {"cctv", "nightlife", "police"}
        assert course["safety_breakdown"]["police"] == 1.0
        assert course["traffic_signal_count"] >= 1
        assert course["lively_ratio"] is not None and course["planned_for_night"] is False


def test_at_night_the_least_secluded_candidates_are_kept():
    quiet = {"id": "a", "distance_km": 5.0, "secluded_ratio": 0.31}
    busy = {"id": "b", "distance_km": 4.1, "secluded_ratio": 0.07}
    similar = {"id": "c", "distance_km": 4.5, "secluded_ratio": 0.10}
    unknown = {"id": "d", "distance_km": 5.0}
    assert [c["id"] for c in g.safest_at_night([quiet, busy, similar, unknown])] == ["b", "c"]
    assert g.safest_at_night([unknown]) == [unknown]   # 잴 수 없는 것뿐이면 그대로 준다
