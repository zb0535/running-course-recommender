"""종합 치안 점수.

CCTV 하나로 치안을 대표할 수 없다. 사람이 안심하고 달릴 수 있는가는 (감시 장비 + 밤에도
사람이 다니는가 + 도움을 청할 곳이 가까운가)의 조합이다. 각 항목을 따로 정규화한 뒤 합친다.
"""
import pytest

from src.recommend.safety import safety_index, score_components

PATH = [[35.15, 126.85], [35.16, 126.85], [35.17, 126.85]]  # 약 2.2km 직선


def _near(lat, lng):
    return {"lat": lat, "lng": lng}


class TestComponents:
    def test_more_cctv_raises_the_score(self):
        few = score_components(PATH, cctv=[_near(35.15, 126.85)], convenience=[], police=[])
        many = score_components(PATH, cctv=[_near(35.15 + i * 0.002, 126.85) for i in range(10)],
                                convenience=[], police=[])
        assert many["cctv"] > few["cctv"]

    def test_nearby_police_raises_the_score_more_than_distant_police(self):
        near = score_components(PATH, cctv=[], convenience=[], police=[_near(35.16, 126.851)])
        far = score_components(PATH, cctv=[], convenience=[], police=[_near(35.40, 127.20)])
        assert near["police"] > far["police"]

    def test_shops_stand_in_for_people_being_around_at_night(self):
        empty = score_components(PATH, cctv=[], convenience=[], police=[])
        busy = score_components(PATH, cctv=[], convenience=[_near(35.15 + i * 0.002, 126.85) for i in range(8)],
                                police=[])
        assert busy["nightlife"] > empty["nightlife"]

    def test_every_component_stays_within_zero_and_one(self):
        crowded = score_components(PATH, cctv=[_near(35.155, 126.85)] * 200,
                                   convenience=[_near(35.155, 126.85)] * 200,
                                   police=[_near(35.155, 126.85)])
        assert all(0.0 <= v <= 1.0 for v in crowded.values())


class TestIndex:
    def test_index_is_between_zero_and_one(self):
        assert 0.0 <= safety_index(PATH, cctv=[], convenience=[], police=[]) <= 1.0

    def test_well_covered_route_scores_higher_than_bare_one(self):
        bare = safety_index(PATH, cctv=[], convenience=[], police=[])
        covered = safety_index(
            PATH,
            cctv=[_near(35.15 + i * 0.002, 126.85) for i in range(10)],
            convenience=[_near(35.15 + i * 0.003, 126.85) for i in range(6)],
            police=[_near(35.16, 126.851)],
        )
        assert covered > bare

    def test_features_far_from_the_route_do_not_count(self):
        """경로에서 2km 떨어진 CCTV는 그 코스의 안전과 무관하다."""
        far = safety_index(PATH, cctv=[_near(35.30, 127.10)] * 20, convenience=[], police=[])
        none = safety_index(PATH, cctv=[], convenience=[], police=[])
        assert far == pytest.approx(none)
