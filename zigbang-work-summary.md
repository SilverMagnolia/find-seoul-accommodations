# 직방 매물 매칭 작업 정리

## 1) 지금까지 완료한 작업

### A. 원천 데이터 정비
- 입력 파일: `crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv`
- 총 레코드 확인: 6,470건
- 위경도 기반 데이터 사용 가능 상태 확인

### B. 카카오 역지오코딩(좌표 -> 지번주소) 자동화
- 참고 문서: [Kakao Local API - 좌표로 주소 변환](https://developers.kakao.com/docs/latest/ko/local/dev-guide#coord-to-address)
- 신규 스크립트 추가: `scripts/reverse_geocode_csv_kakao.py`
- 수행 내용:
  - `latitude,longitude`를 카카오 `coord2address` API로 조회
  - `지번주소` 컬럼을 `주소` 바로 옆에 삽입
  - API 응답 100건마다 캐시/CSV flush
  - 캐시 파일 생성: `crawling-data/geocode-cache/kakao-coord2address-cache.json`
  - 실패 로그 파일 경로 준비: `crawling-data/geocode-cache/reverse-geocode-failed.csv`
- 실행 결과:
  - 성공 6,470건 / 실패 0건
  - 원본 덮어쓰기 전 백업 생성:
    - `crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv.bak`

### C. 지번주소 누락 검증
- 대상 파일에서 `지번주소` 공백 여부 검사
- 결과: 누락 0건

### D. 좌표 중복 분석/분리
- 좌표(`latitude,longitude`) 중복 행만 별도 추출 완료
- 산출 파일:
  - `crawling-data/seoul-accomodations/20260318-212418-with-latlng-duplicate-coordinates.csv`
- 통계:
  - 전체 행: 6,470
  - 중복 좌표 행: 3,125
  - 중복 좌표 그룹: 1,149
  - 파일에 `coord_duplicate_count` 컬럼 추가

### E. 직방 API 구조 확인
- 브라우저 네트워크 관찰 기반으로 호출 흐름 확인:
  1. `GET /house/property/v1/items/villas?geohash=...`
  2. `POST /house/property/v1/items/list` with `itemIds`
- geohash 테스트(샘플 좌표):
  - precision 5: 응답 있음
  - precision 6/7: 응답 없음
- 결론: 현재 기준 조회는 precision 5가 유효한 패턴

---

## 2) 현재 남은 작업

### A. 최종 판정 규칙 확정
- 건물 매칭 기준 확정 필요
  - 거리 임계치(예: 30m 단일)
  - 지번주소 매칭 방식(정확 일치/보조 규칙)
- 결과 라벨 정책 확정 필요
  - `없음`으로 고정할지
  - `미검출`로 표기할지

### B. 직방 조회 파이프라인 전량 실행
- 대상: 6,470건 전체
- 절차:
  1. 좌표 -> geohash(precision 5)
  2. `villas` 조회
  3. `items/list` 상세 조회
  4. 전세/월세 여부 + 건물 매칭 규칙 적용

### C. 최종 결과 CSV 산출
- 권장 결과 컬럼(최소):
  - `listing_exists`
  - `matched_count`
  - `matched_sales_types`
  - `nearest_distance_m`
  - `checked_at`

### D. 샘플 품질 검증
- 랜덤 샘플을 웹 UI와 대조해 오탐/누락 확인
- 필요 시 거리/매칭 규칙 재조정

---

## 3) 참고 산출물 목록

- 입력/주요 데이터
  - `crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv`
  - `crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv.bak`
  - `crawling-data/seoul-accomodations/20260318-212418-with-latlng-duplicate-coordinates.csv`
- 스크립트
  - `scripts/reverse_geocode_csv_kakao.py`
  - `scripts/geocode_csv_kakao.py`
- 직방 관찰 데이터
  - `zigbang/get-villas-api-response.json`
  - `zigbang/list-api-request.json`
  - `zigbang/list-api-response.json`
- 캐시
  - `crawling-data/geocode-cache/kakao-coord2address-cache.json`
  - `crawling-data/geocode-cache/kakao-address-cache.json`

