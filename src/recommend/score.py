"""규칙 기반 가중치 스코어링으로 사용자 조건에 맞는 코스를 추천한다.

지도학습이 아니므로 라벨링된 학습 데이터셋 없이, 온보딩 입력 자체를 매칭 조건으로 사용한다.
"""
import math

from .companion import fits_companion, tune_for_companion
from .profile import elevation_target_m, resolve_target_distance_km, tune_weights
from .timeofday import time_fit_match

DEFAULT_WEIGHTS = {
    "distance": 0.22,
    "elevation": 0.13,
    "safety": 0.13,
    "signal_free": 0.18,
    "tag_match": 0.22,
    "time_fit": 0.12,
}

DEFAULT_WEIGHTS_WITH_ENV = {
    "distance": 0.18,
    "elevation": 0.09,
    "safety": 0.13,
    "signal_free": 0.14,
    "tag_match": 0.18,
    "environment": 0.18,
    "time_fit": 0.10,
}

FACTOR_LABELS = {
    "distance": "거리 적합도",
    "elevation": "고도 적합도",
    "safety": "안전도",
    "signal_free": "신호 적음",
    "tag_match": "선호 풍경",
    "environment": "현재 날씨",
    "time_fit": "시간대 적합도",
    "route_diversity": "왕복 경로 분리도",
}


def _decay(diff: float, tolerance: float) -> float:
    """차이가 tolerance일 때 약 0.37(1/e)로 떨어지는 완만한 감쇠 함수."""
    if tolerance <= 0:
        return 1.0 if diff == 0 else 0.0
    return math.exp(-(diff / tolerance) ** 2)


def distance_match(course: dict, user: dict) -> float:
    """코스가 선호 거리보다 길어도 감점하지 않는다 — 중간에 끊고 돌아오면 되므로 완전 매칭으로 취급.
    짧은 코스는 더 늘릴 방법이 없으니(도로가 거기서 끝남) 부족한 만큼 감쇠 페널티를 준다."""
    preferred = resolve_target_distance_km(user)
    if not preferred:
        return 0.5
    actual = course.get("distance_km", 0)
    if course.get("source") == "live_generated" and course.get("route_type") == "roundtrip":
        return _decay(abs(actual - preferred), tolerance=preferred * 0.3 + 0.5)
    if actual >= preferred:
        return 1.0
    return _decay(preferred - actual, tolerance=preferred * 0.3 + 0.5)


def elevation_match(course: dict, user: dict) -> float:
    target = elevation_target_m(user)
    diff = abs(course.get("elevation_gain_m", 0) - target)
    return _decay(diff, tolerance=target * 0.6 + 10)


def safety_match(course: dict, user: dict) -> float:
    return max(0.0, min(1.0, course.get("safety_score", 0)))


def signal_free_match(course: dict, user: dict) -> float:
    """끊김 정도는 신호등 총 개수가 아니라 km당 밀도로 봐야 한다.

    총 개수로 보면 9km 코스(신호등 30개)가 2km 코스(0개)보다 무조건 불리해지는데,
    실제로 달리면서 체감하는 건 "얼마나 자주 멈추느냐"이지 총 몇 번 멈췄느냐가 아니다.
    """
    tags = set(user.get("environment_tags", []))
    distance_km = max(course.get("distance_km", 0), 0.1)
    density = course.get("traffic_signal_count", 0) / distance_km
    base = 1.0 / (1.0 + density)
    if "차없는길" in tags:
        return base
    return 0.5 + 0.5 * base


def tag_match(course: dict, user: dict) -> float:
    user_tags = set(user.get("environment_tags", []))
    course_tags = set(course.get("tags", []))
    if not user_tags:
        return 0.5
    overlap = user_tags & course_tags
    return len(overlap) / len(user_tags)


def score_course_breakdown(course: dict, user: dict, weights: dict = None,
                           env_context: dict = None) -> dict:
    if weights is None:
        weights = DEFAULT_WEIGHTS_WITH_ENV if env_context is not None else DEFAULT_WEIGHTS
    weights = tune_weights(weights, user.get("purpose"))
    weights = tune_for_companion(weights, user.get("companion"))

    scores = {
        "distance": distance_match(course, user),
        "elevation": elevation_match(course, user),
        "safety": safety_match(course, user),
        "signal_free": signal_free_match(course, user),
        "tag_match": tag_match(course, user),
        "time_fit": time_fit_match(course, user),
    }
    if "environment" in weights:
        from .environment import environment_score
        scores["environment"] = environment_score(env_context or {})

    if course.get("source") == "live_generated" and course.get("route_type") == "roundtrip":
        diversity_weight = 0.25
        distance_priority_bonus = 0.25
        remaining_weight = 1 - diversity_weight - distance_priority_bonus
        base_weight_total = sum(weights.values()) or 1.0
        weights = {
            key: weight / base_weight_total * remaining_weight
            for key, weight in weights.items()
        }
        weights["distance"] += distance_priority_bonus
        weights["route_diversity"] = diversity_weight
        scores["route_diversity"] = 1 - max(0.0, min(1.0, course.get("route_overlap_ratio", 1.0)))

    factors = [
        {
            "key": key,
            "label": FACTOR_LABELS[key],
            "score": scores[key],
            "weight": weights[key],
            "contribution": scores[key] * weights[key],
        }
        for key in weights
    ]
    return {"total": sum(factor["contribution"] for factor in factors), "factors": factors}


def score_course(course: dict, user: dict, weights: dict = None, env_context: dict = None) -> float:
    return score_course_breakdown(course, user, weights, env_context)["total"]


def filter_by_required_tags(courses: list, user: dict) -> list:
    """environment_tags가 있으면, 그중 하나라도 겹치는 코스만 남긴다.

    tag_match는 score_course()의 가중치 중 하나(25%)일 뿐이라, 사용자가 "바다뷰"를 골라도
    바다가 전혀 없는 코스가 다른 점수(거리/고도/안전)만으로 순위에 끼어드는 문제가 있었다.
    명시적으로 고른 태그는 소프트 가중치가 아니라 필수 조건으로 다뤄야 실사용에 맞다.
    """
    user_tags = set(user.get("environment_tags", []))
    if not user_tags:
        return courses
    return [c for c in courses if user_tags & set(c.get("tags", []))]


def recommend(courses: list, user: dict, top_n: int = 5, weights: dict = None, env_context_map: dict = None) -> list:
    """(course, score) 튜플 리스트를 점수 내림차순으로 반환.

    사용자가 environment_tags를 지정하면 그 태그를 하나도 안 가진 코스는 결과에서 제외한다
    (예: "바다뷰"를 골랐는데 내륙 코스가 뜨는 것을 방지). 남는 후보끼리는 기존 가중치 점수로 정렬.
    env_context_map: {course_id: environment_context} 를 주면 실시간 날씨/대기질을 점수에 반영.
    """
    candidates = filter_by_required_tags(courses, user)
    companion = user.get("companion")
    if companion:
        # 유아차에 200m 오르막은 "덜 어울리는 정도"가 아니라 불가능한 코스다 — 점수가 아니라 제외로 다룬다
        target_km = resolve_target_distance_km(user)
        candidates = [c for c in candidates if fits_companion(c, companion, target_km)]
    scored = [
        (c, score_course(c, user, weights, env_context=(env_context_map or {}).get(c["id"]) if env_context_map else None))
        for c in candidates
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_n]
