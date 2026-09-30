from src.recommend.trim import truncate_course

LONG_COURSE = {
    "id": "long",
    "distance_km": 10.0,
    "elevation_gain_m": 100.0,
    "tags": ["숲길"],
    # 4 points roughly 0.01 lat apart (~1.11km each) along a line, ~3.33km total
    "path": [[34.70, 127.70], [34.71, 127.70], [34.72, 127.70], [34.73, 127.70]],
}


def test_truncate_shortens_path_and_distance():
    trimmed = truncate_course(LONG_COURSE, target_km=1.0)
    assert trimmed["trimmed"] is True
    assert trimmed["original_distance_km"] == 10.0
    assert abs(trimmed["distance_km"] - 1.0) < 0.05
    assert len(trimmed["path"]) < len(LONG_COURSE["path"])
    assert trimmed["path"][0] == LONG_COURSE["path"][0]


def test_truncate_leaves_short_course_untouched():
    short = dict(LONG_COURSE, distance_km=2.0)
    result = truncate_course(short, target_km=5.0)
    assert result is short
    assert "trimmed" not in result


ROUNDTRIP = {
    "id": "rt",
    "route_type": "roundtrip",
    "distance_km": 8.0,
    "elevation_gain_m": 100.0,
    # 남북 직선으로 갔다가(3구간) 그대로 되돌아오는 왕복
    "path": [[34.70, 127.70], [34.71, 127.70], [34.72, 127.70], [34.73, 127.70],
             [34.72, 127.70], [34.71, 127.70], [34.70, 127.70]],
    "cum_outbound_m": 3330.0,
}


def _gap_from_start_m(course):
    from src.data_collection.enrich import haversine_m
    return haversine_m(course["path"][0], course["path"][-1])


def test_trimmed_roundtrip_still_ends_where_it_started():
    """왕복을 짧게 잘랐는데 먼 곳에 남겨지면 왕복이 아니다 — 반환점을 당겨야 한다."""
    trimmed = truncate_course(ROUNDTRIP, target_km=2.0)
    assert _gap_from_start_m(trimmed) < 150


def test_trimmed_roundtrip_is_close_to_requested_distance():
    trimmed = truncate_course(ROUNDTRIP, target_km=2.0)
    assert abs(trimmed["distance_km"] - 2.0) < 0.4


def test_trimmed_roundtrip_turns_around_halfway():
    """2km 왕복이면 1km 지점에서 돌아와야 한다."""
    trimmed = truncate_course(ROUNDTRIP, target_km=2.0)
    assert trimmed["cum_outbound_m"] < ROUNDTRIP["cum_outbound_m"]
    assert abs(trimmed["cum_outbound_m"] - 1000) < 200


def test_oneway_trim_is_unchanged_by_the_roundtrip_fix():
    trimmed = truncate_course(dict(LONG_COURSE, route_type="oneway"), target_km=1.0)
    assert trimmed["path"][0] == LONG_COURSE["path"][0]
    assert abs(trimmed["distance_km"] - 1.0) < 0.05


def test_truncate_drops_steps_past_the_cut():
    course_with_steps = dict(LONG_COURSE, steps=[
        {"lat": 34.70, "lng": 127.70, "description": "출발", "turn_type": 200},
        {"lat": 34.71, "lng": 127.70, "description": "1.1km 지점 좌회전", "turn_type": 12},
        {"lat": 34.73, "lng": 127.70, "description": "도착 (3.3km 지점)", "turn_type": 201},
    ])
    trimmed = truncate_course(course_with_steps, target_km=1.0)
    descriptions = [s["description"] for s in trimmed["steps"]]
    assert "출발" in descriptions
    assert "도착 (3.3km 지점)" not in descriptions
