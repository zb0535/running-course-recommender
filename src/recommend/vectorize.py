"""사용자 조건 / 코스 후보를 스키마상의 벡터로 변환.

두 벡터는 축이 1:1 대응하지 않으므로(사용자: 체력/목적/거리/시간/고도선호/환경태그,
코스: 거리/고도/안전점수/신호등수/녹지비율/해안근접도/노면상태), 이 모듈은 각 벡터를
"보기 좋은 형태"로 만드는 역할만 하고 실제 매칭 점수 계산은 score.py가 담당한다.
"""

ELEVATION_PREFERENCE_TARGET_M = {
    "low": 20,
    "medium": 60,
    "high": 150,
}


def user_vector(user: dict) -> dict:
    return {
        "fitness_level": user.get("fitness_level"),
        "purpose": user.get("purpose"),
        "preferred_distance_km": user.get("preferred_distance_km"),
        "preferred_time_min": user.get("preferred_time_min"),
        "elevation_preference": user.get("elevation_preference", "medium"),
        "environment_tags": set(user.get("environment_tags", [])),
    }


def course_features(course: dict) -> dict:
    return {
        "distance_km": course.get("distance_km", 0),
        "elevation_gain_m": course.get("elevation_gain_m", 0),
        "safety_score": course.get("safety_score", 0),
        "traffic_signal_count": course.get("traffic_signal_count", 0),
        "green_ratio": course.get("green_ratio", 0),
        "coastline_proximity": course.get("coastline_proximity", 0),
        "surface": course.get("surface", "unknown"),
        "tags": set(course.get("tags", [])),
    }
