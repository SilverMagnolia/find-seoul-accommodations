# 서울 민박업소 크롤러 실행 가이드

이 프로젝트는 세이프스테이 사이트에서 서울(`STD01`) 민박업소 목록을 순회하고, 각 상세 페이지의 주요 정보를 수집해 CSV로 저장합니다.

## 1) 준비 사항

- Python 3.10 이상
- 인터넷 연결
- 필수 패키지: `requests`, `beautifulsoup4`

설치:

```bash
python3 -m pip install requests beautifulsoup4
```

## 2) 실행 방법

프로젝트 루트에서 아래 명령 실행:

```bash
python3 -u scripts/crawl_seoul_accomodations.py --workers 36 --list-workers 48 --timeout 20 --retries 4
```

기본 실행(옵션 생략):

```bash
python3 scripts/crawl_seoul_accomodations.py
```

기본 저장 경로 규칙:

- `crawling-data/seoul-accomodations/yyyymmdd-HHmmdd.csv`
- 예시: `crawling-data/seoul-accomodations/20260318-211803.csv`

## 3) 주요 옵션

- `--output`: 출력 CSV 경로 (기본값: `crawling-data/seoul-accomodations/yyyymmdd-HHmmdd.csv`)
- `--workers`: 상세 페이지 병렬 수집 스레드 수 (기본값: `24`)
- `--list-workers`: 목록 페이지 병렬 수집 스레드 수 (기본값: `32`)
- `--timeout`: 요청 타임아웃(초) (기본값: `20`)
- `--retries`: 요청 재시도 횟수 (기본값: `4`)

## 4) 결과 파일

출력 파일(기본): `crawling-data/seoul-accomodations/yyyymmdd-HHmmdd.csv`

컬럼:

1. `업소명`
2. `민박업소형태`
3. `주소`
4. `지방행정데이터인허가번호`
5. `지방행정데이터인허가일자`
6. `영업상태`

## 5) 정상 수집 확인

행 수 확인:

```bash
python3 - <<'PY'
import csv
from pathlib import Path

files = sorted(Path("crawling-data/seoul-accomodations").glob("*.csv"))
if len(files) == 0:
    raise SystemExit("CSV 파일이 없습니다. 먼저 크롤링을 실행하세요.")

target = files[-1]
print("target:", target)
with open(target, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.DictReader(f))
print("rows:", len(rows))
print("columns:", list(rows[0].keys()) if rows else [])
PY
```

특정 파일 검증이 필요하면 `--output`으로 경로를 고정해 실행한 뒤 같은 파일명을 넣어 확인하세요.

## 6) 자주 발생하는 이슈

- `ProxyError` 또는 네트워크 오류
  - 사내 프록시/VPN 설정 영향일 수 있습니다.
  - 네트워크 상태 확인 후 재실행하세요.
- 수집 속도가 느릴 때
  - `--workers`, `--list-workers` 값을 줄이거나 늘려 환경에 맞게 조정하세요.
- 파일 인코딩 문제
  - CSV는 `utf-8-sig`로 저장됩니다. 엑셀에서 열 때 한글 깨짐이 있으면 인코딩 설정을 확인하세요.

## 7) 주소 -> 위경도(카카오 API)

카카오 로컬 API의 `주소로 좌표 변환` 엔드포인트를 사용합니다.

- 문서: [Kakao Local REST API](https://developers.kakao.com/docs/latest/ko/local/dev-guide)
- 엔드포인트: `GET /v2/local/search/address.json`
- 인증 헤더: `Authorization: KakaoAK {REST_API_KEY}`

### 실행

기본 실행(현재 스크립트 내부에 키 하드코딩되어 있어 그대로 실행 가능):

```bash
python3 scripts/geocode_csv_kakao.py \
  --input crawling-data/seoul-accomodations/20260318-212418.csv \
  --output crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv
```

### 빠른 실행 순서(복붙용)

1) 프로젝트 폴더 이동

```bash
cd /Users/tony/Documents/projects/airbnb
```

2) 변환 실행

```bash
python3 -u scripts/geocode_csv_kakao.py \
  --input "crawling-data/seoul-accomodations/20260318-212418.csv" \
  --output "crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv" \
  --request-per-second 8 \
  --retries 4
```

3) 결과 확인

```bash
python3 - <<'PY'
import csv
path = "crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv"
with open(path, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.DictReader(f))
print("rows:", len(rows))
print("has_latlng_columns:", "latitude" in rows[0] and "longitude" in rows[0])
filled = sum(1 for r in rows if r.get("latitude", "") != "" and r.get("longitude", "") != "")
print("filled_latlng:", filled)
PY
```

현재 기준으로는 키 관련 추가 설정 없이 실행하면 됩니다.

## 8) 로컬 지도 앱으로 전체 업체 확인

`latitude`, `longitude`가 포함된 CSV를 Streamlit + Folium으로 지도에 표시합니다.

### 설치

```bash
python3 -m pip install streamlit folium streamlit-folium pandas
```

### 실행

프로젝트 루트에서:

```bash
python3 -m streamlit run scripts/view_accommodations_map.py
```

기본 입력 파일:

- `crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv`

실행 후 브라우저에서:

- 자치구/영업상태/검색어 필터 가능
- 마커 클릭 시 업체 상세와 카카오맵/네이버지도 링크 제공
- 로컬에서만 실행하는 내부 업무용 뷰어로 사용 가능

## 9) 모바일 최적화 React SPA

로컬 업무용으로 모바일 화면에 맞춘 SPA 지도를 실행할 수 있습니다.

특징:

- 전체 화면 지도 + 하단 목록(Bottom Sheet)
- 6천건 이상 데이터 대응을 위한 클러스터 렌더링 최적화
- 자치구/영업상태/검색 필터
- 목록 항목 탭 시 지도 이동 + 팝업 오픈

### 실행

프로젝트 루트에서:

```bash
npm install
npm run dev
```

접속 주소:

- [http://localhost:5173](http://localhost:5173)

기본 CSV 경로:

- `/crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv`

참고:

- React SPA는 `react-kakao-maps-sdk`를 사용합니다.
- 카카오 지도 JavaScript 키는 현재 `src/App.tsx`에 하드코딩되어 있습니다.

### 결과

- 기존 컬럼 뒤에 `latitude`, `longitude` 컬럼이 추가됩니다.
- 주소 중복을 자동 제거해 API 호출량을 줄입니다.
- 캐시 파일: `crawling-data/geocode-cache/kakao-address-cache.json`
- 실패 로그: `crawling-data/geocode-cache/geocode-failed.csv`
- 진행 중 100건마다 캐시/결과 CSV를 즉시 저장(flush)합니다.

### 진행이 느리거나 멈춘 것처럼 보일 때

- 이 스크립트는 100건마다 진행 로그를 출력하므로, 중간에 잠시 출력이 없을 수 있습니다.
- 요청 실패 시 재시도 때문에 속도가 내려갈 수 있습니다.
- 같은 입력 파일로 다시 실행하면 캐시를 재사용해 이후 실행은 빨라집니다.
- 너무 느리면 `--request-per-second` 값을 낮춰(예: `5`) 안정적으로 실행하세요.
