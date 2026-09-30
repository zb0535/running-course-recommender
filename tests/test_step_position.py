"""턴바이턴 안내가 경로상 '어디에서' 나와야 하는지를 정한다.

왕복 코스는 갈 때와 올 때가 같은 도로다. 그래서 안내 지점을 "경로에서 가장 가까운 점"으로
찾으면 돌아오는 길 안내가 갈 때 구간으로 끌려가 엉뚱한 시점에 안내된다.
안내는 경로를 따라 순서대로 나오므로, 앞에서 찾은 지점 이후에서만 다음 지점을 찾아야 한다.
"""
from src.recommend.step_position import assign_positions, cumulative_distances

# 남북으로 뻗은 직선 경로(약 1.11km 간격)를 갔다가 되돌아오는 왕복
OUT = [[34.70, 127.70], [34.71, 127.70], [34.72, 127.70]]
ROUNDTRIP_PATH = OUT + [[34.71, 127.70], [34.70, 127.70]]


def test_cumulative_distances_increase_monotonically():
    cum = cumulative_distances(OUT)
    assert cum[0] == 0
    assert cum[1] < cum[2]


def test_outbound_and_return_guidance_at_same_spot_are_placed_apart():
    """같은 좌표의 안내라도 갈 때 것과 올 때 것은 다른 시점에 나와야 한다."""
    steps = [
        {"lat": 34.70, "lng": 127.70, "description": "출발"},
        {"lat": 34.71, "lng": 127.70, "description": "갈 때 좌회전"},
        {"lat": 34.72, "lng": 127.70, "description": "반환점"},
        {"lat": 34.71, "lng": 127.70, "description": "올 때 우회전"},  # 갈 때와 같은 좌표
        {"lat": 34.70, "lng": 127.70, "description": "도착"},
    ]
    placed = assign_positions(steps, ROUNDTRIP_PATH)
    by_desc = {s["description"]: s["cum_m"] for s in placed}
    assert by_desc["갈 때 좌회전"] < by_desc["반환점"] < by_desc["올 때 우회전"]


def test_positions_never_go_backwards():
    steps = [{"lat": p[0], "lng": p[1], "description": str(i)} for i, p in enumerate(ROUNDTRIP_PATH)]
    positions = [s["cum_m"] for s in assign_positions(steps, ROUNDTRIP_PATH)]
    assert positions == sorted(positions)


def test_last_step_lands_near_the_end_of_a_roundtrip():
    steps = [
        {"lat": 34.70, "lng": 127.70, "description": "출발"},
        {"lat": 34.70, "lng": 127.70, "description": "도착"},
    ]
    placed = assign_positions(steps, ROUNDTRIP_PATH)
    total = cumulative_distances(ROUNDTRIP_PATH)[-1]
    assert placed[-1]["cum_m"] == total


def test_handles_empty_input():
    assert assign_positions([], OUT) == []
    assert assign_positions([{"lat": 34.7, "lng": 127.7, "description": "x"}], []) == []
