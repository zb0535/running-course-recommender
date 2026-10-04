"""사용자에게 보여주는 풍경 선택지.

측정으로 붙는 태그는 16가지이고 기준이 "경로의 40% 이상이 해안선 200m 이내" 같은 식이다. 정확하지만
고르는 사람에게는 어렵다 — 보행자길·자전거길·차없는길이 어떻게 다른지, 바다뷰와 해변 중 무엇을 골라야
하는지 알 수 없다. 그래서 사람이 고르는 선택지는 따로 둔다: 10가지, 한 줄 설명, 4개 묶음.

선택지 하나는 태그 여러 개에 대응한다. '바다'를 고르면 바다뷰나 해변 중 하나만 지나도 맞는 코스다.
측정과 태그는 그대로다 — 바뀌는 건 보여주고 고르는 방식뿐이다.
"""

# (선택지 이름, 묶음, 한 줄 설명, 대응하는 측정 태그들)
CHOICES = [
    ("바다", "물가", "바다를 보면서 달려요", ("바다뷰", "해변")),
    ("강·하천", "물가", "물길 옆 산책로를 따라 달려요", ("강변",)),
    ("호수", "물가", "호수나 저수지 옆을 달려요", ("호수",)),
    ("공원", "자연", "공원 안이나 공원 옆을 달려요", ("공원",)),
    ("숲", "자연", "나무가 우거진 길을 달려요", ("숲길", "산길")),
    ("차 없는 길", "길", "차가 다니지 않는 산책로·자전거길로 달려요", ("차없는길", "보행자길", "자전거길")),
    ("캠퍼스·운동장", "길", "대학 캠퍼스나 운동장 옆을 달려요", ("캠퍼스", "운동장")),
    ("도심", "볼거리", "가게가 많은 시내를 달려요", ("도심",)),
    ("명소·전망", "볼거리", "유적·명소나 전망 좋은 곳을 지나요", ("역사문화", "전망")),
    ("다리 건너기", "볼거리", "큰 다리를 건너요", ("다리",)),
]
GROUPS = ["물가", "자연", "길", "볼거리"]
TAGS_OF = {name: tags for name, _, _, tags in CHOICES}
CHOICE_OF = {tag: name for name, _, _, tags in CHOICES for tag in tags}


def is_choice(value: str) -> bool:
    return value in TAGS_OF


def resolve(values) -> tuple:
    """고른 값들 → (측정 태그 집합, 묶음 목록).

    values에는 선택지 이름과 측정 태그가 섞여 있어도 된다(예전 앱, 문장에서 뽑은 태그).
    묶음은 "이 중 하나만 지나면 그 선택을 만족한다"는 단위다. 점수를 매길 때 쓴다.
    """
    groups = {}
    for value in values or ():
        name = value if value in TAGS_OF else CHOICE_OF.get(value)
        if name is None:
            continue
        # 태그를 직접 준 경우에는 그 태그만, 선택지를 고른 경우에는 대응하는 태그 전부
        wanted = TAGS_OF[name] if value in TAGS_OF else (value,)
        groups[name] = tuple(dict.fromkeys(groups.get(name, ()) + tuple(wanted)))
    tags = {tag for group in groups.values() for tag in group}
    return tags, [list(group) for group in groups.values()]


def labels_for(tags) -> list:
    """코스의 측정 태그 → 사용자에게 보여줄 이름들 (선택지 순서대로, 중복 없이)."""
    tags = set(tags or ())
    return [name for name, _, _, mapped in CHOICES if tags & set(mapped)]


def near_text(distance_km) -> str:
    """거리를 숫자 대신 감이 오는 말로."""
    if distance_km is None:
        return ""
    if distance_km < 0.15:
        return "바로 근처"
    if distance_km < 1.0:
        return f"걸어서 {max(2, round(distance_km * 1000 / 80))}분"   # 분당 80m
    return f"{distance_km:.1f}km"
