"""누구와 함께 뛰는지에 따라 코스 조건이 달라진다.

"혼자 / 같이" 두 갈래로는 부족하다. 유아차는 평탄함과 끊김 없음이 전부이고, 소형견은
같은 5km도 무리이며, 러닝크루는 여럿이 나란히 달릴 수 있는 길이어야 한다. 그래서 동반자를
구체적으로 나누고, 각각을 (가중치 조정 + 상한선) 두 가지로 반영한다.

가중치만으로는 부족한 이유: 유아차에 200m 오르막 코스가 "조금 덜 어울리는 정도"가 아니라
아예 불가능한 것처럼, 어떤 조건은 점수가 아니라 상한선으로 걸러야 한다.
"""

COMPANIONS = {
    "혼자": "혼자 달리기",
    "소형견": "소형견과 함께",
    "반려견": "중·대형견과 함께",
    "유아차": "유아차를 밀면서",
    "어린이": "어린이와 함께",
    "러닝크루": "여럿이 함께",
    "어르신": "어르신과 함께",
}

# 가중치 배율. 정규화되므로 상대적인 크기만 의미가 있다.
COMPANION_WEIGHT_BIAS = {
    "소형견": {"signal_free": 1.6, "elevation": 1.4, "distance": 1.2},
    "반려견": {"signal_free": 1.5, "tag_match": 1.2},
    "유아차": {"signal_free": 1.8, "elevation": 1.8, "safety": 1.3},
    "어린이": {"safety": 1.6, "signal_free": 1.5, "elevation": 1.3},
    "러닝크루": {"tag_match": 1.3, "signal_free": 1.3},
    "어르신": {"elevation": 1.7, "safety": 1.5, "signal_free": 1.3},
}

# 점수로 밀어내는 게 아니라 아예 후보에서 빼야 하는 조건
INF = float("inf")
COMPANION_LIMITS = {
    "혼자": {"max_distance_km": INF, "max_elevation_gain_m": INF},
    "소형견": {"max_distance_km": 3.0, "max_elevation_gain_m": 60},
    "반려견": {"max_distance_km": 6.0, "max_elevation_gain_m": 120},
    "유아차": {"max_distance_km": 5.0, "max_elevation_gain_m": 40},
    "어린이": {"max_distance_km": 4.0, "max_elevation_gain_m": 70},
    "러닝크루": {"max_distance_km": INF, "max_elevation_gain_m": INF},
    "어르신": {"max_distance_km": 4.0, "max_elevation_gain_m": 50},
}


def companion_options() -> list:
    """앱 온보딩에 뿌릴 선택지. 문구를 앱에 하드코딩하지 않도록 서버가 내려준다."""
    return [{"value": value, "label": label} for value, label in COMPANIONS.items()]


def companion_limits(companion: str) -> dict:
    return COMPANION_LIMITS.get(companion, COMPANION_LIMITS["혼자"])


def tune_for_companion(weights: dict, companion: str) -> dict:
    bias = COMPANION_WEIGHT_BIAS.get(companion)
    if not bias:
        return weights
    adjusted = {k: v * bias.get(k, 1.0) for k, v in weights.items()}
    total = sum(adjusted.values())
    return {k: v / total for k, v in adjusted.items()}


def fits_companion(course: dict, companion: str, target_km: float = None) -> bool:
    """상한선을 넘는 코스는 추천 후보에서 제외한다.

    긴 코스는 목표 거리로 잘려서 나가므로, 코스 전체가 아니라 실제로 뛰게 될 구간으로 판단한다.
    전체 길이로 거르면(왕복은 거리·고도가 2배) 짧게 뛸 사람에게 맞는 코스까지 전부 탈락한다.
    """
    limits = companion_limits(companion)
    full_km = course.get("distance_km", 0)
    run_km = min(full_km, target_km) if target_km else full_km
    run_elev = course.get("elevation_gain_m", 0) * (run_km / full_km if full_km else 1)  # 구간비례 근사
    return run_km <= limits["max_distance_km"] and run_elev <= limits["max_elevation_gain_m"]
