from src.recommend.location import distance_to_course_km, filter_nearby

COURSE_NEAR = {"id": "near", "path": [[34.7393, 127.7359], [34.7450, 127.7670]]}
COURSE_FAR = {"id": "far", "path": [[35.1565, 126.8386]]}  # 광주, 여수에서 아주 멀다


def test_distance_to_course_km_uses_nearest_path_point():
    d = distance_to_course_km(34.7393, 127.7359, COURSE_NEAR)
    assert d < 0.1  # 시작점과 거의 동일 좌표


def test_distance_to_course_km_no_path_is_infinite():
    assert distance_to_course_km(0, 0, {"id": "x", "path": []}) == float("inf")


def test_filter_nearby_excludes_far_courses():
    nearby = filter_nearby([COURSE_NEAR, COURSE_FAR], 34.7393, 127.7359, max_distance_km=5)
    ids = [c["id"] for c in nearby]
    assert "near" in ids
    assert "far" not in ids
