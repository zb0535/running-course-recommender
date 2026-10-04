from pathlib import Path

import pytest

from src.recommend.graph_loop import graph_loop_course, load_engine
from src.recommend.graph_loop_engine import RouteFailure


MAP_PATH = Path(__file__).resolve().parents[1] / "data" / "graph" / "yongbong.graphml"


@pytest.fixture(scope="module")
def engine():
    return load_engine(str(MAP_PATH))


def test_yongbong_graph_loop_returns_closed_non_retracing_3km_course(engine):
    course = graph_loop_course(engine, 35.1765, 126.9110, 3.0)
    assert course["route_type"] == "loop"
    assert course["source"] == "osm_graph"
    assert course["graph_diagnostics"]["within_tolerance"] is True
    assert course["graph_diagnostics"]["reused_edge_count"] == 0
    assert course["path"][0] == course["path"][-1]
    assert 2.55 <= course["distance_km"] <= 3.45


def test_yongbong_graph_loop_does_not_recommend_known_10km_distance_mismatch(engine):
    with pytest.raises(RouteFailure, match="허용 오차"):
        graph_loop_course(engine, 35.1765, 126.9110, 10.0)


def test_expanded_candidate_search_recovers_g6_5km_tolerance_case(engine):
    """Stage 1A에서 G6 5km는 3.97km로 거리 불일치였다.

    16방위·7거리대 후보 탐색은 같은 출발점에서 허용오차 안의 비중복 Loop를 찾아야 한다.
    """
    course = graph_loop_course(engine, 35.17944116451052, 126.91105620665611, 5.0)
    assert course["graph_diagnostics"]["within_tolerance"] is True
    assert abs(course["distance_km"] - 5.0) / 5.0 <= 0.15
