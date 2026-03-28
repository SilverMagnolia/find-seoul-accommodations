import argparse
import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


VILLAS_API_URL = "https://apis.zigbang.com/house/property/v1/items/villas"
ITEMS_LIST_API_URL = "https://apis.zigbang.com/house/property/v1/items/list"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.zigbang.com/home/villa/map",
}
RENT_SALES_TYPES = {"월세", "전세"}
BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


class ApiRequestError(Exception):
    def __init__(self, message: str, status_code: int, response_body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


@dataclass
class VillaPoint:
    item_id: int
    lat: float
    lng: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="좌표 기반으로 직방 빌라 전월세 매물 존재 여부를 CSV에 추가"
    )
    parser.add_argument("--input", required=True, help="입력 CSV 경로")
    parser.add_argument(
        "--output",
        default="",
        help="출력 CSV 경로 (기본값: 입력파일명-with-zigbang-listing.csv)",
    )
    parser.add_argument(
        "--geohash-precision",
        type=int,
        default=5,
        help="직방 조회 geohash precision (기본값: 5)",
    )
    parser.add_argument(
        "--max-distance-meter",
        type=float,
        default=30.0,
        help="같은 건물 판정 최대 거리(m) (기본값: 30)",
    )
    parser.add_argument(
        "--item-id-batch-size",
        type=int,
        default=100,
        help="items/list 요청 시 itemIds 배치 크기 (기본값: 100)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP 요청 타임아웃(초)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="HTTP 재시도 횟수",
    )
    parser.add_argument(
        "--request-per-second",
        type=float,
        default=6.0,
        help="초당 최대 요청 수",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=0,
        help="앞에서 N건만 샘플 실행 (0이면 전체)",
    )
    return parser.parse_args()


def build_output_path(input_path: str, output_path: str) -> str:
    if output_path != "":
        return output_path
    source = Path(input_path)
    return str(source.with_name(f"{source.stem}-with-zigbang-listing{source.suffix}"))


def read_csv_rows(path: str) -> Tuple[List[Dict[str, str]], List[str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        fieldnames = reader.fieldnames if reader.fieldnames is not None else []
    return rows, fieldnames


def ensure_output_fields(fieldnames: List[str]) -> List[str]:
    result = list(fieldnames)
    additional = [
        "zigbang_listing_exists",
        "zigbang_match_count",
        "zigbang_nearest_distance_m",
        "zigbang_matched_sales_types",
        "zigbang_geohash",
        "zigbang_checked_at",
    ]
    for name in additional:
        if name not in result:
            result.append(name)
    return result


def write_rows(path: str, fieldnames: List[str], rows: List[Dict[str, str]]) -> None:
    output = Path(path)
    if output.parent.exists() is False:
        output.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def encode_geohash(lat: float, lng: float, precision: int) -> str:
    lat_interval = [-90.0, 90.0]
    lng_interval = [-180.0, 180.0]
    geohash: List[str] = []
    is_even = True
    bit = 0
    ch = 0
    bits = [16, 8, 4, 2, 1]

    while len(geohash) < precision:
        if is_even:
            midpoint = (lng_interval[0] + lng_interval[1]) / 2
            if lng >= midpoint:
                ch |= bits[bit]
                lng_interval[0] = midpoint
            else:
                lng_interval[1] = midpoint
        else:
            midpoint = (lat_interval[0] + lat_interval[1]) / 2
            if lat >= midpoint:
                ch |= bits[bit]
                lat_interval[0] = midpoint
            else:
                lat_interval[1] = midpoint
        is_even = not is_even
        if bit < 4:
            bit += 1
        else:
            geohash.append(BASE32[ch])
            bit = 0
            ch = 0
    return "".join(geohash)


def distance_meter(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    earth_radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_m * c


def throttle(last_request_at: List[float], request_interval_sec: float) -> None:
    wait_sec = request_interval_sec - (time.time() - last_request_at[0])
    if wait_sec > 0:
        time.sleep(wait_sec)


def http_get_json(
    url: str,
    timeout: float,
    retries: int,
    request_interval_sec: float,
    last_request_at: List[float],
) -> Dict:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            throttle(last_request_at, request_interval_sec)
            req = Request(url, headers=DEFAULT_HEADERS, method="GET")
            with urlopen(req, timeout=timeout) as response:
                last_request_at[0] = time.time()
                body = response.read().decode("utf-8")
                return json.loads(body)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            last_request_at[0] = time.time()
            last_error = error
            if attempt + 1 != retries:
                time.sleep(min(1.5 ** attempt, 5.0))
    if last_error is None:
        raise RuntimeError("GET 요청 실패: 원인 불명")
    raise RuntimeError(f"GET 요청 실패: {url}") from last_error


def http_post_json(
    url: str,
    payload: Dict,
    timeout: float,
    retries: int,
    request_interval_sec: float,
    last_request_at: List[float],
) -> Dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = dict(DEFAULT_HEADERS)
    headers["Content-Type"] = "application/json"
    last_error: Optional[Exception] = None

    for attempt in range(retries):
        try:
            throttle(last_request_at, request_interval_sec)
            req = Request(url, data=body, headers=headers, method="POST")
            with urlopen(req, timeout=timeout) as response:
                last_request_at[0] = time.time()
                response_body = response.read().decode("utf-8")
                return json.loads(response_body)
        except HTTPError as error:
            last_request_at[0] = time.time()
            status_code = error.code
            response_body = ""
            try:
                response_body = error.read().decode("utf-8", errors="ignore")
            except Exception:
                response_body = ""
            if status_code == 400:
                raise ApiRequestError(
                    message=f"POST 요청 400: {url}",
                    status_code=status_code,
                    response_body=response_body,
                ) from error
            last_error = error
            if attempt + 1 != retries:
                time.sleep(min(1.5 ** attempt, 5.0))
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            last_request_at[0] = time.time()
            last_error = error
            if attempt + 1 != retries:
                time.sleep(min(1.5 ** attempt, 5.0))
    if last_error is None:
        raise RuntimeError("POST 요청 실패: 원인 불명")
    raise RuntimeError(f"POST 요청 실패: {url}") from last_error


def fetch_villas_by_geohash(
    geohash: str,
    timeout: float,
    retries: int,
    request_interval_sec: float,
    last_request_at: List[float],
) -> List[VillaPoint]:
    query = urlencode(
        {
            "geohash": geohash,
            "salesPriceMin": 0,
            "depositMin": 0,
            "rentMin": 0,
        }
    )
    url = f"{VILLAS_API_URL}?{query}"
    payload = http_get_json(
        url=url,
        timeout=timeout,
        retries=retries,
        request_interval_sec=request_interval_sec,
        last_request_at=last_request_at,
    )
    items = payload.get("items", [])
    result: List[VillaPoint] = []
    for item in items:
        item_id = item.get("id")
        lat = item.get("lat")
        lng = item.get("lng")
        if isinstance(item_id, int) and isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            result.append(VillaPoint(item_id=item_id, lat=float(lat), lng=float(lng)))
    return result


def batched(values: List[int], size: int) -> List[List[int]]:
    result: List[List[int]] = []
    index = 0
    while index < len(values):
        result.append(values[index : index + size])
        index += size
    return result


def fetch_sales_type_map(
    item_ids: List[int],
    batch_size: int,
    timeout: float,
    retries: int,
    request_interval_sec: float,
    last_request_at: List[float],
) -> Dict[int, str]:
    sales_type_by_id: Dict[int, str] = {}

    def fetch_chunk(chunk: List[int]) -> None:
        payload = {"itemIds": chunk}
        try:
            data = http_post_json(
                url=ITEMS_LIST_API_URL,
                payload=payload,
                timeout=timeout,
                retries=retries,
                request_interval_sec=request_interval_sec,
                last_request_at=last_request_at,
            )
            items = data.get("items", [])
            for item in items:
                item_id = item.get("item_id")
                sales_type = item.get("sales_type")
                status = item.get("status")
                if isinstance(item_id, int) and isinstance(sales_type, str) and status is True:
                    sales_type_by_id[item_id] = sales_type
        except ApiRequestError:
            if len(chunk) == 1:
                return
            midpoint = len(chunk) // 2
            left = chunk[:midpoint]
            right = chunk[midpoint:]
            if len(left) > 0:
                fetch_chunk(left)
            if len(right) > 0:
                fetch_chunk(right)

    batches = batched(item_ids, batch_size)
    for chunk in batches:
        fetch_chunk(chunk)

    return sales_type_by_id


def parse_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> None:
    args = parse_args()
    input_path = args.input
    output_path = build_output_path(input_path, args.output)

    rows, original_fieldnames = read_csv_rows(input_path)
    if len(rows) == 0:
        raise SystemExit("입력 CSV에 데이터가 없습니다.")

    if args.sample_limit > 0:
        rows = rows[: args.sample_limit]

    fieldnames = ensure_output_fields(original_fieldnames)
    request_interval_sec = 0.0
    if args.request_per_second > 0:
        request_interval_sec = 1.0 / args.request_per_second
    last_request_at = [0.0]

    geohash_to_row_indexes: Dict[str, List[int]] = {}
    row_coordinates: Dict[int, Tuple[float, float]] = {}
    skipped_count = 0
    for index, row in enumerate(rows):
        lat = parse_float(row.get("latitude", ""))
        lng = parse_float(row.get("longitude", ""))
        if lat is None or lng is None:
            skipped_count += 1
            row["zigbang_listing_exists"] = ""
            row["zigbang_match_count"] = ""
            row["zigbang_nearest_distance_m"] = ""
            row["zigbang_matched_sales_types"] = ""
            row["zigbang_geohash"] = ""
            row["zigbang_checked_at"] = ""
            continue
        geohash = encode_geohash(lat, lng, args.geohash_precision)
        row_coordinates[index] = (lat, lng)
        row["zigbang_geohash"] = geohash
        if geohash in geohash_to_row_indexes:
            geohash_to_row_indexes[geohash].append(index)
        else:
            geohash_to_row_indexes[geohash] = [index]

    print(
        f"[입력] rows={len(rows)} geohashes={len(geohash_to_row_indexes)} skipped_no_latlng={skipped_count}",
        flush=True,
    )

    geohash_to_villas: Dict[str, List[VillaPoint]] = {}
    geohash_to_sales_type_map: Dict[str, Dict[int, str]] = {}

    geohash_items = list(geohash_to_row_indexes.items())
    for order, (geohash, _) in enumerate(geohash_items, start=1):
        villas = fetch_villas_by_geohash(
            geohash=geohash,
            timeout=args.timeout,
            retries=args.retries,
            request_interval_sec=request_interval_sec,
            last_request_at=last_request_at,
        )
        geohash_to_villas[geohash] = villas
        unique_ids = list({v.item_id for v in villas})
        sales_type_map: Dict[int, str] = {}
        if len(unique_ids) > 0:
            sales_type_map = fetch_sales_type_map(
                item_ids=unique_ids,
                batch_size=args.item_id_batch_size,
                timeout=args.timeout,
                retries=args.retries,
                request_interval_sec=request_interval_sec,
                last_request_at=last_request_at,
            )
        geohash_to_sales_type_map[geohash] = sales_type_map
        if order % 20 == 0 or order == len(geohash_items):
            print(
                f"[조회] geohash {order}/{len(geohash_items)} 완료 "
                f"(villas={len(villas)} details={len(sales_type_map)})",
                flush=True,
            )

    checked_at = datetime.now(timezone.utc).isoformat()
    positive_count = 0
    for index, row in enumerate(rows):
        coords = row_coordinates.get(index)
        if coords is None:
            continue
        lat, lng = coords
        geohash = row.get("zigbang_geohash", "")
        villas = geohash_to_villas.get(geohash, [])
        sales_type_map = geohash_to_sales_type_map.get(geohash, {})

        matched_sales_types: Set[str] = set()
        matched_count = 0
        nearest_distance: Optional[float] = None
        for villa in villas:
            dist = distance_meter(lat, lng, villa.lat, villa.lng)
            if nearest_distance is None or dist < nearest_distance:
                nearest_distance = dist
            if dist <= args.max_distance_meter:
                sales_type = sales_type_map.get(villa.item_id)
                if sales_type in RENT_SALES_TYPES:
                    matched_sales_types.add(sales_type)
                    matched_count += 1

        exists = matched_count > 0
        if exists:
            positive_count += 1

        row["zigbang_listing_exists"] = "true" if exists else "false"
        row["zigbang_match_count"] = str(matched_count)
        row["zigbang_nearest_distance_m"] = (
            f"{nearest_distance:.2f}" if nearest_distance is not None else ""
        )
        row["zigbang_matched_sales_types"] = ",".join(sorted(matched_sales_types))
        row["zigbang_checked_at"] = checked_at

    write_rows(output_path, fieldnames, rows)
    print(f"[완료] output={output_path}", flush=True)
    print(f"[완료] listing_exists_true={positive_count} / {len(rows)}", flush=True)


if __name__ == "__main__":
    main()
