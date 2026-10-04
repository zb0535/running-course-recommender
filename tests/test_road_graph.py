"""도로망에서 직접 경로를 짠다 — 최단 거리가 아니라 달리기 좋은 길로."""
import pytest

from src.api_clients.tmap_pedestrian import extract_path, extract_steps
from src.data_collection.scenery import build_indexes
from src.recommend import road_graph as rg

LAT, LNG = 35.0, 127.0
D = 0.001  # 위도 약 111m, 경도 약 91m


def _way(wid, coords, **tags):
    return {"type": "way", "id": wid, "geometry": [{"lat": a, "lon": b} for a, b in coords], "tags": tags}


def _graph(ways, model=None):
    return rg.RoadGraph(ways, build_indexes({"all": ways}), model or rg.PRIOR)


@pytest.fixture
def town():
    """A에서 B로 가는 두 길: 곧장 가는 큰길(약 364m)과, 강을 따라 돌아가는 산책로(약 586m)."""
    a, b = (LAT, LNG), (LAT, LNG + 4 * D)
    return [
        _way(1, [a, (LAT, LNG + 2 * D), b], highway="primary", name="큰길"),
        _way(2, [a, (LAT + D, LNG)], highway="residential", name="샛길"),
        _way(3, [(LAT + D, LNG), (LAT + D, LNG + 2 * D), (LAT + D, LNG + 4 * D)], highway="footway", name="강변길"),
        _way(4, [(LAT + D, LNG + 4 * D), b], highway="residential", name="샛길"),
        _way(9, [(LAT + D + 0.0003, LNG - D), (LAT + D + 0.0003, LNG + 5 * D)], waterway="river"),
    ], a, b


def test_riverside_path_scores_higher_than_a_main_road(town):
    ways, _, _ = town
    graph = _graph(ways)
    assert graph.way_score(2) > graph.way_score(0) + 0.3   # 강변 산책로 > 큰길


def test_route_prefers_the_pleasant_path_over_the_shortest(town):
    ways, a, b = town
    result = rg.route(_graph(ways), a, b)
    path, distance = extract_path(result)
    assert [LAT + D, LNG + 2 * D] in [[round(p[0], 6), round(p[1], 6)] for p in path], "강변길로 가지 않았다"
    assert distance == pytest.approx(586, rel=0.05)


def test_detour_is_bounded(town):
    """좋은 길이라도 한없이 돌아가지는 않는다 — 큰길이 훨씬 짧으면 큰길로 간다."""
    ways, a, b = town
    ways[2] = _way(3, [(LAT + D, LNG), (LAT + 12 * D, LNG + 2 * D), (LAT + D, LNG + 4 * D)],
                   highway="footway", name="먼길")
    path, _ = extract_path(rg.route(_graph(ways), a, b))
    assert all(p[0] <= LAT + D for p in path)


def test_loop_does_not_come_back_the_way_it_went(town):
    ways, a, b = town
    result = rg.route(_graph(ways), a, a, waypoints=[b])
    path, distance = extract_path(result)
    assert path[0] == path[-1]
    assert distance == pytest.approx(586 + 364, rel=0.05)   # 강변길로 가서 큰길로 온다


def test_turn_by_turn_steps_are_generated(town):
    ways, a, b = town
    steps = extract_steps(rg.route(_graph(ways), a, b))
    assert [s["turn_type"] for s in steps] == [200, 13, 13, 201]
    assert steps[0]["description"] == "샛길을 따라 111m 이동"
    assert steps[1]["description"].startswith("우회전 후 강변길을 따라 ")
    assert steps[-1]["description"] == "도착"


def test_private_roads_are_not_used(town):
    ways, a, b = town
    ways[2]["tags"]["access"] = "private"
    path, _ = extract_path(rg.route(_graph(ways), a, b))
    assert all(p[0] == LAT for p in path)


def test_unreachable_destination_gives_none(town):
    ways, a, _ = town
    ways.append(_way(20, [(LAT + 0.05, LNG), (LAT + 0.05, LNG + D)], highway="residential"))
    graph = _graph(ways)
    assert rg.route(graph, a, (LAT + 0.05, LNG)) is None
    assert rg.route(graph, a, (LAT + 0.3, LNG)) is None   # 근처에 길이 없다


def test_learned_model_changes_the_scores(town):
    ways, _, _ = town
    learned = {"bias": 0.0, "weights": {"hw_primary": 3.0, "hw_footway": -3.0}}
    graph = _graph(ways, learned)
    assert graph.way_score(0) > graph.way_score(2)


class TestRunnersUseSidewalksNotCarLanes:
    def test_surface_kinds(self):
        assert rg.surface({"highway": "footway"}) == "walkway"
        assert rg.surface({"highway": "cycleway"}) == "walkway"
        assert rg.surface({"highway": "primary", "sidewalk": "both"}) == "sidewalk"
        assert rg.surface({"highway": "residential"}) == "shared"
        assert rg.surface({"highway": "secondary"}) == "carroad"          # 인도가 있는지 모른다
        assert rg.surface({"highway": "secondary", "sidewalk": "no"}) is None, "인도 없는 큰길로 보냈다"
        assert rg.surface({"highway": "motorway"}) is None
        assert rg.surface({"highway": "footway", "foot": "no"}) is None

    def test_stairs_are_avoided_when_a_ramp_is_not_much_longer(self):
        """계단은 달리는 흐름이 끊긴다. 조금 돌아가는 길이 있으면 그쪽으로 간다."""
        a, b = (LAT, LNG), (LAT, LNG + 2 * D)
        ways = [
            _way(1, [a, b], highway="steps"),
            _way(2, [a, (LAT + D, LNG + D), b], highway="footway"),   # 약 1.6배 길다
        ]
        result = rg.route(_graph(ways), a, b)
        assert [LAT + D, LNG + D] in [[round(p[0], 6), round(p[1], 6)] for p in extract_path(result)[0]]

    def test_parallel_walkway_is_taken_instead_of_the_road(self):
        """큰길 옆에 따로 그려진 인도가 있으면 그쪽으로 간다 (같은 길이라도)."""
        a, b = (LAT, LNG), (LAT, LNG + 4 * D)
        side = 0.0001  # 약 11m 옆
        ways = [
            _way(1, [a, b], highway="secondary", name="대로"),
            _way(2, [a, (LAT + side, LNG), (LAT + side, LNG + 4 * D), b], highway="footway"),
        ]
        result = rg.route(_graph(ways), a, b)
        assert result["road_mix"]["walkway"] == 1.0
        assert result["road_mix"]["carroad"] == 0.0

    def test_road_with_no_sidewalk_is_never_used(self):
        a, b = (LAT, LNG), (LAT, LNG + 4 * D)
        ways = [_way(1, [a, b], highway="primary", sidewalk="no")]
        assert rg.route(_graph(ways), a, b) is None

    def test_result_reports_what_kind_of_road_the_course_runs_on(self, town):
        ways, a, b = town
        mix = rg.route(_graph(ways), a, b)["road_mix"]
        assert mix["walkway"] == pytest.approx(364 / 586, abs=0.03)   # 강변길
        assert mix["shared"] == pytest.approx(222 / 586, abs=0.03)    # 샛길 두 구간
        assert sum(mix.values()) == pytest.approx(1.0, abs=0.01)


class TestLearning:
    def test_learns_which_kind_of_road_people_run_on(self):
        """정답 길이 주로 강가 자전거길이면, 그 특징의 가중치가 오른다."""
        from src.recommend import learn_roads

        samples = ([({"hw_cycleway": 1.0, "near_river": 1.0}, 1)] * 40 + [({"hw_cycleway": 1.0}, 1)] * 10
                   + [({"hw_service": 1.0}, 0)] * 120 + [({"hw_residential": 1.0}, 0)] * 70
                   + [({"hw_residential": 1.0, "near_river": 1.0}, 1)] * 5 + [({"hw_residential": 1.0}, 1)] * 5)
        model = learn_roads.train(samples)
        weights = model["weights"]
        assert weights["hw_cycleway"] > 1 and weights["near_river"] > 0.5
        assert weights["hw_service"] < -1
        assert learn_roads.auc(samples, model) > 0.9

    def test_auc_of_a_useless_model_is_a_coin_flip(self):
        from src.recommend import learn_roads

        samples = [({"hw_footway": 1.0}, 1)] * 10 + [({"hw_footway": 1.0}, 0)] * 30
        assert learn_roads.auc(samples, {"bias": 0.0, "weights": {}}) == pytest.approx(0.5)


def test_bicycle_routes_on_car_roads_are_not_examples_of_good_running_roads():
    from src.recommend.learn_roads import label

    assert label({"highway": "cycleway", "route": "bicycle"}) == 1
    assert label({"highway": "primary", "route": "bicycle"}) is None
    assert label({"highway": "residential", "route": "running"}) == 1
    assert label({"highway": "path", "route": "hiking"}) is None
    assert label({"highway": "service"}) == 0


class TestNight:
    """밤에는 인적 드문 산책로 대신 상가·큰길 쪽으로 간다."""

    def _town(self):
        a, b = (LAT, LNG), (LAT, LNG + 4 * D)
        shops = [{"type": "node", "id": 100 + i, "lat": LAT - D, "lon": LNG + i * D, "tags": {"shop": "convenience"}}
                 for i in range(5)]
        return [
            # 강을 따라가는 산책로 (낮에 좋은 길)
            _way(1, [a, (LAT + D, LNG), (LAT + D, LNG + 4 * D), b], highway="footway", name="강변길"),
            _way(9, [(LAT + D + 0.0003, LNG - D), (LAT + D + 0.0003, LNG + 5 * D)], waterway="river"),
            # 상점이 늘어선 인도 있는 길
            _way(2, [a, (LAT - D, LNG), (LAT - D, LNG + 4 * D), b], highway="tertiary", sidewalk="both", name="상가길"),
        ] + shops, a, b

    def test_way_kinds_at_night(self):
        ways, _, _ = self._town()
        graph = _graph(ways)
        assert graph.night_kind(0) == "secluded"
        assert graph.night_kind(1) == "lively"

    def test_by_day_the_riverside_path_is_chosen(self):
        ways, a, b = self._town()
        path, _ = extract_path(rg.route(_graph(ways), a, b))
        assert any(p[0] > LAT for p in path), "낮에는 강변길로 간다"

    def test_at_night_the_lively_street_is_chosen(self):
        ways, a, b = self._town()
        result = rg.route(_graph(ways), a, b, night=True)
        path, _ = extract_path(result)
        assert all(p[0] <= LAT for p in path), "밤인데 인적 드문 강변길로 갔다"
        assert result["lively_ratio"] == 1.0 and result["secluded_ratio"] == 0.0

    def test_secluded_share_is_reported_for_a_daytime_route(self):
        ways, a, b = self._town()
        assert rg.route(_graph(ways), a, b)["secluded_ratio"] == 1.0
