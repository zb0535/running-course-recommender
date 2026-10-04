"""코스가 정말 인도로 가는지 Tmap 보행 네트워크로 확인한다.

달리는 사람은 차도가 아니라 인도로 가야 한다. 그런데 OSM 한국 데이터에는 인도 유무가 거의 적혀
있지 않다(전국에서 약 9천 개 길). Tmap 보행자 경로는 구간마다 길의 종류를 준다:

    roadType 21  차도와 인도가 분리된 길 (인도)
    roadType 23  차가 다닐 수 없는 보행자 도로
    roadType 22  차도와 인도가 분리되지 않은 길 (차와 섞여 달린다)
    roadType 24  쾌적하지 않은 길
    facilityType 15 횡단보도, 12 육교, 14 지하보도

그래서 직접 짠 경로에 인도가 있는지 모르는 큰길이 있으면 Tmap에 그 경로를 따라가 보게 해서 구간별
종류를 얻고, 밑에 깔린 길마다 "인도 있음/없음"을 적어 둔다(sidewalk_facts). 인도가 없거나 확인되지 않은
큰길은 도로망에서 빼고 다시 짠다. 한 번 확인한 길은 다음 요청부터 묻지 않고 안다.
"""
import contextlib
import json
import os
import sqlite3
from collections import defaultdict

import requests

from ..api_clients.tmap_pedestrian import extract_path, extract_steps, get_route
from ..data_collection.enrich import haversine_m

FACTS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "sidewalk_facts.sqlite")
# 저장소에 들어 있는 확인 기록. 처음 실행할 때 이걸로 시작해서, 팀원 모두 같은 길을 이미 아는 상태가 된다
# (안 그러면 사람마다 처음 몇 번은 Tmap에 다시 묻고, 그동안 경로가 서로 다르게 나온다).
SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "sidewalk_facts.seed.json")

SEPARATED, CAR_FREE, MIXED, UNPLEASANT = 21, 23, 22, 24
CROSSINGS = ("12", "14", "15")
MAX_PASS_POINTS = 5          # Tmap passList 한도
SAFE = ("sidewalk", "car_free", "crossing")   # 인도, 차 없는 길, 횡단시설
KINDS = SAFE + ("alley", "car_lane", "unknown")
# 인도 구분이 없는 구간(Tmap roadType 22)은 밑에 깔린 길의 등급으로 둘로 나눈다:
#   alley    골목·이면도로 — 불가피하면 지나가되 최소한으로 (이 비율 이하가 될 때까지 다시 짠다)
#   car_lane 인도 없는 차도 — 조금도 허용하지 않는다. 이런 구간이 있는 코스는 내보내지 않는다
ALLEY_TARGET = 0.10
UNKNOWN_ALLOWED = 0.03   # 종류를 알 수 없는 짧은 연결 구간만 이만큼까지 봐준다
MATCH_M = 15                 # Tmap 구간과 이만큼 안에 있는 길을 같은 길로 본다


def kind_of(properties: dict) -> str:
    """Tmap 구간 하나의 종류: sidewalk / car_free / crossing / mixed / unknown."""
    if str(properties.get("facilityType")) in CROSSINGS:
        return "crossing"
    road_type = properties.get("roadType")
    if road_type == SEPARATED:
        return "sidewalk"
    if road_type == CAR_FREE:
        return "car_free"
    if road_type in (MIXED, UNPLEASANT):
        return "alley"   # 도로망으로 확인하기 전의 가정. resolve가 차도면 car_lane으로 바꾼다
    return "unknown"


def segments(route_json: dict) -> list:
    """[(종류, [(lat, lng), ...], 길이 m)] — 경로 순서대로."""
    result = []
    for feature in route_json.get("features", []):
        if feature["geometry"]["type"] != "LineString":
            continue
        points = [(lat, lng) for lng, lat in feature["geometry"]["coordinates"]]
        length = sum(haversine_m(a, b) for a, b in zip(points, points[1:]))
        result.append((kind_of(feature.get("properties", {})), points, length))
    return result


def mix(route_json: dict) -> dict:
    """길이 기준 비율 {sidewalk, car_free, crossing, alley, car_lane, unknown}."""
    totals = dict.fromkeys(KINDS, 0.0)
    for kind, _, length in segments(route_json):
        totals[kind] += length
    total = sum(totals.values())
    return {kind: round(length / total, 3) for kind, length in totals.items()} if total else totals


def on_sidewalk(road_mix: dict) -> float:
    """인도·보행 전용길·횡단시설로 가는 비율."""
    return round(road_mix["sidewalk"] + road_mix["car_free"] + road_mix["crossing"], 3)


def pass_points(path: list, closed: bool) -> list:
    """경로를 따라 고르게 찍은 경유지 (최대 5개). Tmap이 같은 길을 따라오게 하려는 것이다."""
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + haversine_m(a, b))
    total = cum[-1]
    if total <= 0:
        return []
    count = MAX_PASS_POINTS
    picks, i = [], 0
    for k in range(1, count + 1):
        target = total * k / (count + 1)
        while i < len(cum) - 1 and cum[i] < target:
            i += 1
        picks.append(tuple(path[i]))
    return picks


# ── 확인한 사실을 쌓아 둔다 ──

@contextlib.contextmanager
def _open():
    """연결을 열어 쓰고, 저장한 뒤 반드시 닫는다 (sqlite3의 with는 닫지 않아서 파일이 잠긴 채 남는다)."""
    db = _connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(FACTS_PATH) or ".", exist_ok=True)
    fresh = not os.path.exists(FACTS_PATH)
    db = sqlite3.connect(FACTS_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS facts (way TEXT PRIMARY KEY, sidewalk INTEGER NOT NULL)")
    if fresh and os.path.exists(SEED_PATH):
        with open(SEED_PATH, encoding="utf-8") as f:
            seed = json.load(f)
        db.executemany("INSERT OR IGNORE INTO facts VALUES (?, ?)", [(k, int(v)) for k, v in seed.items()])
        db.commit()
    return db


def way_key(points: list) -> str:
    """길을 양 끝 좌표로 식별한다 (지형 DB를 다시 만들어도 같은 길은 같은 키)."""
    a, b = points[0], points[-1]
    return f"{a[0]:.6f},{a[1]:.6f},{b[0]:.6f},{b[1]:.6f}"


def export_seed() -> int:
    """지금까지 쌓인 확인 기록을 씨앗 파일로 내보낸다 (저장소에 올려 팀과 맞출 때)."""
    facts = load_facts()
    with open(SEED_PATH, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(facts.items())), f, ensure_ascii=False, indent=0)
    return len(facts)


def load_facts() -> dict:
    """{길 키: True(인도 있음) / False(없음)}."""
    if not os.path.exists(FACTS_PATH) and not os.path.exists(SEED_PATH):
        return {}
    with _open() as db:
        return {key: bool(value) for key, value in db.execute("SELECT way, sidewalk FROM facts")}


def save_facts(facts: dict) -> None:
    if not facts:
        return
    with _open() as db:
        db.executemany("INSERT OR REPLACE INTO facts VALUES (?, ?)", [(k, int(v)) for k, v in facts.items()])


# ── 직접 짠 경로를 확인하고, 인도가 아니면 피해서 다시 짠다 ──

SAMPLE_M = 20
MIN_VOTES = 3     # 길 하나에 대해 이만큼의 지점이 같은 말을 해야 사실로 적는다 (교차로에서 옆 길을 잘못 적지 않게)
ROUNDS = 4
BLOCKED_UNVERIFIED = "mixed"   # road_graph.BLOCKED와 같은 값: 탐색이 지나가지 않는다


def _samples(points: list):
    """꺾은선을 SAMPLE_M 간격으로 찍은 지점들."""
    for a, b in zip(points, points[1:]):
        steps = max(1, int(haversine_m(a, b) // SAMPLE_M))
        for i in range(steps):
            f = i / steps
            yield a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
    if points:
        yield points[-1]


def resolve(route_json: dict, graph) -> list:
    """Tmap이 종류를 주지 않은 구간(강변 산책로·공원 길에 많다)을 도로망으로 채운다.

    그 자리에 보행 전용길이 있으면 car_free로 본다. 그래도 모르면 unknown으로 남기고 인도로 치지 않는다.
    인도 구분이 없는 구간은 밑에 깔린 길이 골목인지 차도인지 본다 — 골목이 하나도 없고 차도만 있으면 car_lane.
    """
    from .road_graph import is_alley

    resolved = []
    for kind, points, length in segments(route_json):
        if graph is not None and len(points) >= 2 and kind in ("unknown", "alley"):
            samples = list(_samples(points))
            near = [graph.ways_near(lat, lng, MATCH_M) for lat, lng in samples]
            if kind == "unknown":
                on_walkway = sum(1 for ways in near if any(graph.surfaces[way] in ("walkway", "steps") for way in ways))
                if on_walkway >= len(samples) * 0.7:
                    kind = "car_free"
            else:
                on_road_only = sum(1 for ways in near
                                   if ways and not any(is_alley(graph.ways[way][0]) or graph.surfaces[way] == "walkway"
                                                       for way in ways))
                if on_road_only >= len(samples) * 0.7:
                    kind = "car_lane"
        resolved.append((kind, points, length))
    return resolved


def mix_of(typed: list) -> dict:
    totals = dict.fromkeys(KINDS, 0.0)
    for kind, _, length in typed:
        totals[kind] += length
    total = sum(totals.values())
    return {kind: round(length / total, 3) for kind, length in totals.items()} if total else totals


def learn(graph, typed: list) -> dict:
    """확인한 구간 밑에 깔린 길들에 '인도 있음/없음'을 적는다. 도로망에도 바로 반영한다."""
    from .road_graph import BLOCKED, is_alley

    votes = defaultdict(lambda: [0, 0])   # 길 번호 -> [인도 있음, 없음]
    for kind, points, _ in typed:
        if kind not in ("sidewalk", "car_free", "alley", "car_lane"):
            continue
        for lat, lng in _samples(points):
            for way in graph.ways_near(lat, lng, MATCH_M):
                if graph.surfaces[way] not in (None, "walkway", "steps"):
                    # 골목 구간 옆을 지나는 차도까지 '인도 없음'으로 적지 않는다 (종류가 맞는 길에만 적는다)
                    if kind == "alley" and not is_alley(graph.ways[way][0]):
                        continue
                    if kind == "car_lane" and is_alley(graph.ways[way][0]):
                        continue
                    votes[way][0 if kind in ("sidewalk", "car_free") else 1] += 1
    facts = {}
    for way, (good, bad) in votes.items():
        if max(good, bad) < MIN_VOTES or good == bad:
            continue
        has_sidewalk = good > bad
        facts[way_key(graph.ways[way][1])] = has_sidewalk
        graph.surfaces[way] = "sidewalk" if has_sidewalk else (
            "alley" if is_alley(graph.ways[way][0]) else BLOCKED)
    return facts


def _follow(path: list, start: tuple, end: tuple) -> dict:
    """직접 짠 경로를 따라가는 Tmap 경로 (경유지 5개 = passList 한도). 구간 종류를 얻으려는 것이다."""
    return get_route((start[1], start[0]), (end[1], end[0]), start_name="출발지", end_name="도착지",
                     waypoints=[(lng, lat) for lat, lng in pass_points(path, closed=start == end)])


# 도로망의 길 종류 -> 코스에 표시하는 종류
SHOWN_AS = {"walkway": "car_free", "steps": "car_free", "sidewalk": "sidewalk", "shared": "alley", "alley": "alley"}


def _unverified_roads(graph, route: dict) -> list:
    """경로가 지나는 길 중 인도가 있는지 아직 모르는 차도(간선도로)."""
    return [way for way in route["way_lengths"] if graph.surfaces[way] in ("carroad", "mixed")]


def _deliver(graph, route: dict, start: tuple) -> dict:
    totals = dict.fromkeys(KINDS, 0.0)
    for way, length in route["way_lengths"].items():
        totals[SHOWN_AS[graph.surfaces[way]]] += length
    total = sum(totals.values()) or 1.0
    road_mix = {kind: round(length / total, 3) for kind, length in totals.items()}
    path, _ = extract_path(route)
    access_m = round(haversine_m(start, path[0])) if path else 0
    return dict(route, road_mix=road_mix, no_car_lanes=True, sidewalk_only=road_mix["alley"] == 0,
                access_m=access_m if access_m >= 50 else 0)  # 길 건너편 정도의 차이는 알리지 않는다


def confirm(graph, start: tuple, end: tuple, waypoints: list, local_route: dict, plan) -> dict:
    """직접 짠 경로가 인도 없는 차도를 지나지 않게 한다. 좌표는 (lat, lng).

    차가 다니는 큰길(간선도로)은 인도가 확인된 구간만 쓴다. 경로에 아직 모르는 큰길이 있으면 Tmap 보행
    네트워크에 물어 확인하고(그 결과는 쌓인다), 인도가 없거나 끝내 확인되지 않은 큰길은 도로망에서 빼고
    다시 짠다(plan). 경로 자체는 직접 짠 것을 그대로 내보낸다 — Tmap 경로로 바꾸면 경유지 사이를 최단
    거리로 이어 강변 같은 구간을 벗어난다(여의도 강변 순환에서 강변 41% → 11%).

    골목·이면도로는 간선도로가 아니므로 지나갈 수 있지만 길게 쳐서 불가피할 때만 쓴다.
    인도 없는 차도를 피하는 경로가 끝내 없으면 no_car_lanes=False를 준다 — 호출 쪽은 내보내지 않는다.
    """
    for _ in range(ROUNDS):
        unknown = _unverified_roads(graph, local_route)
        if not unknown:
            return _deliver(graph, local_route, start)
        try:
            path, _ = extract_path(local_route)
            save_facts(learn(graph, resolve(_follow(path, start, end), graph)))
        except (RuntimeError, requests.RequestException):
            pass  # Tmap을 쓸 수 없다(키 없음·장애). 확인하지 못한 큰길은 아래에서 뺀다
        for way in unknown:
            if graph.surfaces[way] == "carroad":
                # 물어봤는데도 인도가 확인되지 않았다. 모르는 차도로는 보내지 않는다 (이 서버가 떠 있는 동안)
                graph.surfaces[way] = BLOCKED_UNVERIFIED
        replanned = plan()
        if replanned is None:
            break
        local_route = replanned
    if not _unverified_roads(graph, local_route):
        return _deliver(graph, local_route, start)
    return dict(local_route, no_car_lanes=False, sidewalk_only=False)
