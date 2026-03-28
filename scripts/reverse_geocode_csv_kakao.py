import argparse
import csv
import json
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


KAKAO_COORD2ADDRESS_API_URL = "https://dapi.kakao.com/v2/local/geo/coord2address.json"
DEFAULT_KAKAO_REST_API_KEY = "9542eaaddfd1b27482ba934484415f4f"


def read_csv_rows(path: str) -> Tuple[List[Dict[str, str]], List[str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fieldnames = reader.fieldnames if reader.fieldnames is not None else []
    return rows, fieldnames


def ensure_output_fieldnames(fieldnames: List[str]) -> List[str]:
    if "지번주소" in fieldnames:
        return fieldnames

    result: List[str] = []
    inserted = False
    for name in fieldnames:
        result.append(name)
        if name == "주소":
            result.append("지번주소")
            inserted = True

    if inserted is False:
        result.append("지번주소")

    return result


def load_cache(cache_path: str) -> Dict[str, str]:
    cache_file = Path(cache_path)
    if cache_file.exists() is False:
        return {}
    with open(cache_path, "r", encoding="utf-8") as file:
        return json.load(file)


def save_cache(cache_path: str, cache: Dict[str, str]) -> None:
    cache_file = Path(cache_path)
    if cache_file.parent.exists() is False:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as file:
        json.dump(cache, file, ensure_ascii=False, indent=2)


def write_rows(path: str, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    output_path = Path(path)
    if output_path.parent.exists() is False:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def write_failed(path: str, failed_rows: List[Dict[str, str]]) -> None:
    if len(failed_rows) == 0:
        return

    output_path = Path(path)
    if output_path.parent.exists() is False:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        fieldnames = ["latitude", "longitude", "reason"]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in failed_rows:
            writer.writerow(row)


def normalize_coord_key(latitude: str, longitude: str) -> str:
    return f"{latitude.strip()},{longitude.strip()}"


def request_jibun_address(
    api_key: str,
    longitude: str,
    latitude: str,
    timeout: float,
    retries: int,
    request_interval_sec: float,
    last_request_at: List[float],
) -> str:
    for attempt in range(retries):
        sleep_sec = request_interval_sec - (time.time() - last_request_at[0])
        if sleep_sec > 0:
            time.sleep(sleep_sec)

        params = {"x": longitude, "y": latitude, "input_coord": "WGS84"}
        req = Request(
            url=f"{KAKAO_COORD2ADDRESS_API_URL}?{urlencode(params)}",
            headers={"Authorization": f"KakaoAK {api_key}"},
            method="GET",
        )

        try:
            with urlopen(req, timeout=timeout) as response:
                last_request_at[0] = time.time()
                payload = json.loads(response.read().decode("utf-8"))
                documents = payload.get("documents", [])
                if len(documents) == 0:
                    return ""
                first = documents[0]
                address = first.get("address")
                if isinstance(address, dict) is False:
                    return ""
                address_name = address.get("address_name", "")
                if isinstance(address_name, str) is False:
                    return ""
                return address_name
        except HTTPError as error:
            last_request_at[0] = time.time()
            status = error.code
            if status == 429 or (status >= 500 and status < 600):
                if attempt + 1 != retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
            raise RuntimeError(f"Kakao API HTTPError status={status}, x={longitude}, y={latitude}") from error
        except URLError as error:
            last_request_at[0] = time.time()
            if attempt + 1 != retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError(f"Kakao API URLError x={longitude}, y={latitude}") from error

    return ""


def build_default_output_path(input_path: str) -> str:
    source = Path(input_path)
    return str(source.with_name(f"{source.stem}-with-jibun{source.suffix}"))


def main() -> None:
    parser = argparse.ArgumentParser(description="CSV 위경도를 카카오 API로 지번주소 변환")
    parser.add_argument("--input", required=True, help="입력 CSV 경로")
    parser.add_argument("--output", default="", help="출력 CSV 경로 (기본값: 입력파일명-with-jibun.csv)")
    parser.add_argument(
        "--kakao-rest-api-key",
        default="",
        help="카카오 REST API 키 (미지정 시 환경변수 KAKAO_REST_API_KEY 사용)",
    )
    parser.add_argument(
        "--cache-path",
        default="crawling-data/geocode-cache/kakao-coord2address-cache.json",
        help="좌표-지번주소 캐시 파일 경로",
    )
    parser.add_argument("--failed-path", default="crawling-data/geocode-cache/reverse-geocode-failed.csv")
    parser.add_argument("--request-per-second", type=float, default=8.0, help="초당 요청 수")
    parser.add_argument("--timeout", type=float, default=8.0, help="요청 타임아웃(초)")
    parser.add_argument("--retries", type=int, default=4, help="재시도 횟수")
    parser.add_argument(
        "--backup",
        action="store_true",
        help="output이 input과 같을 때 덮어쓰기 전 backup(.bak) 생성",
    )
    args = parser.parse_args()

    api_key = (
        args.kakao_rest_api_key
        if args.kakao_rest_api_key != ""
        else os.getenv("KAKAO_REST_API_KEY", DEFAULT_KAKAO_REST_API_KEY)
    )
    if api_key == "":
        raise SystemExit("카카오 API 키가 없습니다. --kakao-rest-api-key 또는 KAKAO_REST_API_KEY를 설정하세요.")

    input_path = args.input
    output_path = args.output if args.output != "" else build_default_output_path(input_path)

    rows, original_fieldnames = read_csv_rows(input_path)
    if len(rows) == 0:
        raise SystemExit("입력 CSV에 데이터가 없습니다.")

    fieldnames = ensure_output_fieldnames(original_fieldnames)
    cache = load_cache(args.cache_path)
    request_interval_sec = 1.0 / args.request_per_second if args.request_per_second > 0 else 0.0
    last_request_at = [0.0]
    failed_rows: List[Dict[str, str]] = []

    unresolved_indexes: List[int] = []
    for index, row in enumerate(rows):
        lat = row.get("latitude", "").strip()
        lng = row.get("longitude", "").strip()
        if lat == "" or lng == "":
            row["지번주소"] = ""
            continue

        coord_key = normalize_coord_key(lat, lng)
        cached_jibun = cache.get(coord_key)
        if cached_jibun is None:
            unresolved_indexes.append(index)
            continue
        row["지번주소"] = cached_jibun

    print(f"[입력] rows={len(rows)} unresolved={len(unresolved_indexes)} cache={len(cache)}")

    api_response_count = 0
    flush_every_response = 100
    resolved_count = 0

    for done_count, row_index in enumerate(unresolved_indexes, start=1):
        row = rows[row_index]
        lat = row.get("latitude", "").strip()
        lng = row.get("longitude", "").strip()
        coord_key = normalize_coord_key(lat, lng)

        try:
            jibun = request_jibun_address(
                api_key=api_key,
                longitude=lng,
                latitude=lat,
                timeout=args.timeout,
                retries=args.retries,
                request_interval_sec=request_interval_sec,
                last_request_at=last_request_at,
            )
            api_response_count += 1
            cache[coord_key] = jibun
            row["지번주소"] = jibun
            resolved_count += 1
        except Exception as error:
            failed_rows.append({"latitude": lat, "longitude": lng, "reason": str(error)})
            row["지번주소"] = ""

        if api_response_count > 0 and api_response_count % flush_every_response == 0:
            save_cache(args.cache_path, cache)
            write_rows(output_path, fieldnames, rows)
            write_failed(args.failed_path, failed_rows)
            print(
                f"[flush] api_responses={api_response_count} processed={done_count}/{len(unresolved_indexes)} "
                f"(resolved={resolved_count}, failed={len(failed_rows)})"
            )

    if input_path == output_path and args.backup:
        backup_path = f"{input_path}.bak"
        shutil.copyfile(input_path, backup_path)
        print(f"[백업] {backup_path}")

    save_cache(args.cache_path, cache)
    write_rows(output_path, fieldnames, rows)
    write_failed(args.failed_path, failed_rows)

    print(f"[완료] output={output_path}")
    print(f"[완료] resolved={resolved_count} failed={len(failed_rows)} cache={args.cache_path}")


if __name__ == "__main__":
    main()
