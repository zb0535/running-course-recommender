"""순환 코스에서 '들어갔다가 그대로 되나오는 가지'를 잘라낸다.

경유지가 막다른 길에 찍히면 Tmap은 그 끝까지 갔다가 같은 길로 돌아 나온다. 달리는 사람에겐
의미 없는 왕복 구간이다(실측: 4.84km 순환 중 0.74km가 이런 구간이었다).
반면 블록을 한 바퀴 도는 작은 고리는 다른 길로 돌아오는 정상 경로라 남겨야 한다.
"""
from src.data_collection.enrich import haversine_m
from src.recommend.generate_live import prune_spurs, retraced_ratio
from src.recommend.step_position import cumulative_distances

D = 0.001  # 약 110m


def _square(lat=34.0, lng=127.0, n=10):
    """한 변을 n개 점으로 나눈 정사각형 고리 (시계방향, 출발=도착)."""
    pts = []
    for i in range(n):
        pts.append([lat, lng + D * i / n * 4])
    for i in range(n):
        pts.append([lat + D * i / n * 4, lng + D * 4])
    for i in range(n):
        pts.append([lat + D * 4, lng + D * 4 - D * i / n * 4])
    for i in range(n):
        pts.append([lat + D * 4 - D * i / n * 4, lng])
    pts.append([lat, lng])
    return pts


def _with_spur(loop, at_index, length_points=8, step=D * 0.6):
    """loop의 at_index 지점에서 바깥으로 뻗었다가 같은 길로 돌아오는 가지를 끼워 넣는다."""
    base = loop[at_index]
    out = [[base[0] - step * k, base[1]] for k in range(1, length_points + 1)]
    back = list(reversed(out[:-1])) + [base]
    return loop[: at_index + 1] + out + back + loop[at_index + 1:]


def _length(path):
    return cumulative_distances(path)[-1]


def test_clean_loop_is_left_alone():
    loop = _square()
    assert prune_spurs(loop) == loop
    assert retraced_ratio(loop) < 0.02


def test_dead_end_spur_is_removed():
    loop = _square()
    spurred = _with_spur(loop, at_index=5)
    assert retraced_ratio(spurred) > 0.1

    pruned = prune_spurs(spurred)
    assert _length(pruned) < _length(spurred)
    assert abs(_length(pruned) - _length(loop)) < 30
    assert retraced_ratio(pruned) < 0.02


def test_pruned_loop_still_returns_to_start():
    pruned = prune_spurs(_with_spur(_square(), at_index=15))
    assert haversine_m(pruned[0], pruned[-1]) < 1


def test_two_spurs_are_both_removed():
    spurred = _with_spur(_with_spur(_square(), at_index=5), at_index=30)
    pruned = prune_spurs(spurred)
    assert retraced_ratio(pruned) < 0.02


def _route(path, steps=()):
    feats = [{"geometry": {"type": "Point", "coordinates": [path[0][1], path[0][0]]},
              "properties": {"totalDistance": int(_length(path))}},
             {"geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in path]}}]
    for lat, lng, desc in steps:
        feats.append({"geometry": {"type": "Point", "coordinates": [lng, lat]},
                      "properties": {"description": desc, "turnType": 11}})
    return {"features": feats}


def test_generated_loop_is_rerouted_without_the_spur(monkeypatch):
    """가지를 잘라내면 갈림길 안내('막다른 길로 좌회전')가 틀려진다. 안내를 지어내지 않고,
    잘라낸 경로 위 지점을 경유지로 다시 요청해 실제 안내를 받는다."""
    from src.recommend import generate_live as g

    loop = _square()
    spurred = _with_spur(loop, at_index=5)
    calls = []

    def fake_route(start, end, start_name="", end_name="", waypoints=None):
        calls.append(waypoints)
        if len(calls) == 1:
            return _route(spurred, [(spurred[6][0], spurred[6][1], "좌회전 후 막다른 길 따라 이동")])
        return _route(loop, [(loop[20][0], loop[20][1], "직진 후 이동")])

    monkeypatch.setattr(g, "get_route", fake_route)
    monkeypatch.setattr(g, "snap_vertices", lambda v: v)
    monkeypatch.setattr(g, "sample_elevation_gain", lambda path: 5.0)

    course = g.generate_loop_course(34.0, 127.0, 1.8, set(), route_type="loop", angle_offset_deg=0.0)

    assert len(calls) == 2, "가지가 있었는데 다시 요청하지 않았다"
    assert retraced_ratio(course["path"]) < 0.02
    descriptions = [s["description"] for s in course["steps"]]
    assert "좌회전 후 막다른 길 따라 이동" not in descriptions
    assert course["distance_km"] == round(_length(loop) / 1000, 2)


def test_pruned_path_is_kept_when_rerouting_makes_it_worse(monkeypatch):
    """다시 요청했더니 Tmap이 다른 길로 새서 더 나빠지면, 잘라낸 경로를 그대로 쓰고
    잘린 갈림길 안내(막다른 길로 들어가라는 안내)는 뺀다 — 틀린 안내를 남기지 않는다."""
    from src.recommend import generate_live as g

    loop = _square()
    spurred = _with_spur(loop, at_index=5)
    worse = _with_spur(_with_spur(loop, at_index=5), at_index=30)
    calls = []

    def fake_route(start, end, start_name="", end_name="", waypoints=None):
        calls.append(1)
        if len(calls) == 1:
            return _route(spurred, [
                (spurred[5][0], spurred[5][1], "좌회전 후 막다른 길로"),
                (spurred[8][0], spurred[8][1], "막다른 길 끝에서 유턴"),
                (loop[20][0], loop[20][1], "직진 후 해안로 따라 이동"),
            ])
        return _route(worse)

    monkeypatch.setattr(g, "get_route", fake_route)
    monkeypatch.setattr(g, "sample_elevation_gain", lambda path: 5.0)

    course = g.generate_loop_course(34.0, 127.0, 1.8, set(), route_type="loop", angle_offset_deg=0.0)

    assert retraced_ratio(course["path"]) < 0.02
    assert abs(course["distance_km"] - round(_length(loop) / 1000, 2)) <= 0.03
    descriptions = [s["description"] for s in course["steps"]]
    assert "좌회전 후 막다른 길로" not in descriptions
    assert "막다른 길 끝에서 유턴" not in descriptions
    assert "직진 후 해안로 따라 이동" in descriptions


def test_clean_loop_is_not_requested_twice(monkeypatch):
    from src.recommend import generate_live as g

    calls = []
    monkeypatch.setattr(g, "get_route", lambda *a, **k: calls.append(1) or _route(_square()))
    monkeypatch.setattr(g, "snap_vertices", lambda v: v)
    monkeypatch.setattr(g, "sample_elevation_gain", lambda path: 5.0)
    g.generate_loop_course(34.0, 127.0, 1.8, set(), route_type="loop", angle_offset_deg=0.0)
    assert len(calls) == 1


def test_small_block_lap_is_kept():
    """다른 길로 돌아와 제자리로 오는 작은 고리는 되짚는 게 아니다 — 남겨야 한다."""
    loop = _square()
    base = loop[5]
    # 안쪽으로 올라갔다가 옆 골목으로 내려와 제자리로 — 갈 때와 올 때가 다른 길
    lap = [[base[0] + D * 0.5, base[1]], [base[0] + D * 0.5, base[1] + D * 0.5],
           [base[0] + D * 0.25, base[1] + D * 0.5], base]
    with_lap = loop[:6] + lap + loop[6:]
    assert len(prune_spurs(with_lap)) == len(with_lap)
