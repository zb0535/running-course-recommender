"""길 점수 모델을 학습한다: 어떤 길이 '달리기 좋은 길'인가.

정답은 사람들이 달리기·걷기·자전거 코스로 OSM에 등록해 둔 길이다(route=running·foot·bicycle 등).
한강·탄천 자전거길처럼 실제로 러너가 몰리는 길이 여기에 들어 있다. 그 길들과, 같은 동네의 나머지
길을 비교해서 길의 종류·주변 지형 중 무엇이 그런 길을 만드는지 로지스틱 회귀로 배운다.

정답에서 빼는 것 (오답에도 넣지 않는다 — 좋은 길이라고도 아니라고도 하기 어렵다):
- 등산로(route=hiking)
- 자전거 코스 중 차도 구간. 자전거는 차도로도 다니지만 달리는 사람은 인도로 간다. 그대로 정답에 넣으면
  "큰길일수록 좋다"를 배운다(실제로 그렇게 나왔다). 자전거 코스는 차가 없는 구간만 정답으로 쓴다.

사용 (로컬 지형 DB가 있어야 한다):
    python -m src.recommend.learn_roads           # 학습 → data/road_model.json
"""
import argparse
import json
import math
import random
import sqlite3
from collections import defaultdict

from ..data_collection import local_osm
from ..data_collection.area_cache import Area
from . import road_graph as rg

ON_FOOT = ("running", "fitness_trail", "foot", "walking")
POSITIVE = ON_FOOT + ("bicycle",)
IGNORED = ("hiking",)


def label(tags: dict):
    """1 = 달리기 좋은 길, 0 = 그렇게 등록되지 않은 길, None = 학습에 쓰지 않는다."""
    route = tags.get("route")
    if route in ON_FOOT:
        return 1
    if route == "bicycle":
        return 1 if rg.surface(tags) == "walkway" else None
    return None if route in IGNORED else 0
NEGATIVES_PER_POSITIVE = 4


def cells_with_routes(db_path: str = None) -> list:
    """공개 코스에 속한 길이 있는 격자 칸."""
    db = sqlite3.connect(db_path or local_osm.DB_PATH)
    try:
        like = " OR ".join(f"""body LIKE '%"route":"{kind}"%'""" for kind in POSITIVE)
        rows = db.execute(
            f"SELECT DISTINCT r, c FROM cells WHERE eid IN (SELECT id FROM elements WHERE kind = 'r' AND ({like}))"
        ).fetchall()
    finally:
        db.close()
    return sorted(rows)


def samples_from_cell(cell: tuple, rng: random.Random, db_path: str = None) -> list:
    """한 칸의 길들에서 (특징, 정답) 표본을 만든다. 주변 지형을 재려고 한 칸 바깥까지 읽는다."""
    r, c = cell
    d = local_osm.CELL_DEG
    bbox = (r * d - 0.004, c * d - 0.004, (r + 1) * d + 0.004, (c + 1) * d + 0.004)
    elements = local_osm.load_bbox(bbox, db_path, kinds="rs")
    indexes = Area(bbox, elements).indexes
    positives, negatives = [], []
    for element in elements:
        tags = element.get("tags") or {}
        if element.get("type") != "way" or not rg._walkable(tags) or label(tags) is None:
            continue
        points = [(g["lat"], g["lon"]) for g in element["geometry"]]
        mid = points[len(points) // 2]
        if not (r * d <= mid[0] < (r + 1) * d and c * d <= mid[1] < (c + 1) * d):
            continue  # 옆 칸에서 다시 나온다 — 두 번 세지 않는다
        (positives if label(tags) else negatives).append((tags, points))
    rng.shuffle(negatives)
    negatives = negatives[: max(len(positives) * NEGATIVES_PER_POSITIVE, 1)]
    return ([(rg.way_features(t, p, indexes), 1) for t, p in positives]
            + [(rg.way_features(t, p, indexes), 0) for t, p in negatives])


def train(samples: list, epochs: int = 30, rate: float = 0.1, l2: float = 1e-4, seed: int = 0) -> dict:
    """로지스틱 회귀(확률적 경사하강). 정답과 오답의 비중을 같게 맞춘다."""
    rng = random.Random(seed)
    positives = sum(label for _, label in samples)
    weight_of = {1: len(samples) / (2 * max(positives, 1)), 0: len(samples) / (2 * max(len(samples) - positives, 1))}
    weights = defaultdict(float)
    bias = 0.0
    samples = list(samples)
    for epoch in range(epochs):
        rng.shuffle(samples)
        step = rate / (1 + epoch * 0.2)
        for features, label in samples:
            z = bias + sum(weights[name] * value for name, value in features.items())
            error = (1 / (1 + math.exp(-max(min(z, 30), -30))) - label) * weight_of[label]
            bias -= step * error
            for name, value in features.items():
                weights[name] -= step * (error * value + l2 * weights[name])
    return {"bias": round(bias, 4), "weights": {name: round(weights[name], 4) for name in rg.FEATURES}}


def auc(samples: list, model: dict) -> float:
    """정답 길이 오답 길보다 높은 점수를 받을 확률 (0.5 = 찍기, 1.0 = 완벽)."""
    scored = sorted((rg.score(features, model), label) for features, label in samples)
    positives = sum(label for _, label in scored)
    negatives = len(scored) - positives
    if not positives or not negatives:
        return float("nan")
    # 같은 점수는 평균 순위로 처리한다 (특징이 대부분 0/1이라 동점이 많다)
    rank_sum, i = 0.0, 0
    while i < len(scored):
        j = i
        while j < len(scored) and scored[j][0] == scored[i][0]:
            j += 1
        average_rank = (i + j + 1) / 2
        rank_sum += average_rank * sum(label for _, label in scored[i:j])
        i = j
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cells", type=int, default=600, help="학습에 쓸 격자 칸 수")
    parser.add_argument("--out", default=rg.MODEL_PATH)
    args = parser.parse_args()
    if not local_osm.available():
        raise SystemExit("로컬 지형 DB가 없습니다. 먼저: python -m src.data_collection.local_osm")

    rng = random.Random(0)
    cells = cells_with_routes()
    rng.shuffle(cells)
    cells = cells[: args.cells]
    held_out = set(cells[: len(cells) // 5])   # 칸 단위로 나눈다 — 같은 길이 학습과 검증에 걸치지 않게
    train_set, test_set = [], []
    for n, cell in enumerate(cells, 1):
        (test_set if cell in held_out else train_set).extend(samples_from_cell(cell, rng))
        if n % 100 == 0:
            print(f"  {n}/{len(cells)}칸", flush=True)

    model = train(train_set)
    model["trained_on"] = {
        "cells": len(cells), "train": len(train_set), "test": len(test_set),
        "positives": sum(label for _, label in train_set + test_set),
        "auc_train": round(auc(train_set, model), 3), "auc_test": round(auc(test_set, model), 3),
        "auc_test_prior": round(auc(test_set, rg.PRIOR), 3),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=2)
    print(json.dumps(model, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
