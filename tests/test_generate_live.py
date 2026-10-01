import pytest

from src.recommend import generate_live


def test_roundtrip_uses_tmap_waypoints_to_close_loop(monkeypatch):
    calls = []

    monkeypatch.setattr(
        generate_live,
        "find_nearby_points",
        lambda lat, lng, radius_m, osm_filter: [(34.001, 127.001), (34.002, 127.002)],
    )
    monkeypatch.setattr(generate_live, "snap_vertices", lambda vertices: vertices)
    monkeypatch.setattr(generate_live, "sample_elevation_gain", lambda path: 10.0)

    def get_route(start, end, start_name="", end_name="", waypoints=None):
        calls.append((start, end, waypoints))
        return {
            "features": [
                {"geometry": {"type": "Point", "coordinates": [127.0, 34.0]},
                 "properties": {"totalDistance": 3000}},
                {"geometry": {"type": "LineString", "coordinates": [
                    [127.0, 34.0], [127.001, 34.001], [127.002, 34.002], [127.0, 34.0],
                ]}},
            ]
        }

    monkeypatch.setattr(generate_live, "get_route", get_route)

    course = generate_live.generate_loop_course(34.0, 127.0, 3.0, {"숲길"})

    assert len(calls) == 1
    assert calls[0][0] == calls[0][1] == (127.0, 34.0)
    assert len(calls[0][2]) == 3
    assert course["path"][0] == course["path"][-1] == [34.0, 127.0]
    assert course["distance_km"] == 3.0


def test_virtual_vertices_use_target_circumference():
    vertices = generate_live.virtual_vertices(34.0, 127.0, 5.0)
    assert len(vertices) == 3
    assert all(abs(point[0] - 34.0) > 0 or abs(point[1] - 127.0) > 0 for point in vertices)


def test_self_intersection_detects_bow_tie_path():
    assert generate_live.has_self_intersection([
        [0.0, 0.0], [1.0, 1.0], [0.0, 1.0], [1.0, 0.0], [0.0, 0.0]
    ])


def test_self_intersection_allows_simple_closed_path():
    assert not generate_live.has_self_intersection([
        [0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]
    ])


def test_snap_vertices_falls_back_to_geometry_when_overpass_times_out(monkeypatch, capsys):
    calls = []

    def fail_request(query, timeout):
        calls.append(query)
        raise generate_live.OverpassUnavailable("Overpass timed out")

    monkeypatch.setattr(generate_live, "post_overpass_query", fail_request)
    vertices = [(34.001, 127.001), (34.002, 127.002)]

    assert generate_live.snap_vertices(vertices) == vertices
    assert len(calls) == 1
    assert capsys.readouterr().out == ""


def test_loop_candidate_generator_uses_multiple_orientations_and_snaps_once(monkeypatch):
    calls = []

    def make_course(lat, lng, distance, tags, **kwargs):
        calls.append(kwargs)
        return {"id": f"candidate-{kwargs['angle_offset_deg']}", "distance_km": 5.0}

    monkeypatch.setattr(generate_live, "generate_loop_course", make_course)

    courses = generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())

    assert [course["id"] for course in courses] == ["candidate-0.0", "candidate-20.0", "candidate--20.0"]
    # 후보 생성은 병렬 실행되므로 mock 호출 도착 순서에는 의존하지 않는다.
    by_angle = {call["angle_offset_deg"]: call for call in calls}
    assert [by_angle[angle]["snap_to_osm"] for angle in generate_live.RETRY_ANGLE_OFFSETS_DEG] == [True, False, False]
    assert [by_angle[angle]["radius_scale"] for angle in generate_live.RETRY_ANGLE_OFFSETS_DEG] == list(generate_live.LOOP_RADIUS_SCALES)


def test_short_loops_trigger_one_larger_attempt(monkeypatch):
    """가지를 잘라내면 순환이 짧아진다. 후보가 전부 목표보다 한참 짧으면 반경을 키워 한 번 더 만든다."""
    calls = []

    def make_course(lat, lng, distance, tags, **kwargs):
        calls.append(kwargs["radius_scale"])
        # 반경에 비례해 거리가 나온다고 가정 (반경 1.0이면 목표의 60%)
        return {"id": f"c{len(calls)}", "distance_km": round(distance * 0.6 * kwargs["radius_scale"], 2)}

    monkeypatch.setattr(generate_live, "generate_loop_course", make_course)
    courses = generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())

    assert len(calls) == 4, "짧은 후보뿐인데 보정 시도를 안 했다"
    assert calls[-1] > max(calls[:3])
    best = min(courses, key=lambda c: abs(c["distance_km"] - 5.0))
    assert abs(best["distance_km"] - 5.0) / 5.0 <= 0.2


def test_on_target_loops_are_not_regenerated(monkeypatch):
    calls = []
    monkeypatch.setattr(generate_live, "generate_loop_course",
                        lambda lat, lng, d, tags, **k: calls.append(1) or {"id": f"c{len(calls)}", "distance_km": 5.1})
    generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())
    assert len(calls) == 3


def _road(lat1, lng1, lat2, lng2):
    return {"geometry": [{"lat": lat1, "lon": lng1}, {"lat": lat2, "lon": lng2}]}


def test_vertices_avoid_directions_with_no_road():
    """해안가에서는 원의 절반이 바다다. 길이 없는 방향에 꼭짓점을 찍으면 코스가 쪼그라든다."""
    # 출발점 북쪽 반원에만 길이 있다 (남쪽은 바다)
    center = (34.0, 127.0)
    ways = [_road(34.0 + 0.002 + i * 0.001, 126.99, 34.0 + 0.002 + i * 0.001, 127.01) for i in range(8)]
    ways += [_road(34.0, 126.99, 34.012, 126.99), _road(34.0, 127.01, 34.012, 127.01)]
    vertices = generate_live.pick_land_vertices(*center, 5.0, ways)
    assert vertices, "길이 있는 방향이 있는데 꼭짓점을 못 골랐다"
    assert all(v[0] >= 34.0 - 1e-9 for v in vertices), "바다 쪽(남쪽)에 꼭짓점을 찍었다"


def test_vertices_are_spread_around_when_roads_are_everywhere():
    center = (34.0, 127.0)
    ways = [_road(34.0 + d, 126.98, 34.0 + d, 127.02) for d in (-0.008, -0.004, 0.0, 0.004, 0.008)]
    ways += [_road(33.98, 127.0 + d, 34.02, 127.0 + d) for d in (-0.008, -0.004, 0.0, 0.004, 0.008)]
    vertices = generate_live.pick_land_vertices(*center, 5.0, ways)
    assert len(vertices) == generate_live.VERTEX_COUNT
    import math
    angles = sorted(math.degrees(math.atan2(v[0] - center[0], v[1] - center[1])) % 360 for v in vertices)
    gaps = [(angles[(i + 1) % len(angles)] - angles[i]) % 360 for i in range(len(angles))]
    assert min(gaps) >= 60, f"꼭짓점이 한쪽에 몰렸다: {angles}"


def test_road_network_is_fetched_once_for_all_candidates(monkeypatch):
    """후보마다 Overpass에 물으면 느리고 속도 제한에 걸린다 — 한 번 받아 모두 쓴다."""
    queries = []

    class Response:
        def json(self):
            return {"elements": []}

    monkeypatch.setattr(generate_live, "post_overpass_query", lambda q, timeout=30: queries.append(q) or Response())
    monkeypatch.setattr(generate_live, "generate_loop_course",
                        lambda lat, lng, d, tags, **k: {"id": f"c{k['angle_offset_deg']}", "distance_km": 5.0})
    generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())
    assert len(queries) == 1


def test_one_candidate_rejected_by_tmap_does_not_fail_the_request(monkeypatch):
    """후보 하나가 Tmap에서 400을 받아도(경유지가 갈 수 없는 곳 등) 나머지로 답해야 한다."""
    import requests

    def make_course(lat, lng, d, tags, **kwargs):
        if kwargs["angle_offset_deg"] == 0.0:
            raise requests.HTTPError("400 Client Error: Bad Request")
        return {"id": f"c{kwargs['angle_offset_deg']}", "distance_km": 5.0}

    monkeypatch.setattr(generate_live, "generate_loop_course", make_course)
    courses = generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())
    assert [c["id"] for c in courses] == ["c20.0", "c-20.0"]


def test_loop_is_not_routed_through_the_sea_when_roads_are_known(monkeypatch):
    """주변 길을 받았는데 길 있는 방향이 부족하면, 바다일 수 있는 계산 좌표로 억지로 요청하지 않는다."""
    monkeypatch.setattr(generate_live, "get_route", lambda *a, **k: pytest.fail("길 없는 좌표로 Tmap을 불렀다"))
    far_road = [{"geometry": [{"lat": 40.0, "lon": 130.0}, {"lat": 40.001, "lon": 130.0}]}]
    with pytest.raises(ValueError):
        generate_live.generate_loop_course(34.0, 127.0, 5.0, set(), route_type="loop",
                                           angle_offset_deg=0.0, ways=far_road)


def _square_loop(lat, lng, size, n=8):
    d = size
    pts = [[lat, lng + d * i / n] for i in range(n)]
    pts += [[lat + d * i / n, lng + d] for i in range(n)]
    pts += [[lat + d, lng + d - d * i / n] for i in range(n)]
    pts += [[lat + d - d * i / n, lng] for i in range(n)]
    return pts + [[lat, lng]]


def test_useless_candidates_are_dropped(monkeypatch):
    """막다른 길 왕복을 걷어내고 나면 거의 안 남는 후보(0.04km 등)는 코스가 아니다."""
    loops = {
        0.0: {"id": "good", "distance_km": 4.9, "path": _square_loop(34.0, 127.0, 0.011)},
        20.0: {"id": "tiny", "distance_km": 0.04, "path": _square_loop(34.0, 127.0, 0.0001)},
        -20.0: {"id": "ok", "distance_km": 4.2, "path": _square_loop(34.0, 127.0, 0.0095)[::-1]},
    }
    monkeypatch.setattr(generate_live, "generate_loop_course",
                        lambda lat, lng, d, tags, **k: loops[k["angle_offset_deg"]])
    ids = [c["id"] for c in generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())]
    assert "tiny" not in ids
    assert "good" in ids


def test_all_useless_candidates_is_a_failure_not_an_empty_answer(monkeypatch):
    monkeypatch.setattr(generate_live, "generate_loop_course",
                        lambda lat, lng, d, tags, **k: {"id": "tiny", "distance_km": 0.05,
                                                        "path": _square_loop(34.0, 127.0, 0.0001)})
    with pytest.raises(ValueError):
        generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())


def test_near_duplicate_candidates_are_collapsed(monkeypatch):
    """사실상 같은 길이면 하나만 남긴다 — 목표 거리에 더 가까운 쪽을."""
    same = _square_loop(34.0, 127.0, 0.011)
    loops = {
        0.0: {"id": "a", "distance_km": 4.6, "path": same},
        20.0: {"id": "b", "distance_km": 4.9, "path": same},
        -20.0: {"id": "c", "distance_km": 5.1, "path": _square_loop(34.02, 127.02, 0.011)},
    }
    monkeypatch.setattr(generate_live, "generate_loop_course",
                        lambda lat, lng, d, tags, **k: loops[k["angle_offset_deg"]])
    ids = [c["id"] for c in generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())]
    assert "a" not in ids and "b" in ids and "c" in ids


def test_smaller_loop_radius_scales_waypoints_inward():
    full_radius = generate_live.virtual_vertices(34.0, 127.0, 5.0, radius_scale=1.0)
    reduced_radius = generate_live.virtual_vertices(34.0, 127.0, 5.0, radius_scale=0.7)

    full_delta = abs(full_radius[0][1] - 127.0)
    reduced_delta = abs(reduced_radius[0][1] - 127.0)
    assert reduced_delta == pytest.approx(full_delta * 0.7)


def test_snap_projects_vertex_to_middle_of_nearby_road_segment(monkeypatch):
    queries = []

    class Response:
        def json(self):
            return {"elements": [{"geometry": [
                {"lat": 34.0, "lon": 127.0},
                {"lat": 34.0, "lon": 127.002},
            ]}]}

    def post_query(query, timeout):
        queries.append(query)
        return Response()

    monkeypatch.setattr(generate_live, "post_overpass_query", post_query)

    snapped = generate_live.snap_to_runnable_point(34.0005, 127.001)

    assert snapped[0] == pytest.approx(34.0, abs=1e-7)
    assert snapped[1] == pytest.approx(127.001, abs=1e-7)
    assert f"around:{generate_live.SNAP_RADIUS_M}" in queries[0]
    assert '["highway"~' in queries[0]
    assert '["leisure"="park"]' not in queries[0]


def test_snap_rejects_nearest_road_outside_200_meter_radius(monkeypatch):
    class Response:
        def json(self):
            return {"elements": [{"geometry": [
                {"lat": 34.003, "lon": 127.0},
                {"lat": 34.003, "lon": 127.002},
            ]}]}

    monkeypatch.setattr(generate_live, "post_overpass_query", lambda query, timeout: Response())

    assert generate_live.snap_to_runnable_point(34.0, 127.001) is None