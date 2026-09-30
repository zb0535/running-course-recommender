"""턴바이턴 안내가 경로상 몇 미터 지점에서 나와야 하는지를 계산해 안내에 박아 둔다.

안내 지점을 "경로에서 가장 가까운 점"으로 그때그때 찾으면 왕복 코스에서 깨진다.
갈 때와 올 때가 같은 도로라 좌표가 거의 같고, 그러면 돌아오는 길 안내가 갈 때 구간으로
끌려가 절반쯤 달렸을 때 이미 "도착"이 뜨는 식이 된다.

안내는 경로를 따라 순서대로 나온다는 점을 이용해, 직전 안내 지점 이후 구간에서만
다음 지점을 찾는다. 이렇게 구한 위치(cum_m)를 코스에 저장해두면 자르기(trim.py)와
화면 표시(navigate.html)가 같은 기준을 쓰게 된다.
"""
from ..data_collection.enrich import haversine_m

ON_PATH_M = 2.0


def cumulative_distances(path: list) -> list:
    """경로 각 지점까지의 누적 거리(m)."""
    cum = [0.0]
    for i in range(1, len(path)):
        cum.append(cum[-1] + haversine_m(path[i - 1], path[i]))
    return cum


def assign_positions(steps: list, path: list) -> list:
    """각 안내에 cum_m(출발점으로부터의 거리)을 붙여 반환. 순서는 유지되고 뒤로 가지 않는다."""
    if not steps or not path:
        return []

    cum = cumulative_distances(path)
    placed = []
    search_from = 0
    for step in steps:
        best_i, best_d = search_from, float("inf")
        for i in range(search_from, len(path)):
            d = haversine_m((step["lat"], step["lng"]), path[i])
            if d < best_d:
                best_i, best_d = i, d
            # Tmap 안내 지점은 경로 꼭짓점 위에 있다. 앞으로 가다 처음 만나는 일치점이 정답이므로
            # 거기서 멈춘다 — 끝까지 훑으면 장거리 코스에서 요청 하나가 수 초씩 걸린다.
            if d <= ON_PATH_M:
                break
        placed.append({**step, "cum_m": cum[best_i]})
        # 다음 안내는 반드시 이 지점 "뒤"에서 찾는다. 같은 지점에 머물게 두면 왕복에서
        # 돌아오는 길 안내가 출발점(같은 좌표)에 그대로 붙어버린다.
        search_from = min(best_i + 1, len(path) - 1)
    return placed
