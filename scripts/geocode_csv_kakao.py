import argparse
import csv
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


KAKAO_ADDRESS_API_URL = "https://dapi.kakao.com/v2/local/search/address.json"
DEFAULT_KAKAO_REST_API_KEY = "9542eaaddfd1b27482ba934484415f4f"


def normalize_address(address: str) -> str:
    cleaned = re.sub(r"^\[[0-9-]+\]\s*", "", address.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def read_csv_rows(path: str) -> Tuple[List[Dict[str, str]], List[str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fieldnames = reader.fieldnames if reader.fieldnames is not None else []
    return rows, fieldnames


def ensure_output_fieldnames(fieldnames: List[str]) -> List[str]:
    base = [name for name in fieldnames if name != "latitude" and name != "longitude"]
    return [*base, "latitude", "longitude"]


def load_cache(cache_path: str) -> Dict[str, Dict[str, str]]:
    cache_file = Path(cache_path)
    if cache_file.exists() is False:
        return {}
    with open(cache_path, "r", encoding="utf-8") as file:
        return json.load(file)


def save_cache(cache_path: str, cache: Dict[str, Dict[str, str]]) -> None:
    cache_file = Path(cache_path)
    if cache_file.parent.exists() is False:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as file:
        json.dump(cache, file, ensure_ascii=False, indent=2)


def request_geocode(
    api_key: str,
    query: str,
    timeout: float,
    retries: int,
    request_interval_sec: float,
    last_request_at: List[float],
) -> Tuple[str, str]:
    for attempt in range(retries):
        sleep_sec = request_interval_sec - (time.time() - last_request_at[0])
        if sleep_sec > 0:
            time.sleep(sleep_sec)

        params = {"query": query, "analyze_type": "similar"}
        req = Request(
            url=f"{KAKAO_ADDRESS_API_URL}?{urlencode(params)}",
            headers={"Authorization": f"KakaoAK {api_key}"},
            method="GET",
        )

        try:
            with urlopen(req, timeout=timeout) as response:
                last_request_at[0] = time.time()
                payload = json.loads(response.read().decode("utf-8"))
                documents = payload.get("documents", [])
                if len(documents) == 0:
                    return "", ""
                first = documents[0]
                longitude = first.get("x", "")
                latitude = first.get("y", "")
                return str(latitude), str(longitude)
        except HTTPError as error:
            last_request_at[0] = time.time()
            status = error.code
            error_body = ""
            try:
                error_body = error.read().decode("utf-8", errors="ignore")
            except Exception:
                error_body = ""
            if status == 429 or (status >= 500 and status < 600):
                if attempt + 1 != retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
            raise RuntimeError(
                f"Kakao API HTTPError status={status}, query={query}, body={error_body}"
            ) from error
        except URLError as error:
            last_request_at[0] = time.time()
            if attempt + 1 != retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError(f"Kakao API URLError query={query}") from error

    return "", ""


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
    fieldnames = ["address", "reason"]
    write_rows(path, fieldnames, failed_rows)


def apply_geo_to_rows(
    rows: List[Dict[str, str]],
    address_to_rows: Dict[str, List[int]],
    address: str,
    latitude: str,
    longitude: str,
) -> None:
    target_indexes = address_to_rows.get(address, [])
    for row_index in target_indexes:
        rows[row_index]["latitude"] = latitude
        rows[row_index]["longitude"] = longitude


def build_default_output_path(input_path: str) -> str:
    source = Path(input_path)
    return str(source.with_name(f"{source.stem}-with-latlng{source.suffix}"))


def main() -> None:
    parser = argparse.ArgumentParser(description="CSV 주소를 카카오 API로 위경도 변환")
    parser.add_argument("--input", required=True, help="입력 CSV 경로")
    parser.add_argument("--output", default="", help="출력 CSV 경로 (기본값: 입력파일명-with-latlng.csv)")
    parser.add_argument(
        "--kakao-rest-api-key",
        default="",
        help="카카오 REST API 키 (미지정 시 환경변수 KAKAO_REST_API_KEY 사용)",
    )
    parser.add_argument(
        "--cache-path",
        default="crawling-data/geocode-cache/kakao-address-cache.json",
        help="주소-좌표 캐시 파일 경로",
    )
    parser.add_argument("--failed-path", default="crawling-data/geocode-cache/geocode-failed.csv")
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
    flush_every = 100

    address_to_rows: Dict[str, List[int]] = {}
    for index, row in enumerate(rows):
        normalized = normalize_address(row.get("주소", ""))
        if normalized == "":
            continue
        if normalized in address_to_rows:
            address_to_rows[normalized].append(index)
        else:
            address_to_rows[normalized] = [index]

    cache = load_cache(args.cache_path)
    unresolved = []
    for address in address_to_rows.keys():
        cached = cache.get(address)
        if cached is None:
            unresolved.append(address)
            continue
        latitude = cached.get("latitude", "")
        longitude = cached.get("longitude", "")
        if latitude == "" and longitude == "":
            unresolved.append(address)

    print(f"[입력] rows={len(rows)} unique_addresses={len(address_to_rows)}")
    print(f"[캐시] cached={len(cache)} unresolved={len(unresolved)}")

    request_interval_sec = 1.0 / args.request_per_second if args.request_per_second > 0 else 0.0
    last_request_at = [0.0]

    failed_rows: List[Dict[str, str]] = []
    resolved_count = 0
    for address, geo in cache.items():
        apply_geo_to_rows(
            rows,
            address_to_rows,
            address,
            geo.get("latitude", ""),
            geo.get("longitude", ""),
        )

    for index, address in enumerate(unresolved, start=1):
        try:
            latitude, longitude = request_geocode(
                api_key=api_key,
                query=address,
                timeout=args.timeout,
                retries=args.retries,
                request_interval_sec=request_interval_sec,
                last_request_at=last_request_at,
            )
            cache[address] = {"latitude": latitude, "longitude": longitude}
            apply_geo_to_rows(rows, address_to_rows, address, latitude, longitude)
            resolved_count += 1
        except Exception as error:
            failed_rows.append({"address": address, "reason": str(error)})

        if index % flush_every == 0 or index == len(unresolved):
            save_cache(args.cache_path, cache)
            write_rows(output_path, fieldnames, rows)
            write_failed(args.failed_path, failed_rows)
            print(
                f"[지오코딩] {index}/{len(unresolved)} 완료 (성공 {resolved_count}, 실패 {len(failed_rows)}) / flush"
            )

    if input_path == output_path and args.backup:
        backup_path = f"{input_path}.bak"
        shutil.copyfile(input_path, backup_path)
        print(f"[백업] {backup_path}")

    save_cache(args.cache_path, cache)
    write_rows(output_path, fieldnames, rows)
    write_failed(args.failed_path, failed_rows)

    print(f"[완료] output={output_path}")
    print(f"[완료] failed={len(failed_rows)} cache={args.cache_path}")


if __name__ == "__main__":
    main()
