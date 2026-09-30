"""코스가 선호 거리보다 길면, 실제로 그만큼만 달리도록 경로(+턴바이턴 안내)를 잘라서 돌려준다.

사용자가 "5km 달리고 싶다"고 했는데 6km/8km짜리 코스를 그대로 보여주면 의미가 없다.
길은 중간에 아무 데서나 돌아서면 그만이므로, score.py의 distance_match는 코스가
선호 거리 이상이면 항상 만점을 주고, 실제 응답에는 여기서 목표 거리만큼 잘라낸 경로를 담는다.
"""
from ..data_collection.enrich import haversine_m
from .step_position import assign_positions


def _cumulative_distances(path: list) -> list:
    cum = [0.0]
    for i in range(1, len(path)):
        cum.append(cum[-1] + haversine_m(path[i - 1], path[i]))
    return cum


def _nearest_cum_distance(point: list, path: list, cum: list) -> float:
    best_i, best_d = 0, float("inf")
    for i, p in enumerate(path):
        d = haversine_m(point, p)
        if d < best_d:
            best_i, best_d = i, d
    return cum[best_i]


def _cut_at(path: list, cum: list, target_m: float) -> tuple:
    """경로를 target_m 지점에서 자른다. (잘린 경로, 실제 잘린 거리) 반환."""
    cut_path = [path[0]]
    cut_m = cum[-1]
    for i in range(1, len(path)):
        if cum[i] >= target_m:
            seg_m = cum[i] - cum[i - 1]
            t = (target_m - cum[i - 1]) / seg_m if seg_m > 0 else 0
            lat = path[i - 1][0] + (path[i][0] - path[i - 1][0]) * t
            lng = path[i - 1][1] + (path[i][1] - path[i - 1][1]) * t
            cut_path.append([lat, lng])
            cut_m = target_m
            break
        cut_path.append(path[i])
    return cut_path, cut_m


def _truncate_roundtrip(course: dict, target_km: float) -> dict:
    """왕복은 뒤를 잘라내면 안 된다 — 반환점을 앞으로 당겨야 출발점으로 돌아온다.

    그냥 목표 거리에서 끊으면 반환점 근처에서 멈추게 돼, 3km 왕복을 요청한 사람이
    집에서 2.4km 떨어진 곳에 남겨진다. 그래서 가는 길을 목표의 절반에서 접고,
    거기서 가장 가까운 복귀 경로 지점부터 이어 붙여 실제로 돌아오게 만든다.
    """
    path = course["path"]
    cum = _cumulative_distances(path)
    outbound_end_m = course["cum_outbound_m"]
    half_m = target_km * 1000 / 2

    outbound_path, out_cut_m = _cut_at(path, cum, min(half_m, outbound_end_m))
    turn_point = outbound_path[-1]

    # 복귀 구간(반환점 이후)에서 되돌아 나갈 지점을 찾는다
    return_start = next((i for i, c in enumerate(cum) if c >= outbound_end_m), len(path) - 1)
    best_i, best_d = return_start, float("inf")
    for i in range(return_start, len(path)):
        d = haversine_m(turn_point, path[i])
        if d < best_d:
            best_i, best_d = i, d

    new_path = outbound_path + path[best_i:]
    return_m = cum[-1] - cum[best_i]
    total_m = out_cut_m + return_m

    original_km = course.get("distance_km")
    trimmed = dict(course)
    trimmed["path"] = new_path
    trimmed["distance_km"] = round(total_m / 1000, 2)
    trimmed["cum_outbound_m"] = out_cut_m
    trimmed["elevation_gain_m"] = round(
        course.get("elevation_gain_m", 0) * (total_m / 1000) / original_km, 1) if original_km else 0
    trimmed["trimmed"] = True
    trimmed["original_distance_km"] = original_km

    steps = course.get("steps")
    if steps:
        kept = [s for s in steps
                if s.get("cum_m", 0) <= out_cut_m or s.get("cum_m", 0) >= cum[best_i]]
        trimmed["steps"] = assign_positions(kept, new_path)
    return trimmed


def truncate_course(course: dict, target_km: float) -> dict:
    """course.distance_km이 target_km보다 길면 경로/안내를 잘라 새 dict를 반환. 아니면 원본 그대로."""
    path = course.get("path") or []
    if not path or course.get("distance_km", 0) <= target_km:
        return course

    if course.get("route_type") == "roundtrip" and course.get("cum_outbound_m"):
        return _truncate_roundtrip(course, target_km)

    target_m = target_km * 1000
    cum = _cumulative_distances(path)

    trimmed_path = [path[0]]
    cut_m = cum[-1]
    for i in range(1, len(path)):
        if cum[i] >= target_m:
            seg_m = cum[i] - cum[i - 1]
            t = (target_m - cum[i - 1]) / seg_m if seg_m > 0 else 0
            lat = path[i - 1][0] + (path[i][0] - path[i - 1][0]) * t
            lng = path[i - 1][1] + (path[i][1] - path[i - 1][1]) * t
            trimmed_path.append([lat, lng])
            cut_m = target_m
            break
        trimmed_path.append(path[i])

    original_km = course.get("distance_km")
    original_elev = course.get("elevation_gain_m", 0)
    fraction = (cut_m / 1000) / original_km if original_km else 1.0

    trimmed = dict(course)
    trimmed["path"] = trimmed_path
    trimmed["distance_km"] = round(cut_m / 1000, 2)
    trimmed["elevation_gain_m"] = round(original_elev * fraction, 1)  # 구간비례 근사치
    trimmed["trimmed"] = True
    trimmed["original_distance_km"] = original_km

    steps = course.get("steps")
    if steps:
        # 왕복은 갈 때와 올 때 좌표가 같아서 "가장 가까운 점"으로 다시 찾으면 복귀 안내가
        # 갈 때 구간으로 끌려간다. 코스를 만들 때 정해둔 위치(cum_m)가 있으면 그걸 쓴다.
        trimmed["steps"] = [
            s for s in steps
            if s.get("cum_m", _nearest_cum_distance([s["lat"], s["lng"]], path, cum)) <= cut_m + 1
        ]

    return trimmed
