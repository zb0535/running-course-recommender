"""러닝 후 만족도로 사용자별 가중치를 학습한다.

같은 조건을 입력해도 사람마다 중요하게 여기는 게 다르다. 어떤 사람은 신호등에 걸리는 걸
못 참고, 어떤 사람은 오르막이 좀 있어도 경치가 좋으면 만족한다. 설문으로는 이걸 다 물을 수
없으니 실제로 뛰고 난 평가에서 배운다.

학습 방식 (지수 경사 가중치 갱신, exponentiated gradient):
  코스마다 항목별 적합도(거리·고도·안전·신호등·풍경·시간대·날씨, 각 0~1)가 있다.
  만족도가 높은데 그 코스가 특히 강했던 항목이 있으면 → 그 항목이 이 사람에게 중요하다는 증거.
  만족도가 낮은데 강했던 항목이 있으면 → 그건 이 사람에게 별로 중요하지 않다는 증거.

      w_k ← w_k · exp(η · r · (s_k − s̄))      r = (평점−3)/2 ∈ [−1, 1]

  s̄는 그 코스의 항목 평균이라, "모든 항목이 고르게 좋았던 코스"는 어느 항목도 특별히 올리지
  않는다. 곱셈 갱신이라 가중치가 음수가 되지 않고, 갱신 후 합이 1이 되도록 정규화한다.
  학습률 η는 평가가 쌓일수록 줄어서, 처음엔 빠르게 적응하고 나중엔 한 번의 평가에 덜 흔들린다.

라벨된 학습 데이터 없이 사용자 한 명의 평가만으로 동작하고(콜드 스타트는 기본 가중치),
어떤 요소를 왜 중시하게 됐는지 설명할 수 있다.

별점 하나로는 "무엇이" 좋았고 나빴는지 알 수 없어서 위 방식은 다른 코스와의 차이로 추정한다.
그래서 항목별 질문도 받는다(FEEDBACK_QUESTIONS): 거리·오르막·멈춤·풍경·안심. 답은 추정이 아니라
그 항목에 대한 직접 증거라, 해당 항목의 가중치만 바로 고친다(learn_aspects). "힘들었다/평탄했다",
"길었다/짧았다"처럼 방향이 있는 답은 가중치가 아니라 목표값 자체를 옮긴다.
"""
import json
import math
import os
import threading

from .score import DEFAULT_WEIGHTS_WITH_ENV

PROFILES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "user_profiles.json")

BASE_LEARNING_RATE = 0.6
LEARNING_RATE_HALF_LIFE = 5   # 평가가 이만큼 쌓이면 학습률이 약 1/√2로 준다
MIN_WEIGHT = 0.03             # 한 항목이 완전히 무시되지 않게
MAX_WEIGHT = 0.45             # 한 항목이 추천을 독점하지 않게
RECENT_LIMIT = 30             # 평가를 기다리는 최근 추천 보관 개수

FACTOR_LABELS = {
    "distance": "원하는 거리에 맞는 코스",
    "elevation": "원하는 고도(오르막 정도)에 맞는 코스",
    "safety": "안전한 코스(CCTV·사람 왕래·치안시설)",
    "signal_free": "신호등에 덜 걸리는 코스",
    "tag_match": "고른 풍경(바다뷰·숲길 등)",
    "time_fit": "지금 시간대에 어울리는 코스",
    "environment": "날씨가 좋은 코스",
}

# 러닝 후 항목별 질문. 앱이 문항을 하드코딩하지 않도록 서버가 내려준다 (GET /onboarding/feedback).
# 각 답: (값, 보여줄 말, 중요도 변화, 방향)
#   중요도 변화: 불만(+1)은 그 항목이 이 사람에게 중요하다는 강한 증거, 칭찬(+0.5)은 약한 증거,
#               "보통"(-0.25)은 별로 신경 쓰지 않았다는 증거. "딱 좋았다"는 목표가 맞았다는 뜻이라 0.
#   방향: 목표값을 올릴지(+1) 내릴지(-1). 오르막과 거리에만 있다.
FEEDBACK_QUESTIONS = [
    {"key": "distance", "factor": "distance", "question": "거리는 어땠나요?", "options": [
        ("short", "짧았어요", 1.0, +1), ("good", "딱 좋았어요", 0.0, 0), ("long", "길었어요", 1.0, -1)]},
    {"key": "elevation", "factor": "elevation", "question": "오르막은 어땠나요?", "options": [
        ("flat", "너무 평탄했어요", 1.0, +1), ("good", "적당했어요", 0.0, 0), ("hard", "힘들었어요", 1.0, -1)]},
    {"key": "stops", "factor": "signal_free", "question": "신호등이나 횡단보도에서 자주 멈췄나요?", "options": [
        ("few", "거의 안 멈췄어요", 0.5, 0), ("ok", "보통이었어요", -0.25, 0), ("many", "자주 멈췄어요", 1.0, 0)]},
    {"key": "scenery", "factor": "tag_match", "question": "풍경은 어땠나요?", "options": [
        ("good", "좋았어요", 0.5, 0), ("ok", "보통이었어요", -0.25, 0), ("bad", "별로였어요", 1.0, 0)]},
    {"key": "safety", "factor": "safety", "question": "달리는 동안 안심됐나요?", "options": [
        ("safe", "안심됐어요", 0.5, 0), ("ok", "보통이었어요", -0.25, 0), ("unsafe", "불안했어요", 1.0, 0)]},
]
_ANSWERS = {q["key"]: {value: (q["factor"], importance, direction) for value, _, importance, direction in q["options"]}
            for q in FEEDBACK_QUESTIONS}
ASPECT_RATE = 0.5              # 항목별 답의 학습률 = 별점 학습률 × 이 값
ELEVATION_STEP = 1.15          # "힘들었다/평탄했다" 한 번에 오르막 목표를 이만큼 줄이거나 늘린다
ELEVATION_SCALE_RANGE = (0.5, 1.8)
DISTANCE_STEP = 1.07           # "길었다/짧았다" 한 번에 (시간으로 고른) 목표 거리를 이만큼 줄이거나 늘린다
DISTANCE_SCALE_RANGE = (0.7, 1.4)

_lock = threading.Lock()


def feedback_questions() -> list:
    return [{"key": q["key"], "question": q["question"],
             "options": [{"value": value, "label": label} for value, label, _, _ in q["options"]]}
            for q in FEEDBACK_QUESTIONS]


def invalid_aspects(aspects: dict) -> list:
    """문항에 없는 키나 값."""
    return [f"{key}={value}" for key, value in (aspects or {}).items() if value not in _ANSWERS.get(key, {})]


def learn_aspects(profile: dict, aspects: dict) -> dict:
    """항목별 답으로 그 항목의 가중치와 목표값을 고친다. n_feedback은 올리지 않는다(별점 쪽에서 센다)."""
    if not aspects:
        return profile
    eta = learning_rate(profile["n_feedback"]) * ASPECT_RATE
    weights = dict(profile["weights"])
    log = profile.setdefault("aspect_log", {})
    for key, value in aspects.items():
        factor, importance, direction = _ANSWERS[key][value]
        if factor in weights:
            weights[factor] *= math.exp(eta * importance)
        if direction and key == "elevation":
            low, high = ELEVATION_SCALE_RANGE
            scale = profile.get("elevation_scale", 1.0) * ELEVATION_STEP ** direction
            profile["elevation_scale"] = round(min(max(scale, low), high), 3)
        if direction and key == "distance":
            low, high = DISTANCE_SCALE_RANGE
            scale = profile.get("distance_scale", 1.0) * DISTANCE_STEP ** direction
            profile["distance_scale"] = round(min(max(scale, low), high), 3)
        counts = log.setdefault(key, {})
        counts[value] = counts.get(value, 0) + 1
    profile["weights"] = _normalize_bounded(weights)
    return profile


def answers_view(profile: dict) -> list:
    """지금까지 항목별로 뭐라고 답했는지 — 문항과 보기 그대로, 횟수와 함께."""
    log = profile.get("aspect_log", {})
    return [{"key": q["key"], "question": q["question"],
             "answers": [{"value": value, "label": label, "count": log.get(q["key"], {}).get(value, 0)}
                         for value, label, _, _ in q["options"]]}
            for q in FEEDBACK_QUESTIONS]


def answer_labels(aspects: dict) -> list:
    """{"stops": "many"} → ["자주 멈췄어요"] (이력을 사람이 읽을 수 있게)."""
    labels = {q["key"]: {value: label for value, label, _, _ in q["options"]} for q in FEEDBACK_QUESTIONS}
    return [labels[key][value] for key, value in (aspects or {}).items() if value in labels.get(key, {})]


def taste_summary(profile: dict) -> list:
    """이 사람의 취향을 사용자에게 보여줄 문장으로. 답한 내용에 근거가 있는 것만 말한다."""
    log = profile.get("aspect_log", {})

    def said(key, value):
        return log.get(key, {}).get(value, 0)

    lines = []
    if said("stops", "many"):
        lines.append(f"신호등에 자주 멈추는 걸 불편해하세요 ({said('stops', 'many')}번 말씀하셨어요). 덜 멈추는 코스를 먼저 추천해요.")
    elif said("stops", "few") >= 2:
        lines.append("멈추지 않고 달리는 코스에 만족하세요.")
    if said("scenery", "bad"):
        lines.append(f"풍경이 별로면 아쉬워하세요 ({said('scenery', 'bad')}번). 고른 풍경을 더 많이 지나는 코스를 먼저 추천해요.")
    elif said("scenery", "good") >= 2:
        lines.append("풍경 좋은 코스를 좋아하세요.")
    if said("safety", "unsafe"):
        lines.append(f"불안하게 느낀 코스가 있었어요 ({said('safety', 'unsafe')}번). 사람 왕래가 있고 밝은 길을 먼저 추천해요.")
    elevation = profile.get("elevation_scale", 1.0)
    if elevation <= 0.9:
        lines.append(f"오르막을 힘들어하셔서 고른 것보다 {round((1 - elevation) * 100)}% 완만한 코스를 찾아요.")
    elif elevation >= 1.1:
        lines.append(f"평탄한 코스가 심심하다고 하셔서 고른 것보다 오르막이 {round((elevation - 1) * 100)}% 더 있는 코스를 찾아요.")
    distance = profile.get("distance_scale", 1.0)
    if distance <= 0.95:
        lines.append(f"코스가 길다고 하셔서, 시간으로 고르실 때 거리를 {round((1 - distance) * 100)}% 짧게 잡아요.")
    elif distance >= 1.05:
        lines.append(f"코스가 짧다고 하셔서, 시간으로 고르실 때 거리를 {round((distance - 1) * 100)}% 길게 잡아요.")
    return lines


def new_profile(user_id: str, survey: dict = None) -> dict:
    return {
        "user_id": user_id,
        "survey": dict(survey or {}),
        "weights": dict(DEFAULT_WEIGHTS_WITH_ENV),
        "n_feedback": 0,
        "recent": {},  # course_id -> 추천 당시 항목별 적합도 (평가가 오면 이걸로 학습)
    }


def _normalize_bounded(weights: dict) -> dict:
    """합이 1이면서 각 값이 [MIN_WEIGHT, MAX_WEIGHT] 안에 들도록 맞춘다.

    잘라내고 다시 정규화하는 걸 반복하면 경계에서 왔다 갔다 하며 수렴하지 않는다. 경계를
    넘는 항목은 경계값으로 고정하고, 남은 몫을 나머지 항목에 비율대로 나누는 걸 넘는 항목이
    없어질 때까지 반복한다(항목 수만큼이면 끝난다).
    """
    fixed = {}
    free = {k: max(v, 1e-12) for k, v in weights.items()}
    for _ in range(len(weights) + 1):
        remaining = 1.0 - sum(fixed.values())
        total_free = sum(free.values())
        scaled = {k: v / total_free * remaining for k, v in free.items()}
        over = [k for k, v in scaled.items() if v > MAX_WEIGHT]
        if over:
            for k in over:
                fixed[k] = MAX_WEIGHT
                free.pop(k)
            continue
        under = [k for k, v in scaled.items() if v < MIN_WEIGHT]
        if under:
            for k in under:
                fixed[k] = MIN_WEIGHT
                free.pop(k)
            continue
        return {**fixed, **scaled}
    return dict(fixed)


def learning_rate(n_feedback: int) -> float:
    return BASE_LEARNING_RATE / math.sqrt(1 + n_feedback / LEARNING_RATE_HALF_LIFE)


def learn(profile: dict, component_scores: dict, rating: int, baseline: dict = None) -> dict:
    """평점(1~5)과 그 코스의 항목별 적합도로 가중치를 갱신한다. 갱신된 프로필을 반환.

    baseline은 같이 추천됐던 코스들의 항목별 평균이다. "이 코스가 다른 선택지보다 무엇이
    달랐나"로 학습해야 한다 — 절대 점수로 학습하면, 모든 코스가 목표 거리에 맞았을 때도
    만족할 때마다 거리 가중치가 올라가서 "당신은 거리를 중시합니다" 같은 틀린 설명이 나온다.
    비교할 선택지가 없으면(추천 1개) 그 코스 안에서 어느 항목이 두드러졌는지로 대신한다.
    """
    if rating not in (1, 2, 3, 4, 5):
        raise ValueError("만족도는 1~5 사이 정수여야 합니다.")

    weights = profile["weights"]
    shared = [k for k in weights if k in component_scores]
    if shared:
        reward = (rating - 3) / 2
        if baseline:
            reference = {k: baseline.get(k, component_scores[k]) for k in shared}
        else:
            mean = sum(component_scores[k] for k in shared) / len(shared)
            reference = {k: mean for k in shared}
        eta = learning_rate(profile["n_feedback"])
        updated = {
            k: w * math.exp(eta * reward * (component_scores[k] - reference[k])) if k in reference else w
            for k, w in weights.items()
        }
        profile["weights"] = _normalize_bounded(updated)
    profile["n_feedback"] += 1
    return profile


def weights_for(profile: dict, with_environment: bool) -> dict:
    """점수 계산에 쓸 가중치. 실시간 날씨를 안 쓰면 날씨 항목을 빼고 다시 합을 1로 맞춘다."""
    weights = dict(profile["weights"])
    if not with_environment:
        weights.pop("environment", None)
        total = sum(weights.values())
        weights = {k: v / total for k, v in weights.items()}
    return weights


def remember_recommendation(profile: dict, course_id: str, component_scores: dict,
                            alternatives_mean: dict = None) -> None:
    """나중에 평가가 오면 '그때 무엇 때문에, 무엇과 비교해 추천했는지'로 학습하기 위해 남긴다."""
    recent = profile.setdefault("recent", {})
    recent.pop(course_id, None)
    recent[course_id] = {
        "scores": {k: round(v, 4) for k, v in component_scores.items()},
        "baseline": {k: round(v, 4) for k, v in alternatives_mean.items()} if alternatives_mean else None,
    }
    while len(recent) > RECENT_LIMIT:
        recent.pop(next(iter(recent)))


def recalled(profile: dict, course_id: str):
    """(항목별 적합도, 비교 기준) 또는 None."""
    entry = profile.get("recent", {}).get(course_id)
    if entry is None:
        return None
    return entry["scores"], entry.get("baseline")


def explain(profile: dict) -> list:
    """이 사람이 기본값보다 더/덜 중시하게 된 항목을 큰 순서로.

    가중치 합은 항상 1이라, 어떤 항목이 줄면 선택과 무관했던 항목들도 같이 조금씩 오른다.
    그 상승을 "중시한다"고 설명하면 틀린 설명이 된다(실제로 평가 1번에 '날씨를 중시한다'가
    나왔었다). 그래서 기본값 대비 배율의 로그를 보고, 그중 가운데 값(=학습에 안 걸린 항목들이
    공통으로 받은 정규화 몫)을 빼서 "실제로 더/덜 중시하게 된 정도"를 preference로 준다.
    """
    ratios = {k: math.log(w / DEFAULT_WEIGHTS_WITH_ENV[k])
              for k, w in profile["weights"].items() if DEFAULT_WEIGHTS_WITH_ENV.get(k)}
    neutral = sorted(ratios.values())[len(ratios) // 2] if ratios else 0.0

    rows = []
    for k, w in profile["weights"].items():
        base = DEFAULT_WEIGHTS_WITH_ENV.get(k, 0)
        rows.append({
            "factor": k,
            "label": FACTOR_LABELS.get(k, k),
            "weight": round(w, 3),
            "change": round(w - base, 3),
            "preference": round(ratios.get(k, 0.0) - neutral, 3),
        })
    rows.sort(key=lambda r: r["preference"], reverse=True)
    return rows


def _read_all(path) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_profile(user_id: str, path=None):
    """프로필은 사용자 DB(user_store)에 둔다. path를 주면 예전 방식의 JSON 파일에서 읽는다."""
    from . import user_store

    if path is not None:
        with _lock:
            return _read_all(path).get(user_id)
    profile = user_store.get_profile(user_id)
    if profile is None:
        # 예전에는 JSON 파일 하나에 모든 사용자를 넣어 두었다. 거기 있던 사람은 처음 찾을 때 옮겨 온다
        with _lock:
            profile = _read_all(PROFILES_PATH).get(user_id)
        if profile is not None:
            user_store.put_profile(profile)
            forget_legacy(user_id)
    return profile


def forget_legacy(user_id: str) -> None:
    """예전 JSON 파일에서 이 사람을 지운다.

    DB로 옮긴 뒤에도 파일에 남아 있으면, 데이터를 지우거나 계정으로 옮긴 사람이 다음 조회 때
    파일에서 되살아난다(실제로 가입 후에도 손님 id로 옛 취향이 조회됐다).
    """
    with _lock:
        profiles = _read_all(PROFILES_PATH)
        if user_id not in profiles:
            return
        del profiles[user_id]
        tmp = f"{PROFILES_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROFILES_PATH)


def save_profile(profile: dict, path=None) -> None:
    from . import user_store

    if path is None:
        user_store.put_profile(profile)
        return
    # 여러 요청이 동시에 저장해도 한쪽이 다른 쪽을 덮어쓰지 않게 읽기-수정-쓰기를 묶는다
    with _lock:
        profiles = _read_all(path)
        profiles[profile["user_id"]] = profile
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
