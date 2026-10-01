"""러닝 후 만족도로 사용자별 가중치를 학습한다.

같은 조건을 입력해도 사람마다 중요하게 여기는 게 다르다. 어떤 사람은 신호등에 걸리는 걸
못 참고, 어떤 사람은 오르막이 조금 있어도 경치가 좋으면 만족한다. 설문으로는 이걸 다 물을
수 없으니, 실제로 뛰고 난 평가에서 배운다.
"""
import pytest

from src.recommend import personalize as p
from src.recommend.score import DEFAULT_WEIGHTS_WITH_ENV

# 신호등 적음에서만 점수를 크게 받고 나머지는 평범한 코스
SIGNAL_FREE_COURSE = {"distance": 0.5, "elevation": 0.5, "safety": 0.5, "signal_free": 1.0,
                      "tag_match": 0.5, "time_fit": 0.5, "environment": 0.5}


def _profile():
    return p.new_profile("u1", survey={"experience_level": "beginner"})


class TestColdStart:
    def test_new_profile_starts_from_default_weights(self):
        prof = _profile()
        assert prof["weights"] == pytest.approx(DEFAULT_WEIGHTS_WITH_ENV)
        assert prof["n_feedback"] == 0

    def test_survey_is_kept(self):
        assert _profile()["survey"]["experience_level"] == "beginner"


class TestLearning:
    def test_liking_a_course_raises_the_weight_it_was_strong_on(self):
        prof = _profile()
        before = prof["weights"]["signal_free"]
        p.learn(prof, SIGNAL_FREE_COURSE, rating=5)
        assert prof["weights"]["signal_free"] > before

    def test_disliking_it_lowers_that_weight(self):
        prof = _profile()
        before = prof["weights"]["signal_free"]
        p.learn(prof, SIGNAL_FREE_COURSE, rating=1)
        assert prof["weights"]["signal_free"] < before

    def test_neutral_rating_changes_nothing(self):
        prof = _profile()
        before = dict(prof["weights"])
        p.learn(prof, SIGNAL_FREE_COURSE, rating=3)
        assert prof["weights"] == pytest.approx(before)

    def test_weights_stay_normalized_and_bounded_after_many_updates(self):
        prof = _profile()
        for _ in range(200):
            p.learn(prof, SIGNAL_FREE_COURSE, rating=5)
        assert sum(prof["weights"].values()) == pytest.approx(1.0)
        assert all(p.MIN_WEIGHT - 1e-9 <= w <= p.MAX_WEIGHT + 1e-9 for w in prof["weights"].values())

    def test_early_feedback_moves_weights_more_than_later_feedback(self):
        """처음 몇 번의 평가로 빠르게 적응하고, 쌓일수록 한 번의 평가에 덜 흔들린다."""
        prof = _profile()
        start = prof["weights"]["signal_free"]
        p.learn(prof, SIGNAL_FREE_COURSE, rating=5)
        first_step = prof["weights"]["signal_free"] - start
        for _ in range(30):
            p.learn(prof, SIGNAL_FREE_COURSE, rating=3)  # 횟수만 쌓는다
        before = prof["weights"]["signal_free"]
        p.learn(prof, SIGNAL_FREE_COURSE, rating=5)
        later_step = prof["weights"]["signal_free"] - before
        assert later_step < first_step

    def test_invalid_rating_is_rejected(self):
        with pytest.raises(ValueError):
            p.learn(_profile(), SIGNAL_FREE_COURSE, rating=6)


class TestLearningFromAlternatives:
    def test_factor_that_did_not_differ_from_alternatives_is_not_credited(self):
        """같이 추천된 코스들이 전부 거리가 맞았다면, 거리는 이 선택의 이유가 아니다."""
        prof = _profile()
        before = prof["weights"]["distance"]
        liked = {**SIGNAL_FREE_COURSE, "distance": 1.0}
        alternatives_mean = {**SIGNAL_FREE_COURSE, "distance": 1.0, "signal_free": 0.3}
        p.learn(prof, liked, rating=5, baseline=alternatives_mean)
        after = prof["weights"]
        assert after["signal_free"] > DEFAULT_WEIGHTS_WITH_ENV["signal_free"]
        # 정규화로 약간 줄 수는 있어도, 신호등만큼 올라가서는 안 된다
        assert after["distance"] <= before + 1e-9

    def test_single_recommendation_falls_back_to_within_course_comparison(self):
        prof = _profile()
        p.learn(prof, SIGNAL_FREE_COURSE, rating=5, baseline=None)
        assert prof["weights"]["signal_free"] > DEFAULT_WEIGHTS_WITH_ENV["signal_free"]


class TestWeightsForScoring:
    def test_environment_weight_is_dropped_when_live_environment_is_off(self):
        weights = p.weights_for(_profile(), with_environment=False)
        assert "environment" not in weights
        assert sum(weights.values()) == pytest.approx(1.0)


class TestExplanation:
    def test_explains_what_the_runner_cares_about_more_than_average(self):
        prof = _profile()
        for _ in range(5):
            p.learn(prof, SIGNAL_FREE_COURSE, rating=5)
        top = p.explain(prof)[0]
        assert top["factor"] == "signal_free"
        assert top["change"] > 0
        assert "신호등" in top["label"]


class TestExplanationIgnoresNormalizationDrift:
    def test_untouched_factors_are_not_described_as_preferred(self):
        """다른 항목이 줄면 합을 1로 맞추느라 나머지가 같이 오른다. 그걸 '중시한다'고 말하면 안 된다."""
        prof = _profile()
        liked = {k: 0.5 for k in SIGNAL_FREE_COURSE}
        liked.update({"signal_free": 1.0, "safety": 0.1, "elevation": 0.1})
        baseline = {k: 0.5 for k in SIGNAL_FREE_COURSE}
        p.learn(prof, liked, rating=5, baseline=baseline)

        rows = {r["factor"]: r for r in p.explain(prof)}
        assert p.explain(prof)[0]["factor"] == "signal_free"
        for untouched in ("distance", "tag_match", "time_fit", "environment"):
            assert abs(rows[untouched]["preference"]) < 0.02, untouched
        assert rows["safety"]["preference"] < 0
        assert rows["elevation"]["preference"] < 0


class TestPersistence:
    def test_profile_survives_save_and_load(self, tmp_path):
        path = tmp_path / "profiles.json"
        prof = _profile()
        p.learn(prof, SIGNAL_FREE_COURSE, rating=5)
        p.save_profile(prof, path)
        loaded = p.load_profile("u1", path)
        assert loaded["weights"] == pytest.approx(prof["weights"])
        assert loaded["n_feedback"] == 1

    def test_unknown_user_has_no_profile(self, tmp_path):
        assert p.load_profile("nobody", tmp_path / "profiles.json") is None
