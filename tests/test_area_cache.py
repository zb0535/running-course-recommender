"""출발점 주변의 길·지형을 지역당 한 번만 받아 둔다.

Overpass는 건당 5~20초에 504가 잦다. 요청마다 부르면 그게 전체 응답 시간을 좌우하고, 받은
데이터는 버려진다. 한 번 받아 저장해 두면 그 동네의 다음 요청부터는 Overpass 호출이 0건이고,
풍경 쪽으로 경유지를 잡는 것과 태그를 재는 것까지 전부 로컬에서 된다.
"""
import pytest

from src.data_collection import area_cache
from src.data_collection.scenery import FeatureIndex


def _way(wid, nodes, coords, **tags):
    return {"type": "way", "id": wid, "nodes": nodes,
            "geometry": [{"lat": a, "lon": b} for a, b in coords], "tags": tags}


class TestNearestPoint:
    def test_snaps_to_the_closest_point_on_a_segment(self):
        roads = FeatureIndex([_way(1, [1, 2], [(34.0, 127.0), (34.0, 127.002)], highway="residential")])
        snapped = roads.nearest_point(34.0005, 127.001, radius_m=200)
        assert snapped[0] == pytest.approx(34.0, abs=1e-6)
        assert snapped[1] == pytest.approx(127.001, abs=1e-6)

    def test_nothing_within_radius_returns_none(self):
        roads = FeatureIndex([_way(1, [1, 2], [(34.0, 127.0), (34.0, 127.002)], highway="residential")])
        assert roads.nearest_point(34.01, 127.001, radius_m=200) is None


class TestDeadEnds:
    def test_dead_end_street_is_not_a_snap_target(self):
        """막다른 길에 경유지를 찍으면 Tmap이 끝까지 갔다가 되돌아 나온다. 처음부터 후보에서 뺀다."""
        through = _way(1, [1, 2, 3], [(34.0, 127.0), (34.0, 127.001), (34.0, 127.002)], highway="residential")
        other = _way(2, [3, 4, 1], [(34.0, 127.002), (34.001, 127.001), (34.0, 127.0)], highway="residential")
        dead_end = _way(3, [2, 9], [(34.0, 127.001), (33.999, 127.001)], highway="residential")
        kept = [w["id"] for w in area_cache.through_ways([through, other, dead_end])]
        assert kept == [1, 2]

    def test_parking_and_driveways_are_not_snap_targets(self):
        loop_a = _way(1, [1, 2, 3], [(34.0, 127.0), (34.0, 127.001), (34.0, 127.002)], highway="residential")
        loop_b = _way(2, [3, 4, 1], [(34.0, 127.002), (34.001, 127.001), (34.0, 127.0)], highway="service")
        kept = [w["id"] for w in area_cache.through_ways([loop_a, loop_b])]
        assert kept == [1]


class TestCaching:
    def test_same_neighbourhood_is_fetched_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(area_cache, "_fetch", lambda bbox: calls.append(bbox) or [])
        area_cache.load_area(34.7393, 127.7359, radius_m=900)
        area_cache.load_area(34.7401, 127.7352, radius_m=900)  # 100m 옆
        assert len(calls) == 1

    def test_cache_survives_a_restart(self, monkeypatch):
        calls = []
        monkeypatch.setattr(area_cache, "_fetch", lambda bbox: calls.append(bbox) or [])
        area_cache.load_area(34.7393, 127.7359, radius_m=900)
        monkeypatch.setattr(area_cache, "_memory", {})  # 서버 재시작
        area_cache.load_area(34.7393, 127.7359, radius_m=900)
        assert len(calls) == 1

    def test_area_covers_the_whole_loop_radius(self, monkeypatch):
        monkeypatch.setattr(area_cache, "_fetch", lambda bbox: [])
        for radius in (900, 1700, 3500):
            area = area_cache.load_area(34.7393, 127.7359, radius_m=radius)
            assert area.covers(34.7393, 127.7359, radius), radius

    def test_fetch_failure_gives_none_and_is_not_cached(self, monkeypatch):
        def fail(bbox):
            raise area_cache.OverpassUnavailable("504")

        monkeypatch.setattr(area_cache, "_fetch", fail)
        assert area_cache.load_area(34.7393, 127.7359, radius_m=900) is None
        calls = []
        monkeypatch.setattr(area_cache, "_fetch", lambda bbox: calls.append(1) or [])
        assert area_cache.load_area(34.7393, 127.7359, radius_m=900) is not None
        assert calls, "실패를 캐시해서 다시 시도하지 않았다"

    def test_lookup_without_fetching(self, monkeypatch):
        """코스를 등재하면서 태그를 잴 때는 Overpass를 새로 부르지 않고, 받아 둔 게 있을 때만 쓴다."""
        monkeypatch.setattr(area_cache, "_fetch", lambda bbox: pytest.fail("받아 둔 데이터만 써야 한다"))
        assert area_cache.load_area(34.7393, 127.7359, radius_m=900, allow_fetch=False) is None
