"""받아 둔 도로망에서 경로를 직접 짠다. 최단 거리가 아니라 '달리기 좋은 길'을 따라간다.

Tmap 보행자 경로는 최단 거리를 준다. 러닝 코스로는 맞지 않는다 — 강변 산책로가 바로 옆에 있어도
한 블록 안쪽의 빠른 길로 잇는다. 여기서는 길마다 달리기 좋은 정도(0~1)를 매기고, 그 점수가 낮은
길은 실제보다 길게 쳐서 경로를 찾는다. 결과적으로 조금 돌아가더라도 좋은 길을 고른다.

길 점수는 학습한다(learn_roads.py): 사람들이 달리기·걷기·자전거 코스로 등록해 둔 길(OSM route
관계)을 정답으로, 길의 종류와 주변 지형에서 그런 길의 특징을 배운다. 학습된 모델 파일이 없으면
같은 특징에 손으로 정한 가중치를 쓴다.

안내(턴바이턴)도 여기서 만든다. 출력은 Tmap 응답과 같은 모양이라 뒤 단계가 그대로 쓴다.
"""
import heapq
import json
import math
import os
from collections import defaultdict

from ..data_collection.enrich import haversine_m
from ..data_collection.scenery import MEASURE_RADIUS

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "road_model.json")

ROAD_KINDS = ("footway", "path", "pedestrian", "cycleway", "living_street", "residential", "unclassified",
              "tertiary", "secondary", "primary", "service", "track", "steps")
SCENERY_NEAR = ("river", "coast", "lake", "park", "forest")
FEATURES = tuple(f"hw_{k}" for k in ROAD_KINDS) + tuple(f"near_{k}" for k in SCENERY_NEAR) + (
    "bridge", "tunnel", "lit", "named", "sidewalk")

# 학습된 모델이 없을 때 쓰는 값. 보행·자전거 전용길과 물가·녹지를 우대하고 차도·통로를 낮춘다
PRIOR = {
    "bias": -0.5,
    "weights": {"hw_footway": 0.8, "hw_path": 0.6, "hw_pedestrian": 0.8, "hw_cycleway": 1.6,
                "hw_living_street": 0.2, "hw_residential": 0.0, "hw_unclassified": -0.2, "hw_tertiary": -0.2,
                "hw_secondary": -0.6, "hw_primary": -0.9, "hw_service": -0.9, "hw_track": -0.3, "hw_steps": -1.2,
                "near_river": 1.2, "near_coast": 1.0, "near_lake": 0.8, "near_park": 0.7, "near_forest": 0.3,
                "bridge": 0.0, "tunnel": -0.8, "lit": 0.3, "named": 0.2, "sidewalk": 0.4},
}

# 밤에는 길의 종류보다 '사람이 있는 길인가'가 중요하다. 낮에 좋은 강변·공원·숲 산책로가 밤에는 인적이
# 드물고 어둡다. 가로등 데이터는 거의 없어서(전국 약 8천 개 길) 대신 이렇게 본다:
#   lively   상점이 바로 옆에 있거나, 인도가 있는 큰길이거나, 조명이 있다고 표시된 길
#   secluded 상점도 조명 표시도 없는 공원·숲·물가의 보행로
#   neutral  그 밖 (주택가 길 등)
NIGHT_PENALTY = {"lively": 1.0, "neutral": 1.3, "secluded": 2.5}
MAIN_ROADS = {"tertiary", "secondary", "primary"}
SHOP_NEAR_M = 60
DETOUR_FOR_BAD_ROAD = 2.0    # 점수 0인 길은 길이를 (1 + 이 값)배로 친다
REUSE_PENALTY = 4.0          # 이미 지나간 길을 다시 지나면 이만큼 더 길게 친다 (같은 길 되짚기 방지)
SNAP_M = 150
TURN_DEG = 35                # 이보다 크게 꺾이면 좌·우회전으로 안내한다

_model_cache = {}


def load_model(path: str = None) -> dict:
    path = path or MODEL_PATH
    if path not in _model_cache:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                _model_cache[path] = json.load(f)
        else:
            _model_cache[path] = PRIOR
    return _model_cache[path]


def way_features(tags: dict, points: list, indexes: dict) -> dict:
    """길 하나의 특징. points는 [(lat, lng), ...]. 주변 지형은 길의 가운데 지점으로 본다."""
    highway = tags.get("highway", "").replace("_link", "")
    if highway == "road":
        highway = "unclassified"
    features = {f"hw_{highway}": 1.0} if highway in ROAD_KINDS else {}
    lat, lng = points[len(points) // 2]
    for name in SCENERY_NEAR:
        index = indexes.get(name)
        if index is not None and index.size and index.near(lat, lng, max(MEASURE_RADIUS[name], 30)):
            features[f"near_{name}"] = 1.0
    if tags.get("bridge") == "yes":
        features["bridge"] = 1.0
    if tags.get("tunnel") in ("yes", "building_passage"):
        features["tunnel"] = 1.0
    if tags.get("lit") == "yes":
        features["lit"] = 1.0
    if tags.get("name"):
        features["named"] = 1.0
    if tags.get("sidewalk") in ("both", "left", "right", "separate", "yes"):
        features["sidewalk"] = 1.0
    return features


def score(features: dict, model: dict) -> float:
    """달리기 좋은 정도 0~1."""
    weights = model["weights"]
    z = model["bias"] + sum(weights.get(name, 0.0) * value for name, value in features.items())
    return 1 / (1 + math.exp(-z))


WALKWAYS = {"footway", "path", "pedestrian", "cycleway", "steps", "track"}   # 차가 다니지 않는 길
SHARED = {"residential", "living_street", "service", "unclassified", "road"}  # 인도 구분 없이 같이 쓰는 이면도로
HAS_SIDEWALK = ("both", "left", "right", "separate", "yes")
NO_SIDEWALK = ("no", "none")
# 달리는 사람은 차도가 아니라 인도로 간다. 길의 종류에 따라 길이를 이만큼 더 길게 쳐서,
# 보행 전용길이 있으면 그쪽으로 가고 인도가 확인되지 않은 큰길은 다른 길이 없을 때만 쓴다.
# shared·carroad는 인도가 있는지 아직 모르는 길이다. 일단 후보로 쓰되 불리하게 치고, 만든 경로는 반드시
# Tmap 보행 네트워크로 확인한다(sidewalk.py). 확인 결과 차와 섞여 달려야 하는 길(mixed)은 지나가지 않는다.
#   alley = 인도 구분이 없다고 확인된 골목·이면도로. 불가피할 때만 지나도록 크게 불리하게 친다.
#   mixed = 인도가 없다고 확인된 차도. 지나가지 않는다.
#   steps = 계단. 학습 데이터(공개 걷기 코스)에는 계단이 많아 점수가 높게 나오지만, 달리는 사람에게는
#           흐름이 끊기는 구간이다. 다른 길이 훨씬 멀 때만 지난다.
SURFACE_PENALTY = {"walkway": 1.0, "sidewalk": 1.0, "shared": 2.0, "carroad": 3.0, "alley": 4.0, "steps": 3.0}
BLOCKED = "mixed"


def is_alley(tags: dict) -> bool:
    """골목·이면도로인가 (주택가 길, 생활도로, 통로). 간선도로(3차선급 이상 분류)는 아니다."""
    return tags.get("highway") in SHARED


def surface(tags: dict) -> str | None:
    """달리는 사람 입장에서 이 길이 무엇인가. 지나갈 수 없으면 None.

    - walkway: 보행·자전거 전용길
    - sidewalk: 인도가 있다고 표시된 차도
    - shared: 인도 구분이 없는 이면도로·통로
    - carroad: 인도가 있는지 알 수 없는 큰길 (OSM 한국 데이터는 대부분의 큰길에 인도 정보가 없다)
    인도가 없다고 표시된 큰길, 보행 금지, 사유지는 지나가지 않는다.
    """
    if tags.get("foot") == "no" or tags.get("access") in ("private", "no"):
        return None
    highway = tags.get("highway", "").replace("_link", "")
    if highway == "steps":
        return "steps"
    if highway in WALKWAYS:
        return "walkway"
    if highway not in ROAD_KINDS and highway != "road":
        return None
    if tags.get("sidewalk") in HAS_SIDEWALK:
        return "sidewalk"
    if highway in SHARED:
        return "shared"
    return None if tags.get("sidewalk") in NO_SIDEWALK else "carroad"


def _walkable(tags: dict) -> bool:
    return surface(tags) is not None


class RoadGraph:
    """한 동네의 도로망. 꼭짓점이 곧 노드다(OSM에서 이어진 길은 좌표를 공유한다)."""

    def __init__(self, elements: list, indexes: dict, model: dict = None, facts: dict = None):
        from . import sidewalk
        facts = sidewalk.load_facts() if facts is None else facts
        self.indexes = indexes
        self.model = model or load_model()
        self.ways = []                       # (tags, points)
        self.surfaces = []                   # 길 번호 -> walkway / sidewalk / shared / carroad
        self.adjacent = defaultdict(list)    # 노드 -> [(이웃 노드, 길이 m, 길 번호)]
        self._scores = {}
        self._night = {}
        self._grid = defaultdict(list)
        for element in elements:
            tags = element.get("tags") or {}
            if element.get("type") != "way" or not _walkable(tags):
                continue
            points = [(g["lat"], g["lon"]) for g in element.get("geometry", [])]
            way = len(self.ways)
            self.ways.append((tags, points))
            kind = surface(tags)
            # 이전 요청들에서 Tmap으로 확인해 둔 사실이 OSM 태그보다 우선한다 (보행 전용길은 그대로 둔다)
            known = facts.get(sidewalk.way_key(points))
            if known is not None and kind not in ("walkway", "steps"):
                kind = "sidewalk" if known else ("alley" if is_alley(tags) else BLOCKED)
            self.surfaces.append(kind)
            for a, b in zip(points, points[1:]):
                if a == b:
                    continue
                length = haversine_m(a, b)
                self.adjacent[a].append((b, length, way))
                self.adjacent[b].append((a, length, way))
        for node in self.adjacent:
            self._grid[self._cell(node)].append(node)

    @staticmethod
    def _cell(node: tuple) -> tuple:
        return int(node[0] / 0.002), int(node[1] / 0.002)

    def way_score(self, way: int) -> float:
        """길 점수는 처음 필요할 때 계산해 둔다 — 탐색이 닿는 길만 계산하면 된다."""
        if way not in self._scores:
            tags, points = self.ways[way]
            self._scores[way] = score(way_features(tags, points, self.indexes), self.model)
        return self._scores[way]

    def night_kind(self, way: int) -> str:
        """밤에 이 길이 어떤 길인가: lively / neutral / secluded."""
        if way not in self._night:
            tags, points = self.ways[way]
            lat, lng = points[len(points) // 2]
            shops = self.indexes.get("shops")
            if (tags.get("lit") == "yes"
                    or (self.surfaces[way] == "sidewalk" and tags.get("highway", "").replace("_link", "") in MAIN_ROADS)
                    or (shops is not None and shops.size and shops.near(lat, lng, SHOP_NEAR_M))):
                kind = "lively"
            elif self.surfaces[way] in ("walkway", "steps") and any(
                    self.indexes.get(name) is not None and self.indexes[name].size
                    and self.indexes[name].near(lat, lng, max(MEASURE_RADIUS[name], 30))
                    for name in ("forest", "park", "river", "coast", "lake")):
                kind = "secluded"
            else:
                kind = "neutral"
            self._night[way] = kind
        return self._night[way]

    def ways_near(self, lat: float, lng: float, radius_m: float) -> set:
        """이 지점에서 radius_m 안을 지나는 길 번호들 (선분까지의 거리로 잰다)."""
        r, c = self._cell((lat, lng))
        k = math.cos(math.radians(lat)) * 111_320
        found = set()
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for node in self._grid.get((r + dr, c + dc), ()):
                    ax, ay = (node[1] - lng) * k, (node[0] - lat) * 111_320
                    for neighbour, _, way in self.adjacent[node]:
                        if way in found:
                            continue
                        bx, by = (neighbour[1] - lng) * k, (neighbour[0] - lat) * 111_320
                        dx, dy = bx - ax, by - ay
                        length = dx * dx + dy * dy
                        f = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
                        if math.hypot(ax + f * dx, ay + f * dy) <= radius_m:
                            found.add(way)
        return found

    def nearest_node(self, lat: float, lng: float, radius_m: float = SNAP_M):
        r, c = self._cell((lat, lng))
        best, best_d = None, radius_m
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for node in self._grid.get((r + dr, c + dc), ()):
                    d = haversine_m((lat, lng), node)
                    if d <= best_d:
                        best, best_d = node, d
        return best

    def shortest(self, start: tuple, goal: tuple, used: set = None, max_m: float = 30000, night: bool = False):
        """점수를 반영한 비용으로 가장 좋은 길. [(노드, 길 번호), ...] 또는 None.

        used에 든 구간(양방향)은 REUSE_PENALTY만큼 비싸게 쳐서, 순환 코스가 왔던 길로 돌아오지 않게 한다.
        night이면 인적 드문 길을 길게 쳐서 상가·큰길 쪽으로 간다.
        """
        used = used or set()
        best = {start: 0.0}
        came = {}
        heap = [(0.0, 0.0, start)]
        while heap:
            cost, length, node = heapq.heappop(heap)
            if node == goal:
                break
            if cost > best.get(node, float("inf")) or length > max_m:
                continue
            for neighbour, edge_m, way in self.adjacent[node]:
                if self.surfaces[way] == BLOCKED:
                    continue  # 인도가 없다고 확인된 길
                factor = (1 + DETOUR_FOR_BAD_ROAD * (1 - self.way_score(way))) * SURFACE_PENALTY[self.surfaces[way]]
                if night:
                    factor *= NIGHT_PENALTY[self.night_kind(way)]
                if (node, neighbour) in used or (neighbour, node) in used:
                    factor *= REUSE_PENALTY
                new_cost = cost + edge_m * factor
                if new_cost < best.get(neighbour, float("inf")):
                    best[neighbour] = new_cost
                    came[neighbour] = (node, way)
                    heapq.heappush(heap, (new_cost, length + edge_m, neighbour))
        if goal not in came and goal != start:
            return None
        hops = [(goal, None)]
        node = goal
        while node != start:
            node, way = came[node]
            hops.append((node, way))
        hops.reverse()
        # hops[i] = (노드, 그 노드에서 다음 노드로 가는 길 번호)
        return hops


def _bearing(a: tuple, b: tuple) -> float:
    return math.degrees(math.atan2((b[1] - a[1]) * math.cos(math.radians(a[0])), b[0] - a[0])) % 360


def _turn(before: float, after: float) -> float:
    """꺾이는 각도. 오른쪽이 +, 왼쪽이 -."""
    return (after - before + 540) % 360 - 180


def _particle(name: str) -> str:
    """'OO길을' / 'OO로를' — 받침에 따라 을·를을 고른다."""
    last = name[-1]
    if "가" <= last <= "힣":
        return "을" if (ord(last) - 0xAC00) % 28 else "를"
    return "을(를)"


def build_steps(graph: RoadGraph, hops: list) -> list:
    """경로를 따라가며 안내를 만든다. Tmap과 같은 turn_type을 쓴다(200 출발, 11 직진, 12 좌, 13 우, 201 도착).

    안내는 방향이 크게 바뀌는 곳에서만 낸다. 길 이름만 바뀌고 그대로 직진이면 내지 않는다 —
    안내가 많으면 달리면서 듣기 어렵다.
    """
    nodes = [node for node, _ in hops]
    if len(nodes) < 2:
        return []
    marks = [(0, 200, "출발")]
    for i in range(1, len(nodes) - 1):
        if len(graph.adjacent[nodes[i]]) < 3 and hops[i - 1][1] == hops[i][1]:
            continue  # 갈림길이 아니고 같은 길 위 — 굽은 길을 따라가는 것일 뿐이다
        angle = _turn(_bearing(nodes[i - 1], nodes[i]), _bearing(nodes[i], nodes[i + 1]))
        if abs(angle) >= 150:
            marks.append((i, 14, "유턴"))
        elif angle >= TURN_DEG:
            marks.append((i, 13, "우회전"))
        elif angle <= -TURN_DEG:
            marks.append((i, 12, "좌회전"))

    steps = []
    for k, (i, turn_type, word) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(nodes) - 1
        distance = sum(haversine_m(nodes[j], nodes[j + 1]) for j in range(i, end))
        name = graph.ways[hops[i][1]][0].get("name") if hops[i][1] is not None else None
        along = f"{name}{_particle(name)} 따라 " if name else ""
        if turn_type == 200:
            description = f"{along}{distance:.0f}m 이동"
        else:
            description = f"{word} 후 {along}{distance:.0f}m 이동"
        steps.append({"lat": nodes[i][0], "lng": nodes[i][1], "description": description, "turn_type": turn_type})
    steps.append({"lat": nodes[-1][0], "lng": nodes[-1][1], "description": "도착", "turn_type": 201})
    return steps


def route(graph: RoadGraph, start: tuple, end: tuple, waypoints: list = None, night: bool = False):
    """start → 경유지들 → end 경로를 Tmap 응답과 같은 모양으로 준다. 길이 이어지지 않으면 None.

    좌표는 모두 (lat, lng). 반환은 {"features": [...]} — extract_path / extract_steps가 그대로 읽는다.
    """
    stops = [start] + list(waypoints or []) + [end]
    snapped = [graph.nearest_node(lat, lng) for lat, lng in stops]
    if any(node is None for node in snapped):
        return None
    hops, used = [], set()
    for a, b in zip(snapped, snapped[1:]):
        if a == b:
            continue
        leg = graph.shortest(a, b, used, night=night)
        if leg is None:
            return None
        used.update((leg[i][0], leg[i + 1][0]) for i in range(len(leg) - 1))
        hops = hops[:-1] + leg if hops else leg
    if len(hops) < 2:
        return None

    nodes = [node for node, _ in hops]
    total = sum(haversine_m(a, b) for a, b in zip(nodes, nodes[1:]))
    features = [{"geometry": {"type": "Point", "coordinates": [nodes[0][1], nodes[0][0]]},
                 "properties": {"totalDistance": int(round(total))}},
                {"geometry": {"type": "LineString", "coordinates": [[lng, lat] for lat, lng in nodes]}}]
    for step in build_steps(graph, hops):
        features.append({"geometry": {"type": "Point", "coordinates": [step["lng"], step["lat"]]},
                         "properties": {"description": step["description"], "turnType": step["turn_type"]}})
    # 어떤 길로 얼마나 가는지 (길이 기준 비율) — 인도로 달리는 코스인지 확인할 수 있게 같이 준다
    mix = dict.fromkeys(SURFACE_PENALTY, 0.0)
    mix[BLOCKED] = 0.0
    weighted_score = 0.0
    way_lengths = {}   # 길 번호 -> 그 길 위를 지나는 길이. 인도 확인(sidewalk.confirm)이 쓴다
    night_m = dict.fromkeys(NIGHT_PENALTY, 0.0)
    for (a, way), (b, _) in zip(hops, hops[1:]):
        length = haversine_m(a, b)
        mix[graph.surfaces[way]] += length
        weighted_score += graph.way_score(way) * length
        way_lengths[way] = way_lengths.get(way, 0.0) + length
        night_m[graph.night_kind(way)] += length
    return {"features": features, "router": "local", "way_lengths": way_lengths,
            # 밤에 사람이 있는 길 / 인적 드문 길의 비율 (낮에 만든 코스에도 계산해 둔다 — 시간대 점수가 쓴다)
            "lively_ratio": round(night_m["lively"] / total, 3) if total else None,
            "secluded_ratio": round(night_m["secluded"] / total, 3) if total else None,
            "road_score": round(weighted_score / total, 3) if total else None,
            "road_mix": {kind: round(length / total, 3) for kind, length in mix.items()} if total else None}
