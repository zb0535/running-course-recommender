"""러너 프로필(페이스·숙련도·목적)을 추천 파라미터로 바꾼다.

온보딩에서 받는 값들이 실제로 점수를 바꾸도록 연결하는 모듈:

- 페이스: 아는 사람은 직접 입력(분/km). 모르면 숙련도로 추정한다.
- 시간: "30분 뛸래"를 페이스로 나눠 목표 거리로 환산한다. 같은 30분이어도
  입문자와 상급자의 목표 거리는 달라야 한다.
- 목적: 숙련도마다 물어보는 항목이 다르다(입문자에게 "인터벌 훈련"을 물어봐야 소용없다).
  목적은 고도 목표와 가중치를 바꾼다 — 기록을 단축하려는 사람에게 신호등이 많은 코스는
  단순한 감점 요소가 아니라 목적 자체를 망치는 조건이기 때문이다.
"""

PACE_BY_EXPERIENCE = {
    "beginner": 8.0,
    "intermediate": 6.0,
    "advanced": 5.0,
}
DEFAULT_PACE_MIN_PER_KM = PACE_BY_EXPERIENCE["beginner"]

PURPOSES_BY_EXPERIENCE = {
    "beginner": ["체중 감량", "체력 기르기", "5km 완주"],
    "intermediate": ["체중 감량", "기록 단축", "꾸준한 습관"],
    "advanced": ["대회 준비", "기록 단축", "인터벌 훈련"],
}

# 목적별 고도 목표(m). 같은 "적당한 코스"라도 목적에 따라 원하는 오르막이 다르다.
PURPOSE_ELEVATION_TARGET_M = {
    "체중 감량": 30,    # 중간에 걷게 되면 지속이 안 됨 -> 완만하게
    "체력 기르기": 120,  # 부하가 목적이므로 오르막 환영
    "5km 완주": 20,     # 첫 완주가 목표 -> 최대한 평탄
    "기록 단축": 40,
    "꾸준한 습관": 50,
    "대회 준비": 90,
    "인터벌 훈련": 20,   # 속도 반복이 목적 -> 평탄한 구간 필요
}

# 목적별 가중치 배율. 정규화되므로 상대적인 크기만 의미가 있다.
PURPOSE_WEIGHT_BIAS = {
    "체중 감량": {"signal_free": 1.4, "elevation": 0.7},
    "체력 기르기": {"elevation": 1.4},
    "5km 완주": {"distance": 1.4, "safety": 1.3, "elevation": 0.6},
    "기록 단축": {"signal_free": 1.7, "distance": 1.3, "elevation": 0.6},
    "꾸준한 습관": {"safety": 1.3, "tag_match": 1.3},
    "대회 준비": {"distance": 1.5, "elevation": 1.2},
    "인터벌 훈련": {"signal_free": 1.8, "elevation": 0.5},
}


def resolve_pace(user: dict) -> float:
    """분/km. 직접 입력이 있으면 그대로, 없으면 숙련도로 추정한다."""
    pace = user.get("pace_min_per_km")
    if pace:
        return float(pace)
    return PACE_BY_EXPERIENCE.get(user.get("experience_level"), DEFAULT_PACE_MIN_PER_KM)


def resolve_target_distance_km(user: dict):
    """목표 거리(km). 거리를 직접 골랐으면 그대로, 시간만 골랐으면 페이스로 환산한다.

    동반자 상한보다 길면 상한으로 줄인다 — 유아차로 10km를 요청해도 5km 코스를 준다.
    """
    from .companion import companion_limits

    if user.get("preferred_distance_km"):
        target = user["preferred_distance_km"]
    elif user.get("preferred_time_min"):
        target = round(user["preferred_time_min"] / resolve_pace(user), 2)
    else:
        return None
    cap = companion_limits(user.get("companion"))["max_distance_km"]
    return min(target, cap)


def purposes_for(experience_level: str) -> list:
    """해당 숙련도에서 물어볼 목적 항목. 모르는 값이면 입문자 기준."""
    return PURPOSES_BY_EXPERIENCE.get(experience_level, PURPOSES_BY_EXPERIENCE["beginner"])


def elevation_target_m(user: dict) -> float:
    """목적이 있으면 목적 기준, 없으면 사용자가 고른 고도 선호 기준."""
    from .vectorize import ELEVATION_PREFERENCE_TARGET_M

    target = PURPOSE_ELEVATION_TARGET_M.get(user.get("purpose"))
    if target is not None:
        return target
    pref = user.get("elevation_preference") or "medium"
    return ELEVATION_PREFERENCE_TARGET_M.get(pref, ELEVATION_PREFERENCE_TARGET_M["medium"])


def tune_weights(weights: dict, purpose: str) -> dict:
    """목적에 맞게 가중치를 조정하고 합이 1이 되도록 정규화한다."""
    bias = PURPOSE_WEIGHT_BIAS.get(purpose)
    if not bias:
        return weights
    adjusted = {k: v * bias.get(k, 1.0) for k, v in weights.items()}
    total = sum(adjusted.values())
    return {k: v / total for k, v in adjusted.items()}
