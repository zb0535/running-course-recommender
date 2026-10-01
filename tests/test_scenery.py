"""풍경 태그는 '경로가 실제로 그 지형을 얼마나 지나는가'로 붙인다.

예전 숲길 태그는 "경로 150m 안에 녹지가 조금이라도 있으면"이라 26개 코스 중 22개에 붙었다.
골라도 걸러지는 게 없으니 선택지로서 의미가 없었다. 태그마다 경로의 몇 %가 해당 지형에
걸치는지를 재서, 기준을 넘을 때만 붙인다.
"""
import pytest

from src.data_collection.scenery import (
    FeatureIndex,
    coverage,
    count_near,
    length_on,
    resample,
    scenery_tags,
)

# 남북으로 뻗은 약 2.2km 직선 경로
PATH = [[34.70, 127.70], [34.71, 127.70], [34.72, 127.70]]


def _line(*points):
    return {"type": "way", "geometry": [{"lat": a, "lon": b} for a, b in points]}


def _square(lat, lng, half):
    return _line((lat - half, lng - half), (lat - half, lng + half), (lat + half, lng + half),
                 (lat + half, lng - half), (lat - half, lng - half))


class TestResample:
    def test_points_are_evenly_spaced(self):
        pts = resample(PATH, spacing_m=100)
        assert 20 <= len(pts) <= 24  # 2.2km / 100m

    def test_short_path_keeps_its_ends(self):
        pts = resample([[34.7, 127.7], [34.7001, 127.7]], spacing_m=100)
        assert pts[0] == [34.7, 127.7]


class TestCoverage:
    def test_line_running_alongside_the_whole_route(self):
        river = FeatureIndex([_line((34.70, 127.7003), (34.72, 127.7003))])  # 약 27m 옆
        assert coverage(PATH, river, radius_m=50) == pytest.approx(1.0, abs=0.05)

    def test_line_alongside_half_the_route(self):
        river = FeatureIndex([_line((34.70, 127.7003), (34.71, 127.7003))])
        assert coverage(PATH, river, radius_m=50) == pytest.approx(0.5, abs=0.08)

    def test_feature_far_away_gives_zero(self):
        far = FeatureIndex([_line((34.70, 127.80), (34.72, 127.80))])
        assert coverage(PATH, far, radius_m=50) == 0.0

    def test_distance_uses_the_segment_not_just_its_endpoints(self):
        """해안선처럼 꼭짓점이 드문드문한 선도, 선분 자체가 가까우면 가깝다고 봐야 한다."""
        sparse = FeatureIndex([_line((34.60, 127.7003), (34.80, 127.7003))])  # 꼭짓점은 11km 밖
        assert coverage(PATH, sparse, radius_m=50) == pytest.approx(1.0, abs=0.05)

    def test_route_inside_a_large_area_counts_even_far_from_its_edge(self):
        """큰 숲 한가운데를 지나면 경계에서 멀어도 숲을 지나는 것이다."""
        forest = FeatureIndex([_square(34.71, 127.70, 0.05)])  # 한 변 약 11km
        assert coverage(PATH, forest, radius_m=15) == pytest.approx(1.0, abs=0.05)

    def test_route_outside_an_area_does_not_count(self):
        park = FeatureIndex([_square(34.71, 127.75, 0.005)])
        assert coverage(PATH, park, radius_m=15) == 0.0


class TestCounts:
    def test_points_near_the_route_are_counted(self):
        shops = FeatureIndex([{"type": "node", "lat": 34.705, "lon": 127.7002},
                              {"type": "node", "lat": 34.715, "lon": 127.7002},
                              {"type": "node", "lat": 34.715, "lon": 127.75}])
        assert count_near(PATH, shops, radius_m=50) == 2


class TestLength:
    def test_length_of_route_on_a_feature(self):
        bridge = FeatureIndex([_line((34.705, 127.70), (34.707, 127.70))])  # 약 220m
        assert length_on(PATH, bridge, radius_m=10) == pytest.approx(220, abs=60)


class TestLongestRun:
    def test_crossing_a_bridge_is_one_long_run(self):
        from src.data_collection.scenery import longest_run_m

        bridge = FeatureIndex([_line((34.705, 127.70), (34.709, 127.70))])  # 경로를 따라 약 440m
        assert longest_run_m(PATH, bridge, radius_m=10) == pytest.approx(440, abs=70)

    def test_passing_under_bridges_is_not_a_crossing(self):
        """천변길은 다리 밑을 여러 번 지난다. 합치면 길어 보이지만 다리를 건넌 게 아니다."""
        from src.data_collection.scenery import longest_run_m

        overhead = FeatureIndex([_line((34.70 + k * 0.004, 127.699), (34.70 + k * 0.004, 127.701))
                                 for k in range(1, 5)])  # 경로와 직각으로 걸친 다리 4개
        assert length_on(PATH, overhead, radius_m=30) >= 150    # 합치면 150m를 넘지만
        assert longest_run_m(PATH, overhead, radius_m=30) < 100  # 한 번에 이어진 건 짧다


class TestTags:
    def test_riverside_path_under_bridges_is_not_tagged_as_bridge(self):
        assert "다리" not in scenery_tags({"bridge_run_m": 40}, {"distance_km": 5, "elevation_gain_m": 0})
        assert "다리" in scenery_tags({"bridge_run_m": 450}, {"distance_km": 5, "elevation_gain_m": 0})

    def test_downtown_can_be_recognized_by_signals_when_shop_data_is_missing(self):
        """광주는 OSM 상점 데이터가 비어 있어 상점 밀도만으론 도심을 놓친다."""
        course = {"distance_km": 6.45, "elevation_gain_m": 0, "traffic_signal_count": 30}
        assert "도심" in scenery_tags({"shops_per_km": 0.8}, course)

    def test_car_free_means_actually_running_on_paths_without_cars(self):
        """신호등 기록이 0이라는 이유만으로 차없는길이 되지 않는다(신호등 데이터가 누락된 지역이 있다)."""
        course = {"distance_km": 4, "elevation_gain_m": 0, "traffic_signal_count": 0}
        assert "차없는길" not in scenery_tags({"carfree": 0.1}, course)
        assert "차없는길" in scenery_tags({"carfree": 0.7}, course)

    def test_tag_needs_enough_of_the_route(self):
        """경로 끝에서 숲을 살짝 스치는 정도로는 '숲길'이 아니다."""
        assert "숲길" not in scenery_tags({"forest": 0.08}, {"distance_km": 5, "elevation_gain_m": 30})
        assert "숲길" in scenery_tags({"forest": 0.6}, {"distance_km": 5, "elevation_gain_m": 30})

    def test_mountain_trail_needs_forest_and_climb(self):
        flat_forest = scenery_tags({"forest": 0.7}, {"distance_km": 5, "elevation_gain_m": 20})
        steep_forest = scenery_tags({"forest": 0.7}, {"distance_km": 5, "elevation_gain_m": 250})
        assert "산길" not in flat_forest
        assert "산길" in steep_forest

    def test_no_measures_no_scenery_tags(self):
        assert scenery_tags({}, {"distance_km": 5, "elevation_gain_m": 0}) == []


class TestScoringByDegree:
    """같은 태그라도 '얼마나 그런 풍경인가'가 순위에 반영돼야 한다."""

    def test_more_coast_ranks_above_barely_coast(self):
        from src.recommend.score import tag_match

        user = {"environment_tags": {"바다뷰"}}
        barely = {"tags": ["바다뷰"], "scenery": {"coast": 0.41}}
        fully = {"tags": ["바다뷰"], "scenery": {"coast": 1.0}}
        assert tag_match(fully, user) > tag_match(barely, user)

    def test_course_without_the_tag_scores_zero(self):
        from src.recommend.score import tag_match

        assert tag_match({"tags": ["강변"], "scenery": {"river": 0.9}}, {"environment_tags": {"바다뷰"}}) == 0

    def test_old_course_without_measures_still_matches_by_tag(self):
        from src.recommend.score import tag_match

        assert tag_match({"tags": ["바다뷰"]}, {"environment_tags": {"바다뷰"}}) > 0


class TestUnverifiedTags:
    def test_generated_course_is_not_given_a_tag_just_because_it_was_requested(self, monkeypatch):
        """지형 데이터가 없는 곳에서 만든 코스에 '바다뷰를 요청했으니 바다뷰'라고 붙이지 않는다."""
        from src.data_collection import scenery
        from src.recommend import generate_live as g

        monkeypatch.setattr(scenery, "tags_for_path", lambda path, course=None: None)
        loop = [[34.0, 127.0], [34.004, 127.0], [34.004, 127.004], [34.0, 127.004], [34.0, 127.0]]
        monkeypatch.setattr(g, "get_route", lambda *a, **k: {"features": [
            {"geometry": {"type": "Point", "coordinates": [127.0, 34.0]}, "properties": {"totalDistance": 1700}},
            {"geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in loop]}}]})
        monkeypatch.setattr(g, "snap_vertices", lambda v: v)
        monkeypatch.setattr(g, "sample_elevation_gain", lambda path: 5.0)

        course = g.generate_loop_course(34.0, 127.0, 1.7, {"바다뷰"}, route_type="loop", angle_offset_deg=0.0)
        assert course["tags"] == []
        assert course["scenery_pending"] is True

    def test_generated_course_gets_measured_tags_where_data_exists(self, monkeypatch):
        from src.data_collection import scenery
        from src.recommend import generate_live as g

        monkeypatch.setattr(scenery, "tags_for_path", lambda path, course=None: ({"river": 0.8}, ["강변"]))
        loop = [[34.0, 127.0], [34.004, 127.0], [34.004, 127.004], [34.0, 127.004], [34.0, 127.0]]
        monkeypatch.setattr(g, "get_route", lambda *a, **k: {"features": [
            {"geometry": {"type": "Point", "coordinates": [127.0, 34.0]}, "properties": {"totalDistance": 1700}},
            {"geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in loop]}}]})
        monkeypatch.setattr(g, "snap_vertices", lambda v: v)
        monkeypatch.setattr(g, "sample_elevation_gain", lambda path: 5.0)

        course = g.generate_loop_course(34.0, 127.0, 1.7, {"바다뷰"}, route_type="loop", angle_offset_deg=0.0)
        assert course["tags"] == ["강변"]  # 요청한 바다뷰가 아니라 실제로 잰 결과
        assert "scenery_pending" not in course

    def test_unverified_course_is_not_filtered_out_by_a_tag_request(self):
        """확인할 수 없는 코스를 '태그가 없다'는 이유로 버리면, 지형 데이터가 없는 지역에선 추천이 0개가 된다."""
        from src.recommend.score import filter_by_required_tags

        pending = {"id": "p", "tags": [], "scenery_pending": True}
        verified_other = {"id": "v", "tags": ["강변"]}
        kept = filter_by_required_tags([pending, verified_other], {"environment_tags": {"바다뷰"}})
        assert [c["id"] for c in kept] == ["p"]


class TestKeywords:
    def test_every_keyword_tag_is_a_real_scenery_tag(self):
        """사전에만 있고 실제로는 붙지 않는 태그가 있으면, 그 말을 쓴 사용자는 항상 결과 0개를 받는다."""
        from src.data_collection.scenery import tag_descriptions
        from src.recommend.nl_keywords import KEYWORD_TAG_MAP

        assert set(KEYWORD_TAG_MAP) <= set(tag_descriptions())

    @pytest.mark.parametrize("sentence,expected", [
        ("강변 따라 뛰고 싶어", "강변"),
        ("호수 한 바퀴 돌 수 있는 곳", "호수"),
        ("유적지 구경하면서 달리기", "역사문화"),
        ("경치 좋은 코스", "전망"),
        ("대교 건너는 코스", "다리"),
        ("자전거도로로 쭉 달리고 싶어", "자전거길"),
    ])
    def test_sentences_map_to_tags(self, sentence, expected):
        from src.recommend.nl_keywords import extract_tags

        assert expected in extract_tags(sentence)
