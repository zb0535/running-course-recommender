from src.recommend import route_type
from src.recommend.route_type import apply_route_type, attach_actual_return_path, make_roundtrip, mark_oneway

COURSE = {
    "id": "c1",
    "distance_km": 3.0,
    "elevation_gain_m": 30.0,
    "path": [[34.70, 127.70], [34.71, 127.70], [34.72, 127.70]],
    "steps": [{"lat": 34.70, "lng": 127.70, "description": "출발", "turn_type": 200}],
}


def test_make_roundtrip_doubles_distance_and_mirrors_path():
    rt = make_roundtrip(COURSE)
    assert rt["distance_km"] == 6.0
    assert rt["elevation_gain_m"] == 60.0
    assert rt["route_type"] == "roundtrip"
    assert len(rt["path"]) == 2 * len(COURSE["path"]) - 1
    assert rt["path"][0] == COURSE["path"][0]
    assert rt["path"][-1] == COURSE["path"][0]  # 왕복이니 출발점으로 되돌아옴


def test_make_roundtrip_adds_turnaround_step():
    rt = make_roundtrip(COURSE)
    assert len(rt["steps"]) == len(COURSE["steps"]) + 1
    assert "반환점" in rt["steps"][-1]["description"]


RETURN_LEG = {
    "return_path": [[34.72, 127.70], [34.71, 127.7005], [34.70, 127.70]],
    "return_steps": [
        {"lat": 34.71, "lng": 127.7005, "description": "좌회전 후 120m 이동", "turn_type": 12},
        {"lat": 34.70, "lng": 127.70, "description": "도착", "turn_type": 201},
    ],
}


def test_roundtrip_uses_real_return_guidance_when_available():
    """돌아오는 길도 실제 Tmap 안내로 채워져야 한다. 반환점 한 줄로 끝나면 안 된다."""
    rt = make_roundtrip(dict(COURSE, **RETURN_LEG))
    descriptions = [s["description"] for s in rt["steps"]]
    assert "좌회전 후 120m 이동" in descriptions
    assert "도착" in descriptions
    assert "반환점" not in " ".join(descriptions)


def test_roundtrip_guidance_is_ordered_along_the_route():
    rt = make_roundtrip(dict(COURSE, **RETURN_LEG))
    positions = [s["cum_m"] for s in rt["steps"]]
    assert positions == sorted(positions)


def test_return_guidance_comes_after_the_outbound_half():
    rt = make_roundtrip(dict(COURSE, **RETURN_LEG))
    outbound_end = rt["cum_outbound_m"]
    arrival = next(s for s in rt["steps"] if s["description"] == "도착")
    assert arrival["cum_m"] > outbound_end


def test_roundtrip_without_return_leg_still_marks_turnaround():
    """복귀 경로가 아직 없는 코스는 없는 안내를 지어내지 않고 반환점만 표시한다."""
    rt = make_roundtrip(COURSE)
    assert "반환점" in rt["steps"][-1]["description"]


def test_mark_oneway_leaves_path_untouched():
    ow = mark_oneway(COURSE)
    assert ow["path"] == COURSE["path"]
    assert ow["distance_km"] == COURSE["distance_km"]
    assert ow["route_type"] == "oneway"


def test_apply_route_type_dispatches():
    assert apply_route_type(COURSE, "roundtrip")["distance_km"] == 6.0
    assert apply_route_type(COURSE, "oneway")["distance_km"] == 3.0


def test_attach_actual_return_path_uses_tmap_route(monkeypatch):
    monkeypatch.setattr(
        route_type,
        "get_route",
        lambda start, end, **kwargs: {"features": [
            {"geometry": {"type": "LineString", "coordinates": [
                [start[0], start[1]], [127.7005, 34.71], [end[0], end[1]],
            ]}}
        ]},
    )

    result = attach_actual_return_path(COURSE)

    assert result["return_path"] == [[34.72, 127.7], [34.71, 127.7005], [34.7, 127.7]]
