"""사용자 현위치 기반 실시간 코스 생성 (프로토타입).

DB에 미리 저장된 코스가 아니라, 요청이 들어온 그 순간 사용자 좌표 주변에서
실제 도로/지형 데이터를 조회해 코스를 즉석에서 만든다.

방식: 목표 둘레로 가상의 다각형 꼭짓점을 만들고, 각 꼭짓점을 OSM의 공원·보행로로
스냅한 뒤 Tmap passList에 넣는다. route_type="roundtrip"이면 출발점과 도착점을
같게 하여 경유지를 순서대로 통과하는 순환 코스를 만든다.

한계 (검증 완료 사항):
- 요청 1건당 Overpass + Tmap + 고도 API를 순차 호출하므로 응답에 수 초 소요.
"""
import math
import logging

from ..api_clients.elevation import get_elevation_profile
from ..api_clients.tmap_pedestrian import extract_path, extract_steps, get_route
from ..data_collection.enrich import haversine_m
from ..data_collection.osm_overpass import OverpassUnavailable, post_overpass_query
from .step_position import assign_positions, cumulative_distances

TAG_TO_OSM_FILTER = {
    "바다뷰": '["natural"="coastline"]',
    "숲길": '["natural"="wood"]',
}
SNAP_RADIUS_M = 200
VERTEX_COUNT = 3
RETRY_ANGLE_OFFSETS_DEG = (0.0, 20.0, -20.0)
LOOP_RADIUS_SCALES = (0.85, 0.70, 0.55)
DISTANCE_MATCH_TOLERANCE_RATIO = 0.05
MAX_DISTANCE_REFINEMENTS = 2
MIN_LOOP_RADIUS_SCALE = 0.25
MAX_LOOP_RADIUS_SCALE = 1.25
logger = logging.getLogger(__name__)


def virtual_vertices(lat: float, lng: float, target_distance_km: float,
                     count: int = VERTEX_COUNT, angle_offset_deg: float = 0.0,
                     radius_scale: float = 1.0) -> list[tuple]:
    """목표 거리에서 도로 우회분을 고려한 다각형 꼭짓점을 계산한다."""
    radius_m = target_distance_km * radius_scale * 1000 / (2 * math.pi)
    lat_radius = radius_m / 111_320
    lng_radius = radius_m / (111_320 * math.cos(math.radians(lat)))
    return [
        (
            lat + lat_radius * math.sin(math.radians(angle_offset_deg + i * 360 / count)),
            lng + lng_radius * math.cos(math.radians(angle_offset_deg + i * 360 / count)),
        )
        for i in range(count)
    ]


def snap_to_runnable_point(lat: float, lng: float) -> tuple | None:
    """가상 꼭짓점을 반경 내 보행 가능 도로의 중심선에 스냅한다."""
    query = f"""
    [out:json][timeout:15];
    way(around:{SNAP_RADIUS_M},{lat},{lng})["highway"~"footway|path|pedestrian|living_street|residential|service|cycleway|track|unclassified|tertiary"];
    out geom;
    """
    resp = post_overpass_query(query, timeout=20)

    best_point = None
    best_distance_m = float("inf")
    for element in resp.json().get("elements", []):
        geometry = element.get("geometry", [])
        for start, end in zip(geometry, geometry[1:]):
            point, distance_m = _project_onto_segment(
                (lat, lng), (start["lat"], start["lon"]), (end["lat"], end["lon"])
            )
            if distance_m < best_distance_m:
                best_point = point
                best_distance_m = distance_m

    return best_point if best_distance_m <= SNAP_RADIUS_M else None


def _project_onto_segment(origin: tuple, start: tuple, end: tuple) -> tuple[tuple, float]:
    """Return closest (lat, lng) on a segment and distance in meters."""
    lat_scale = 111_320
    lng_scale = 111_320 * math.cos(math.radians(origin[0]))

    def to_xy(point: tuple) -> tuple[float, float]:
        return ((point[1] - origin[1]) * lng_scale, (point[0] - origin[0]) * lat_scale)

    start_xy = to_xy(start)
    end_xy = to_xy(end)
    dx = end_xy[0] - start_xy[0]
    dy = end_xy[1] - start_xy[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        fraction = 0.0
    else:
        fraction = max(0.0, min(1.0, -(start_xy[0] * dx + start_xy[1] * dy) / length_sq))

    projected_xy = (start_xy[0] + fraction * dx, start_xy[1] + fraction * dy)
    projected = (
        origin[0] + projected_xy[1] / lat_scale,
        origin[1] + projected_xy[0] / lng_scale,
    )
    return projected, math.hypot(*projected_xy)


def snap_vertices(vertices: list[tuple]) -> list[tuple]:
    snapped_vertices = []
    for index, (lat, lng) in enumerate(vertices):
        try:
            snapped = snap_to_runnable_point(lat, lng)
        except OverpassUnavailable as error:
            logger.debug("OSM 스냅을 건너뛰고 계산 좌표를 사용합니다: %s", error)
            return snapped_vertices + vertices[index:]
        snapped_vertices.append(snapped or vertices[index])
    return snapped_vertices


def _orientation(a: tuple, b: tuple, c: tuple) -> float:
    return (b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])


def _segments_intersect(a: tuple, b: tuple, c: tuple, d: tuple) -> bool:
    ab_c = _orientation(a, b, c)
    ab_d = _orientation(a, b, d)
    cd_a = _orientation(c, d, a)
    cd_b = _orientation(c, d, b)
    return (ab_c * ab_d < 0 and cd_a * cd_b < 0)


def has_self_intersection(path: list[list[float]]) -> bool:
    """인접 선분과 닫힌 고리의 시작·끝 선분은 제외하고 교차 여부를 검사한다."""
    if len(path) < 4:
        return False
    points = [tuple(point) for point in path]
    segment_count = len(points) - 1
    for first in range(segment_count):
        for second in range(first + 1, segment_count):
            if second - first <= 1:
                continue
            if first == 0 and second == segment_count - 1:
                continue
            if _segments_intersect(points[first], points[first + 1], points[second], points[second + 1]):
                return True
    return False


def _sample_polyline(path: list[list[float]], spacing_m: float = 25.0) -> list[tuple[tuple, float]]:
    cumulative = cumulative_distances(path)
    samples = []
    for index, (start, end) in enumerate(zip(path, path[1:])):
        segment_m = cumulative[index + 1] - cumulative[index]
        divisions = max(1, math.ceil(segment_m / spacing_m))
        for step in range(divisions):
            fraction = step / divisions
            point = (
                start[0] + (end[0] - start[0]) * fraction,
                start[1] + (end[1] - start[1]) * fraction,
            )
            samples.append((point, cumulative[index] + fraction * segment_m))
    if path:
        samples.append((tuple(path[-1]), cumulative[-1]))
    return samples


def route_overlap_ratio(path: list[list[float]], threshold_m: float = 20.0,
                        adjacent_exclusion_m: float = 40.0) -> float:
    """Measure repeated route length against all non-local portions of the closed path."""
    if len(path) < 4:
        return 0.0
    cumulative = cumulative_distances(path)
    total_m = cumulative[-1]
    if total_m <= 0:
        return 0.0

    segments = [
        (tuple(start), tuple(end), cumulative[index], cumulative[index + 1])
        for index, (start, end) in enumerate(zip(path, path[1:]))
    ]
    samples = _sample_polyline(path)
    repeated = 0

    for point, position_m in samples:
        for start, end, segment_start_m, segment_end_m in segments:
            if segment_start_m <= position_m <= segment_end_m:
                along_gap_m = 0.0
            elif position_m < segment_start_m:
                along_gap_m = segment_start_m - position_m
            else:
                along_gap_m = position_m - segment_end_m

            # The path is closed: its first and last segments are neighbors too.
            along_gap_m = min(along_gap_m, total_m - along_gap_m)
            if along_gap_m <= adjacent_exclusion_m:
                continue

            distance_m = _project_onto_segment(point, start, end)[1]
            if distance_m <= threshold_m:
                repeated += 1
                break

    return repeated / len(samples) if samples else 0.0


def find_nearby_points(lat: float, lng: float, radius_m: float, osm_filter: str, limit: int = 3) -> list:
    """기존 호출부 호환용으로 주변 지형의 대표 좌표를 반환한다."""
    query = f"""
    [out:json][timeout:25];
    (way(around:{radius_m},{lat},{lng}){osm_filter};);
    out geom 3;
    """
    resp = requests.post(OVERPASS_URL, data={"data": query}, headers=OSM_HEADERS, timeout=30)
    resp.raise_for_status()
    elements = resp.json().get("elements", [])
    points = []
    for element in elements:
        geometry = element.get("geometry", [])
        if not geometry:
            continue
        for point in (geometry[0], geometry[len(geometry) // 2], geometry[-1]):
            candidate = (point["lat"], point["lon"])
            if candidate not in points:
                points.append(candidate)

    if not points:
        return []

    # 출발점에서 먼 순서로 배치한다.
    points.sort(key=lambda point: (point[0] - lat) ** 2 + (point[1] - lng) ** 2, reverse=True)
    return points[:limit]


def find_nearby_point(lat: float, lng: float, radius_m: float, osm_filter: str) -> tuple:
    """기존 호출부 호환용으로 가장 먼 후보 하나를 반환한다."""
    points = find_nearby_points(lat, lng, radius_m, osm_filter, limit=1)
    return points[0] if points else None


def sample_elevation_gain(path: list, n: int = 20) -> float:
    if len(path) < 2:
        return 0.0
    step = max(1, len(path) // n)
    samples = path[::step]
    elevations = get_elevation_profile(samples)
    return round(sum(max(0.0, elevations[i] - elevations[i - 1]) for i in range(1, len(elevations))), 1)


def generate_loop_course(lat: float, lng: float, target_distance_km: float, tags: set,
                         route_type: str = "roundtrip", angle_offset_deg: float | None = None,
                         snap_to_osm: bool = True, radius_scale: float = 1.0) -> dict:
    """Generate one Tmap course candidate for a waypoint polygon orientation."""
    matched_tag = next((tag for tag in TAG_TO_OSM_FILTER if tag in tags), None)
    offsets = (angle_offset_deg,) if angle_offset_deg is not None else RETRY_ANGLE_OFFSETS_DEG

    for offset in offsets:
        if route_type == "roundtrip":
            vertices = virtual_vertices(
                lat, lng, target_distance_km,
                angle_offset_deg=offset,
                radius_scale=radius_scale,
            )
            waypoints = snap_vertices(vertices) if snap_to_osm else vertices
            route = get_route(
                (lng, lat), (lng, lat),
                start_name="출발지", end_name="출발지",
                waypoints=[(point[1], point[0]) for point in waypoints],
            )
        else:
            vertices = virtual_vertices(lat, lng, target_distance_km, count=1)
            waypoint = globals()["snap_vertices"](vertices)[0]
            route = get_route((lng, lat), (waypoint[1], waypoint[0]))

        full_path, distance_m = extract_path(route)
        if not full_path:
            continue
        if route_type == "roundtrip" and has_self_intersection(full_path):
            continue
        break
    else:
        if route_type == "roundtrip":
            raise ValueError("Tmap 보행자 순환 경로를 찾지 못했습니다.")
        raise ValueError("Tmap 보행자 경로를 찾지 못했습니다.")

    steps = assign_positions(extract_steps(route), full_path)
    offset_suffix = f"-a{offset:g}-r{radius_scale:g}" if route_type == "roundtrip" else ""
    name_suffix = f", {offset:g}°, 반경 {radius_scale:.0%}" if route_type == "roundtrip" else ""
    course_tags = [matched_tag] if matched_tag else []
    name = f"현위치 기반 {'왕복' if route_type == 'roundtrip' else '편도'} 코스 ({matched_tag or '기본'}{name_suffix})"

    return {
        "id": f"generated-{lat:.4f}-{lng:.4f}-{target_distance_km}-{route_type}{offset_suffix}",
        "name": name,
        "region": "실시간 생성",
        "distance_km": round(distance_m / 1000, 2),
        "radius_scale": radius_scale,
        "elevation_gain_m": sample_elevation_gain(full_path),
        "surface": "paved",
        "safety_score": 0.7,
        "path": full_path,
        "route_overlap_ratio": route_overlap_ratio(full_path) if route_type == "roundtrip" else 0.0,
        "steps": steps,
        "route_type": route_type,
        "tags": course_tags,
        "source": "live_generated",
    }


def generate_loop_candidates(lat: float, lng: float, target_distance_km: float, tags: set,
                             route_type: str = "roundtrip") -> list[dict]:
    """Generate loop variants and adjust each radius using the actual Tmap route distance."""
    if route_type != "roundtrip":
        return [generate_loop_course(lat, lng, target_distance_km, tags, route_type)]

    candidates = []
    errors = []
    for index, (angle_offset, radius_scale) in enumerate(zip(RETRY_ANGLE_OFFSETS_DEG, LOOP_RADIUS_SCALES)):
        try:
            candidate = generate_loop_course(
                lat, lng, target_distance_km, tags,
                route_type=route_type,
                angle_offset_deg=angle_offset,
                snap_to_osm=index == 0,
                radius_scale=radius_scale,
            )
        except ValueError as error:
            errors.append(str(error))
            continue

        best_candidate = candidate
        best_error = abs(candidate["distance_km"] - target_distance_km)
        current_candidate = candidate
        for _ in range(MAX_DISTANCE_REFINEMENTS):
            actual_distance = current_candidate["distance_km"]
            if actual_distance <= 0 or best_error <= target_distance_km * DISTANCE_MATCH_TOLERANCE_RATIO:
                break

            corrected_scale = current_candidate["radius_scale"] * target_distance_km / actual_distance
            corrected_scale = max(MIN_LOOP_RADIUS_SCALE, min(MAX_LOOP_RADIUS_SCALE, corrected_scale))
            if abs(corrected_scale - current_candidate["radius_scale"]) < 0.02:
                break
            try:
                refined = generate_loop_course(
                    lat, lng, target_distance_km, tags,
                    route_type=route_type,
                    angle_offset_deg=angle_offset,
                    snap_to_osm=False,
                    radius_scale=corrected_scale,
                )
            except ValueError:
                break

            refined_error = abs(refined["distance_km"] - target_distance_km)
            if refined_error < best_error:
                best_candidate = refined
                best_error = refined_error
            current_candidate = refined

        candidate = best_candidate
        candidates.append(candidate)

    if not candidates:
        raise ValueError(errors[-1] if errors else "Tmap 보행자 순환 경로를 찾지 못했습니다.")
    return candidates
