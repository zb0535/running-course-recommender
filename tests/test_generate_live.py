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
        return {
            "id": f"candidate-{kwargs['angle_offset_deg']}",
            "distance_km": distance,
            "radius_scale": kwargs["radius_scale"],
        }

    monkeypatch.setattr(generate_live, "generate_loop_course", make_course)

    courses = generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())

    assert [course["id"] for course in courses] == ["candidate-0.0", "candidate-20.0", "candidate--20.0"]
    assert [call["snap_to_osm"] for call in calls] == [True, False, False]
    assert [call["radius_scale"] for call in calls] == [0.85, 0.70, 0.55]


def test_loop_candidates_refine_each_route_toward_requested_distance(monkeypatch):
    calls = []

    def make_course(lat, lng, target, tags, **kwargs):
        scale = kwargs["radius_scale"]
        calls.append(kwargs)
        return {
            "id": f"candidate-{kwargs['angle_offset_deg']}-r{scale:.3f}",
            "distance_km": round(target * scale, 2),
            "radius_scale": scale,
        }

    monkeypatch.setattr(generate_live, "generate_loop_course", make_course)

    candidates = generate_live.generate_loop_candidates(34.0, 127.0, 5.0, set())

    assert len(candidates) == 3
    assert all(abs(candidate["distance_km"] - 5.0) <= 0.25 for candidate in candidates)
    assert len(calls) == 6
    assert [call["snap_to_osm"] for call in calls] == [True, False, False, False, False, False]


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


def test_route_overlap_ratio_distinguishes_retraced_and_separate_loop_legs():
    retraced = [[0.0, 0.0], [0.0, 0.002], [0.0, 0.004], [0.0, 0.002], [0.0, 0.0]]
    separate = [[0.0, 0.0], [0.0, 0.002], [0.002, 0.002], [0.002, 0.0], [0.0, 0.0]]

    assert generate_live.route_overlap_ratio(retraced) > 0.9
    assert generate_live.route_overlap_ratio(separate) < 0.2


def test_route_overlap_ratio_counts_a_retraced_detour_in_an_asymmetric_loop():
    partial_retrace = [
        [0.0, 0.0], [0.0, 0.002], [0.002, 0.002],
        [0.0, 0.002], [-0.002, 0.002], [0.0, 0.0],
    ]

    overlap = generate_live.route_overlap_ratio(partial_retrace)

    assert 0.2 < overlap < 0.5