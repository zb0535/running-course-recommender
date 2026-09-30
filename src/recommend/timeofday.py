"""지금이 몇 시인지에 따라 어울리는 코스가 달라진다.

시간대는 사용자에게 물어볼 필요가 없다 — 요청이 들어온 시각을 보면 안다.
(밤에 미리 내일 아침 코스를 짜는 경우를 위해 `time_of_day`로 직접 지정할 수는 있다.)

무엇을 기준으로 판단하는가: `safety_score`가 아직 모든 코스에서 0.75 고정값이라 안전 점수로는
밤낮을 구분할 수 없다. 대신 실제로 코스마다 다른 값인 녹지비율과 신호등 밀도를 쓴다.
녹지비율이 높다는 건 낮에는 그늘이 있다는 뜻이지만, 밤에는 인적이 드물다는 뜻이기도 하다.
"""
from datetime import datetime

PERIODS = ("morning", "afternoon", "evening", "night")


def current_period(now: datetime = None) -> str:
    hour = (now or datetime.now()).hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def time_fit_match(course: dict, user: dict) -> float:
    """지금 시간대에 이 코스가 얼마나 어울리는지 0~1."""
    period = user.get("time_of_day") or current_period()
    if period not in PERIODS:
        return 0.5

    green = course.get("green_ratio", 0.5)

    if period == "night":
        # 인적 드문 길을 피한다. 신호등이 있다는 건 사람과 차가 다니는 길이라는 뜻이다.
        signals_per_km = course.get("traffic_signal_count", 0) / max(course.get("distance_km", 0), 0.1)
        populated = min(signals_per_km / 3.0, 1.0)
        return round(max(0.0, min(1.0, 0.65 * (1 - green) + 0.35 * populated)), 3)

    if period == "afternoon":
        return round(max(0.0, min(1.0, green)), 3)  # 햇볕을 피할 그늘

    return 0.5  # 아침·저녁은 어느 쪽도 특별히 유리하지 않다
