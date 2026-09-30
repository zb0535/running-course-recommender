"""자연어 서술형 입력 -> 환경태그 키워드 사전 매칭.

프론트는 문장형 UI("멈추지 않고 달릴 수 있고 나무가 많거나 바다가 보이는 코스")를 받고,
여기서 사전 정의된 키워드를 매칭해 environment_tags 집합으로 변환한 뒤 동일 추천 파이프라인에 투입한다.
정교한 NLU는 이번 학기 범위 밖 (프로젝트 문서 참고).
"""

KEYWORD_TAG_MAP = {
    "숲길": ["숲", "나무", "녹지", "산책로", "그늘"],
    "바다뷰": ["바다", "해안", "해변", "바닷가"],
    "차없는길": ["멈추지 않", "안 멈추", "신호등 없이", "신호 없이", "끊기지 않"],
    "도심": ["도심", "시내", "번화가", "도시"],
}


def extract_tags(text: str) -> set:
    """문장에서 environment_tags 집합을 추출한다."""
    if not text:
        return set()

    found = set()
    for tag, keywords in KEYWORD_TAG_MAP.items():
        if any(kw in text for kw in keywords):
            found.add(tag)
    return found
