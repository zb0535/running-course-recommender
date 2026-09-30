"""코스별 종합 치안 점수.

그동안 `safety_score`는 모든 코스에서 0.75 고정값이었다. 스코어링 가중치의 13~15%가
아무 코스도 구분하지 못한 채로 돌아가고 있었다는 뜻이다.

CCTV 개수 하나로 치안을 대표할 수는 없어서 세 가지를 나눠 보고 합친다:

- 감시 장비가 있는가 (CCTV 밀도)
- 밤에도 사람이 다니는가 (편의점 밀도 — 24시간 영업이라 야간 유동인구의 대리 지표가 된다)
- 도움을 청할 곳이 가까운가 (지구대·파출소까지 거리)

한계를 분명히 해둔다: 가로등은 OSM에 한국 데이터가 사실상 없어서(광주 전역 0개) 빠져 있고,
CCTV도 지자체 공식 데이터가 아니라 OSM 수집분이라 실제보다 적게 잡힌다. 공공데이터포털에서
가로등·CCTV 데이터셋을 활용신청하면 같은 구조에 소스만 추가하면 된다.
"""
from ..data_collection.enrich import haversine_m

NEAR_PATH_M = 150          # 경로에서 이 거리 안에 있어야 그 코스와 관련 있다고 본다
POLICE_NEAR_M = 300        # 이 정도면 바로 달려갈 수 있는 거리
POLICE_FAR_M = 2000        # 이보다 멀면 도움을 기대하기 어렵다
# 만점 기준은 실제 코스 26개의 밀도 분포 상위 10% 지점에 맞췄다. 너무 낮게 잡으면
# 도심 코스가 전부 만점으로 뭉쳐 서로 구분되지 않는다(2.0/3.0으로 뒀을 때 5개가 동점이었다).
CCTV_PER_KM_FULL = 3.0     # 실측 분포: 중앙 1.6 / 90% 2.8 / 최대 3.6
SHOPS_PER_KM_FULL = 4.0    # 실측 분포: 중앙 1.9 / 90% 3.8 / 최대 5.4

WEIGHTS = {"cctv": 0.40, "nightlife": 0.35, "police": 0.25}


def _path_length_km(path: list) -> float:
    if len(path) < 2:
        return 0.0
    return sum(haversine_m(path[i - 1], path[i]) for i in range(1, len(path))) / 1000


def _count_near_path(points: list, path: list, radius_m: float = NEAR_PATH_M) -> int:
    count = 0
    for p in points:
        if any(haversine_m((p["lat"], p["lng"]), (q[0], q[1])) <= radius_m for q in path):
            count += 1
    return count


def _nearest_distance_m(points: list, path: list) -> float:
    best = float("inf")
    for p in points:
        for q in path:
            d = haversine_m((p["lat"], p["lng"]), (q[0], q[1]))
            if d < best:
                best = d
    return best


def score_components(path: list, cctv: list, convenience: list, police: list) -> dict:
    """세 항목을 각각 0~1로 정규화해서 반환. 합치기 전 값이라 어떤 항목이 약한지 볼 수 있다."""
    length_km = max(_path_length_km(path), 0.1)

    cctv_per_km = _count_near_path(cctv, path) / length_km
    shops_per_km = _count_near_path(convenience, path) / length_km

    nearest_police = _nearest_distance_m(police, path)
    if nearest_police <= POLICE_NEAR_M:
        police_score = 1.0
    elif nearest_police >= POLICE_FAR_M:
        police_score = 0.0
    else:
        police_score = 1 - (nearest_police - POLICE_NEAR_M) / (POLICE_FAR_M - POLICE_NEAR_M)

    return {
        "cctv": round(min(cctv_per_km / CCTV_PER_KM_FULL, 1.0), 3),
        "nightlife": round(min(shops_per_km / SHOPS_PER_KM_FULL, 1.0), 3),
        "police": round(police_score, 3),
    }


def safety_index(path: list, cctv: list, convenience: list, police: list) -> float:
    parts = score_components(path, cctv, convenience, police)
    return round(sum(parts[k] * w for k, w in WEIGHTS.items()), 3)
