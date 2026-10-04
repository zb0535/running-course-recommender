# GraphML Loop 파일럿 (용봉동)

이 기능은 기존 Tmap 기반 추천을 교체하지 않는다. `?graph_debug=1` 화면에서 사용자가 **순환**을 선택했을 때만 프런트가 `loop_engine: "graphml"`을 보내며, 서버는 GraphML Loop 엔진을 사용한다.

## 배포 환경 변수

배포 서버의 환경 변수에 아래를 추가한다.

```text
GRAPH_LOOP_ENABLED=1
GRAPH_LOOP_MAP_PATH=data/graph/yongbong.graphml
```

서버가 시작될 때 용봉동 GraphML을 워커당 한 번 메모리에 올린다. Render 등에서는 워커를 1개만 사용한다.

GraphML 엔진이 꺼진 상태에서 검증 화면으로 순환 코스를 요청하면 명시적으로 `503`을 반환한다. 조용히 Tmap으로 바꾸지 않는다.

## 검증 범위

```text
용봉동 검증 좌표: 35.1765, 126.9110
3km: GraphML Loop 반환 및 거리 허용오차 통과 확인
5km: 통과 가능한 후보가 있는 경우만 반환
10km: 현재 용봉동 그래프에서는 거리 불일치 후보를 추천하지 않고 실패 처리
```

## 일반 사용자 영향

- 일반 주소에서는 `loop_engine` 필드를 보내지 않아 기존 Tmap 추천 흐름이 유지된다.
- GraphML 코스는 아직 Tmap 도로명/turnType 정보가 없으므로 `steps`를 빈 배열로 반환한다. 지도 경로와 거리·횡단보도 수는 보이지만 음성 턴바이턴은 다음 단계에서 보완한다.
- `scenery_pending=true`로 표시한다. GraphML edge에 풍경·고도·안전 속성이 추가되기 전에는 풍경을 추정해서 붙이지 않는다.
