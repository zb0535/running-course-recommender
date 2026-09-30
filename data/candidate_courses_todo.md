# 코스 후보 30개 채우기 — 진행 상황

## 자동화 완료

좌표를 사람이 직접 찍지 않아도 됩니다. 랜드마크 이름 두 개(시작~끝)만 알면
`build_courses_from_landmarks.py`가 Nominatim(좌표조회)→카카오(실제경로)→OpenTopography(고도)→OSM(녹지/해안/신호등 태그)까지
전부 자동으로 처리해서 `data/courses.json`에 등록합니다.

```bash
python -m src.data_collection.build_courses_from_landmarks \
  --id my-course --name "코스이름" --region 여수 \
  --start "시작 랜드마크, 여수" --end "끝 랜드마크, 여수"
```

랜드마크 이름은 OSM Nominatim이 인식할 수 있게 조금 구체적으로("오동도, 여수"처럼 지역명 포함) 적어야 함.

## 등록 완료 (7개, 전부 실제 카카오 경로 + 실제 고도 + OSM 태그)

- [x] `yeosu-coastal-01` 이순신광장~오동도 해안로 (3.53km)
- [x] `yeosu-coastal-03` 돌산공원~이순신광장 해안로 (2.36km)
- [x] `yeosu-sports-01` 진남체육공원~이순신광장 (4.69km)
- [x] `yeosu-coastal-04` 웅천친수공원~진남체육공원 해안로 (7.15km)
- [x] `yeosu-expo-01` 여수엑스포~돌산대교 (4.59km)
- [x] `gwangju-urban-01` 상무지구~풍암동 (8.18km)
- [x] `gwangju-river-01` 광주천~학동 산책로 (4.43km)

## 남은 후보 (팀원이 아는 랜드마크 이름만 알려주면 바로 등록)

### 여수
- [ ] 소호요트경기장 주변
- [ ] 여수 자산공원

### 광주
- [ ] 중외공원 순환로 (기존 샘플 `gwangju-park-01`은 가짜 좌표라 실좌표로 교체 필요)
- [ ] 풍암호수공원
- [ ] 광주호수생태공원
- [ ] 영산강 자전거길 (첨단지구 구간)

목표 30개 중 7개 실데이터 등록 완료. 나머지는 팀원별로 아는 동네 러닝 코스 이름만 던져주면 계속 채워집니다.
