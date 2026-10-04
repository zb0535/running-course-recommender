"""내려받은 OSM 파일로 만든 로컬 지형 DB. 실행 중에 Overpass를 부르지 않기 위한 것이다.

Overpass는 건당 5~20초가 걸리고 504·429로 자주 실패한다. 같은 데이터가 Geofabrik에 파일로
배포되므로(한국 전체 약 290MB) 한 번 받아 필요한 길·지형만 SQLite에 넣어 두면, 어느 동네든
수십 ms 안에 읽을 수 있고 실패할 일이 없다.

만들기:
    pip install osmium==4.3.1
    python -m src.data_collection.local_osm            # 파일을 내려받아 DB를 만든다
    python -m src.data_collection.local_osm --pbf 경로  # 이미 받은 파일로 만든다

DB가 없으면 area_cache가 예전처럼 Overpass로 받는다.
"""
import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time

DB_PATH = os.path.join("data", "osm_local", "korea.sqlite")
PBF_PATH = os.path.join("data", "osm_extract", "south-korea.osm.pbf")
# 팀 전원이 같은 지도를 쓰도록 날짜를 고정한 파일을 받는다. "latest"를 받으면 받은 날마다 데이터가
# 달라서, 같은 요청에도 사람마다 다른 경로가 나온다. 바꿀 때는 URL·MD5·BUILD_VERSION을 같이 바꾼다.
PBF_URL = "https://download.geofabrik.de/asia/south-korea-260929.osm.pbf"
PBF_MD5 = "7775afecd9ccbf3285ace3f1fa2ec7f4"
# DB를 만드는 코드가 바뀌면 올린다. 설치 스크립트가 이 값이 다른 DB를 다시 만든다.
BUILD_VERSION = "2026-10-01-safety"
CELL_DEG = 0.02

# 저장할 길: 사람이 지나갈 수 있는 길 전부. 경로를 직접 짜려면(road_graph) 큰길·통로까지 이어져 있어야 한다.
# 자동차 전용(motorway·trunk)만 뺀다. 경유지를 붙일 길은 area_cache.SNAP_HIGHWAYS가 따로 고른다.
RUNNABLE = {"footway", "path", "pedestrian", "living_street", "residential", "cycleway",
            "unclassified", "tertiary", "tertiary_link", "secondary", "secondary_link",
            "primary", "primary_link", "service", "track", "steps", "road"}
# 사람들이 실제로 달리거나 걷는 코스로 등록해 둔 경로(OSM route 관계). 길 점수 학습의 정답으로 쓴다
ROUTE_KINDS = ("running", "fitness_trail", "foot", "walking", "bicycle", "hiking")
ROAD_DETAIL = ("name", "sidewalk", "tunnel", "foot", "access")
NOT_A_ROAD = {"proposed", "construction", "abandoned", "razed", "platform"}
KEYS = ("highway", "natural", "waterway", "leisure", "landuse", "amenity", "historic", "tourism", "shop", "man_made")


def keep_tags(tags: dict, is_node: bool = False) -> dict | None:
    """풍경 측정과 경유지 잡기에 쓰는 태그만 남긴다. 쓸 일이 없는 요소면 None."""
    kept = {}
    highway = tags.get("highway")
    if highway and not is_node and (highway in RUNNABLE or tags.get("bridge") == "yes"):
        kept["highway"] = highway
        if tags.get("bridge") == "yes":
            kept["bridge"] = "yes"
        if tags.get("lit") in ("yes", "24/7", "automatic"):
            kept["lit"] = "yes"
        for key in ROAD_DETAIL:
            if tags.get(key):
                kept[key] = tags[key]
    if not is_node:
        if tags.get("natural") in ("coastline", "beach", "water", "wood"):
            kept["natural"] = tags["natural"]
            if tags.get("water"):
                kept["water"] = tags["water"]
        if tags.get("waterway") in ("river", "canal", "stream"):
            kept["waterway"] = tags["waterway"]
        if tags.get("leisure") in ("park", "garden", "track", "stadium", "sports_centre"):
            kept["leisure"] = tags["leisure"]
        if tags.get("landuse") == "forest":
            kept["landuse"] = "forest"
        if tags.get("amenity") in ("university", "college"):
            kept["amenity"] = tags["amenity"]
    if tags.get("historic"):
        kept["historic"] = tags["historic"]
    if tags.get("tourism") in ("museum", "attraction", "viewpoint"):
        kept["tourism"] = tags["tourism"]
    if is_node and tags.get("shop"):
        kept["shop"] = tags["shop"]
    # 안전 점수와 신호등 수에 쓰는 것: CCTV, 지구대·파출소, 신호등
    if is_node and tags.get("man_made") == "surveillance":
        kept["man_made"] = "surveillance"
    if tags.get("amenity") == "police":
        kept["amenity"] = "police"
    if is_node and tags.get("highway") == "traffic_signals":
        kept["highway"] = "traffic_signals"
    return kept or None


def _cells(points: list):
    """점들의 외곽 상자가 걸치는 격자 칸."""
    lats, lngs = [p[0] for p in points], [p[1] for p in points]
    r1, r2 = math.floor(min(lats) / CELL_DEG), math.floor(max(lats) / CELL_DEG)
    c1, c2 = math.floor(min(lngs) / CELL_DEG), math.floor(max(lngs) / CELL_DEG)
    return [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]


class Writer:
    """요소를 SQLite에 쌓는다. 좌표는 [lat, lng, lat, lng, ...]로 납작하게 저장해 크기를 줄인다."""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        if os.path.exists(db_path):
            os.remove(db_path)
        self.db = sqlite3.connect(db_path)
        self.db.executescript("""
            PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;
            CREATE TABLE elements (id INTEGER PRIMARY KEY, body TEXT, first_node INTEGER, last_node INTEGER,
                                   dead_end INTEGER DEFAULT 0, kind TEXT);
            CREATE TABLE cells (r INTEGER, c INTEGER, eid INTEGER);
        """)
        self.count = 0
        self.endpoints = set()   # 저장한 길의 양 끝 노드 — 막다른 길 판정에 쓴다
        self.source_md5 = None   # 만든 OSM 파일의 MD5 — 팀원과 같은 데이터인지 확인용
        self._elements, self._cells = [], []

    def _add(self, body: dict, points: list, first=None, last=None):
        self.count += 1
        # 종류: r=길, p=상점, s=풍경 지형, f=안전 시설. 필요한 종류만 읽기 위한 것
        tags = body["k"]
        facility = (tags.get("man_made") == "surveillance" or tags.get("amenity") == "police"
                    or tags.get("highway") == "traffic_signals")
        # f=안전 시설(CCTV·경찰·신호등). 신호등은 highway 태그를 쓰지만 길이 아니다
        kind = "f" if facility else "r" if "highway" in tags else "p" if "shop" in tags else "s"
        self._elements.append((self.count, json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                               first, last, kind))
        self._cells.extend((r, c, self.count) for r, c in _cells(points))
        if len(self._elements) >= 20000:
            self.flush()

    def add_node(self, lat: float, lng: float, tags: dict):
        self._add({"t": "n", "g": [round(lat, 6), round(lng, 6)], "k": tags}, [(lat, lng)])

    def add_way(self, points: list, tags: dict, first_node=None, last_node=None):
        flat = [round(v, 6) for p in points for v in p]
        is_road = "highway" in tags and first_node is not None
        if is_road:
            self.endpoints.update((first_node, last_node))
        self._add({"t": "w", "g": flat, "k": tags}, points,
                  first_node if is_road else None, last_node if is_road else None)

    def add_rings(self, rings: list, tags: dict, holes: list = ()):
        """여러 고리로 된 면(멀티폴리곤). holes는 그 안에 뚫린 구멍."""
        points = [p for ring in rings for p in ring]
        if points:
            flat = lambda group: [[round(v, 6) for p in ring for v in p] for ring in group]
            body = {"t": "r", "g": flat(rings), "k": tags}
            if holes:
                body["h"] = flat(holes)
            self._add(body, points)

    def flush(self):
        self.db.executemany("INSERT INTO elements (id, body, first_node, last_node, kind) VALUES (?,?,?,?,?)", self._elements)
        self.db.executemany("INSERT INTO cells VALUES (?,?,?)", self._cells)
        self._elements, self._cells = [], []

    def finish(self, degree: dict):
        """degree: {노드: 그 노드를 지나는 길의 수}. 끝이 다른 길과 이어지지 않은 길을 막다른 길로 표시한다."""
        self.flush()
        dead = [(eid,) for eid, first, last in
                self.db.execute("SELECT id, first_node, last_node FROM elements WHERE first_node IS NOT NULL")
                if first != last and (degree.get(first, 0) < 2 or degree.get(last, 0) < 2)]
        self.db.executemany("UPDATE elements SET dead_end = 1 WHERE id = ?", dead)
        self.db.execute("CREATE INDEX cells_rc ON cells (r, c)")
        self.db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        self.db.executemany("INSERT INTO meta VALUES (?,?)", [
            ("elements", str(self.count)), ("dead_ends", str(len(dead))), ("build_version", BUILD_VERSION),
            ("source_md5", self.source_md5 or ""), ("built_at", time.strftime("%Y-%m-%d %H:%M:%S"))])
        self.db.commit()
        self.db.close()
        return len(dead)


def _pairs(flat: list) -> list:
    return [{"lat": flat[i], "lon": flat[i + 1]} for i in range(0, len(flat), 2)]


def _element(eid: int, body: dict, dead_end: int) -> dict:
    """저장 형식을 Overpass 응답과 같은 모양으로 되돌린다 (색인·측정 코드가 그 모양을 쓴다)."""
    if body["t"] == "n":
        return {"type": "node", "id": eid, "lat": body["g"][0], "lon": body["g"][1], "tags": body["k"]}
    if body["t"] == "r":
        return {"type": "relation", "id": eid, "tags": body["k"],
                "members": [{"role": "outer", "geometry": _pairs(ring)} for ring in body["g"]]
                + [{"role": "inner", "geometry": _pairs(ring)} for ring in body.get("h", [])]}
    element = {"type": "way", "id": eid, "geometry": _pairs(body["g"]), "tags": body["k"]}
    if "highway" in body["k"]:
        element["dead_end"] = bool(dead_end)
    return element


def available(db_path: str = None) -> bool:
    return os.path.exists(db_path or DB_PATH)


def info(db_path: str = None):
    """DB가 어떤 데이터·어떤 코드로 만들어졌는지. 없으면 None. 팀원끼리 같은 환경인지 비교할 때 쓴다."""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return None
    db = sqlite3.connect(path)
    try:
        has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        meta = dict(db.execute("SELECT key, value FROM meta")) if has_meta else {}
    finally:
        db.close()
    return {"elements": int(meta["elements"]) if meta.get("elements") else None,
            "build_version": meta.get("build_version"), "source_md5": meta.get("source_md5"),
            "built_at": meta.get("built_at"),
            "matches_team": meta.get("build_version") == BUILD_VERSION and meta.get("source_md5") == PBF_MD5}


def file_md5(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stamp(db_path: str, pbf_path: str) -> None:
    """메타 정보가 없는 예전 DB에 출처를 적는다 (다시 만들지 않고)."""
    db = sqlite3.connect(db_path)
    try:
        count = db.execute("SELECT COUNT(*) FROM elements").fetchone()[0]
        dead = db.execute("SELECT COUNT(*) FROM elements WHERE dead_end = 1").fetchone()[0]
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
            ("elements", str(count)), ("dead_ends", str(dead)), ("build_version", BUILD_VERSION),
            ("source_md5", file_md5(pbf_path)), ("built_at", time.strftime("%Y-%m-%d %H:%M:%S"))])
        db.commit()
    finally:
        db.close()


def load_bbox(bbox: tuple, db_path: str = None, kinds: str = None) -> list:
    """(south, west, north, east) 안에 걸친 요소들. kinds="s"처럼 주면 그 종류만 읽는다."""
    south, west, north, east = bbox
    r1, r2 = math.floor(south / CELL_DEG), math.floor(north / CELL_DEG)
    c1, c2 = math.floor(west / CELL_DEG), math.floor(east / CELL_DEG)
    db = sqlite3.connect(db_path or DB_PATH)
    try:
        rows = db.execute(
            "SELECT id, body, dead_end FROM elements WHERE id IN "
            "(SELECT eid FROM cells WHERE r BETWEEN ? AND ? AND c BETWEEN ? AND ?)"
            + (f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else ""),
            (r1, r2, c1, c2, *(kinds or ""))).fetchall()
    finally:
        db.close()
    return [_element(eid, json.loads(body), dead_end) for eid, body, dead_end in rows]


# ── 여기부터는 DB를 만드는 쪽 (osmium 필요) ──

def download(pbf_path: str):
    import requests
    os.makedirs(os.path.dirname(pbf_path), exist_ok=True)
    print(f"내려받는 중: {PBF_URL}")
    with requests.get(PBF_URL, stream=True, timeout=60, allow_redirects=True) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        done = 0
        with open(pbf_path + ".part", "wb") as f:
            for chunk in response.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and done % (20 << 20) < (1 << 20):
                    print(f"  {done >> 20}/{total >> 20} MB", flush=True)
    got = file_md5(pbf_path + ".part")
    if got != PBF_MD5:
        os.remove(pbf_path + ".part")
        raise SystemExit(f"받은 파일이 팀 기준 파일과 다릅니다 (MD5 {got}). 다시 시도하거나 팀원에게 파일을 받으세요.")
    os.replace(pbf_path + ".part", pbf_path)


def build(pbf_path: str, db_path: str):
    try:
        import osmium
    except ImportError:
        sys.exit("osmium이 필요합니다: pip install osmium==4.3.1")

    started = time.time()
    # 달리기·걷기·자전거 코스로 등록된 길을 먼저 모은다
    on_route = {}
    for relation in osmium.FileProcessor(pbf_path, osmium.osm.RELATION):
        kind = relation.tags.get("route")
        if relation.tags.get("type") == "route" and kind in ROUTE_KINDS:
            for member in relation.members:
                if member.type == "w":
                    on_route.setdefault(member.ref, kind)
    print(f"  공개 코스에 속한 길 {len(on_route):,}개", flush=True)
    writer = Writer(db_path)
    writer.source_md5 = file_md5(pbf_path)
    reader = osmium.FileProcessor(pbf_path).with_locations().with_areas().with_filter(osmium.filter.KeyFilter(*KEYS))
    for obj in reader:
        if obj.is_node():
            tags = keep_tags({t.k: t.v for t in obj.tags}, is_node=True)
            if tags:
                writer.add_node(obj.lat, obj.lon, tags)
        elif obj.is_way():
            tags = keep_tags({t.k: t.v for t in obj.tags})
            if not tags:
                continue
            points = [(n.lat, n.lon) for n in obj.nodes if n.location.valid()]
            if "highway" in tags and obj.id in on_route:
                tags["route"] = on_route[obj.id]
            if len(points) >= 2:
                writer.add_way(points, tags, obj.nodes[0].ref, obj.nodes[-1].ref)
        elif obj.is_area() and not obj.from_way():
            tags = keep_tags({t.k: t.v for t in obj.tags})
            if tags:
                tags.pop("highway", None)
                coords = lambda ring: [(n.lat, n.lon) for n in ring if n.location.valid()]
                rings, holes = [], []
                for outer in obj.outer_rings():
                    rings.append(coords(outer))
                    holes.extend(coords(inner) for inner in obj.inner_rings(outer))
                writer.add_rings([r for r in rings if len(r) >= 4], tags, [h for h in holes if len(h) >= 4])
        if writer.count and writer.count % 500000 == 0:
            print(f"  {writer.count:,}개 저장 ({time.time() - started:.0f}초)", flush=True)

    # 길의 끝이 다른 길과 이어지는지 본다. 저장하지 않는 큰길·통로도 이어진 길로 친다 —
    # 주택가 길이 큰길로 나가는 것은 막다른 길이 아니다.
    print("  길 연결 확인 중...", flush=True)
    degree = dict.fromkeys(writer.endpoints, 0)
    for way in osmium.FileProcessor(pbf_path, osmium.osm.WAY).with_filter(osmium.filter.KeyFilter("highway")):
        if way.tags.get("highway") in NOT_A_ROAD:
            continue
        for node in way.nodes:
            if node.ref in degree:
                degree[node.ref] += 1
    dead = writer.finish(degree)
    size_mb = os.path.getsize(db_path) / 1e6
    print(f"완료: 요소 {writer.count:,}개 (막다른 길 {dead:,}개), {size_mb:.0f}MB, {time.time() - started:.0f}초 → {db_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pbf", default=PBF_PATH, help="OSM 파일(.osm.pbf). 없으면 내려받는다")
    parser.add_argument("--out", default=DB_PATH)
    args = parser.parse_args()
    if not os.path.exists(args.pbf):
        download(args.pbf)
    build(args.pbf, args.out)


if __name__ == "__main__":
    main()
