import argparse
import csv
import json
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set
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
TARGET_SALES_TYPES = {"전세", "월세"}
BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


class ApiRequestError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimiter:
    def __init__(self, request_per_second: float) -> None:
        self.interval_sec = 0.0
        if request_per_second > 0:
            self.interval_sec = 1.0 / request_per_second
        self.lock = threading.Lock()
        self.last_request_at = 0.0

    def wait(self) -> None:
        if self.interval_sec == 0.0:
            return
        with self.lock:
            now = time.time()
            wait_sec = self.interval_sec - (now - self.last_request_at)
            if wait_sec > 0:
                time.sleep(wait_sec)
            self.last_request_at = time.time()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="직방 API로 건물 단위 전월세 매물 매칭(전체/샘플) 후 matched/unmatched 저장"
    )
    parser.add_argument("--input", required=True, help="입력 CSV 경로")
    parser.add_argument("--output-root", default="output", help="결과 디렉터리 루트")
    parser.add_argument("--sample-size", type=int, default=0, help="랜덤 샘플 크기 (0이면 전체)")
    parser.add_argument("--seed", type=int, default=42, help="sample-size 사용 시 샘플 고정 시드")
    parser.add_argument("--geohash-precision", type=int, default=5, help="geohash precision")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP 타임아웃(초)")
    parser.add_argument("--retries", type=int, default=3, help="재시도 횟수")
    parser.add_argument("--request-per-second", type=float, default=6.0, help="전역 초당 요청 수")
    parser.add_argument("--villas-workers", type=int, default=8, help="villas 병렬 worker 수")
    parser.add_argument("--list-workers", type=int, default=3, help="items/list 병렬 worker 수")
    parser.add_argument("--item-id-batch-size", type=int, default=100, help="itemIds 배치 크기")
    parser.add_argument("--flush-every", type=int, default=30, help="N건마다 flush")
    parser.add_argument("--distance-b", type=float, default=20.0, help="B 매칭 거리 임계치(m)")
    parser.add_argument("--distance-c", type=float, default=12.0, help="C 매칭 거리 임계치(m)")
    parser.add_argument(
        "--disable-neighbor-fallback",
        action="store_true",
        help="미매칭 시 인접 geohash 재조회 비활성화",
    )
    return parser.parse_args()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_seoul_alias(value: str) -> str:
    normalized = normalize_space(value)
    if normalized == "서울특별시" or normalized == "서울시" or normalized == "서울":
        return "서울"
    return normalized


def strip_seoul_prefix(value: str) -> str:
    result = re.sub(r"^\s*서울(?:특별시|시)?\s+", "서울 ", value)
    return normalize_space(result)


def split_jibun_number(number_text: str) -> Dict[str, str]:
    number_text_normalized = normalize_space(number_text)
    if number_text_normalized == "":
        return {"main_no": "", "sub_no": ""}
    if "-" in number_text_normalized:
        parts = number_text_normalized.split("-", 1)
        return {"main_no": normalize_space(parts[0]), "sub_no": normalize_space(parts[1])}
    return {"main_no": number_text_normalized, "sub_no": ""}


def parse_jibun_components_from_text(jibun: str) -> Dict[str, str]:
    normalized = strip_seoul_prefix(jibun)
    tokens = normalized.split(" ")
    if len(tokens) < 3:
        return {
            "local1": "서울",
            "local2": "",
            "local3": "",
            "main_no": "",
            "sub_no": "",
            "raw": normalized,
        }

    number_token = ""
    for token in tokens[::-1]:
        if re.match(r"^\d+(?:-\d+)?$", token) is not None:
            number_token = token
            break

    local1 = ""
    local2 = ""
    local3 = ""
    for token in tokens:
        if local1 == "":
            local1 = normalize_seoul_alias(token)
            if local1 == "서울":
                continue
        if local2 == "" and token.endswith("구"):
            local2 = normalize_space(token)
            continue
        if local2 != "" and local3 == "":
            local3 = normalize_space(token)
            break

    if local1 == "":
        local1 = "서울"
    number_parts = split_jibun_number(number_token)
    return {
        "local1": local1,
        "local2": local2,
        "local3": local3,
        "main_no": number_parts["main_no"],
        "sub_no": number_parts["sub_no"],
        "raw": normalized,
    }


def parse_jibun_components_from_item(item: Dict) -> Dict[str, str]:
    address_origin = item.get("addressOrigin")
    if isinstance(address_origin, dict) is False:
        return {
            "local1": "",
            "local2": "",
            "local3": "",
            "main_no": "",
            "sub_no": "",
            "raw": "",
        }
    local1 = address_origin.get("local1")
    local2 = address_origin.get("local2")
    local3 = address_origin.get("local3")
    address2 = address_origin.get("address2")
    if (
        isinstance(local1, str) is False
        or isinstance(local2, str) is False
        or isinstance(local3, str) is False
        or isinstance(address2, str) is False
    ):
        return {
            "local1": "",
            "local2": "",
            "local3": "",
            "main_no": "",
            "sub_no": "",
            "raw": "",
        }
    number_parts = split_jibun_number(address2)
    return {
        "local1": normalize_seoul_alias(local1),
        "local2": normalize_space(local2),
        "local3": normalize_space(local3),
        "main_no": number_parts["main_no"],
        "sub_no": number_parts["sub_no"],
        "raw": f"{normalize_seoul_alias(local1)}|{normalize_space(local2)}|{normalize_space(local3)}|{normalize_space(address2)}",
    }


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


def decode_geohash_bbox(geohash: str) -> Dict[str, float]:
    lat_interval = [-90.0, 90.0]
    lng_interval = [-180.0, 180.0]
    is_even = True
    bits = [16, 8, 4, 2, 1]
    for char in geohash:
        ch = BASE32.find(char)
        if ch < 0:
            raise ValueError(f"잘못된 geohash 문자: {char}")
        for mask in bits:
            if is_even:
                midpoint = (lng_interval[0] + lng_interval[1]) / 2
                if ch & mask:
                    lng_interval[0] = midpoint
                else:
                    lng_interval[1] = midpoint
            else:
                midpoint = (lat_interval[0] + lat_interval[1]) / 2
                if ch & mask:
                    lat_interval[0] = midpoint
                else:
                    lat_interval[1] = midpoint
            is_even = not is_even
    return {
        "center_lat": (lat_interval[0] + lat_interval[1]) / 2,
        "center_lng": (lng_interval[0] + lng_interval[1]) / 2,
        "lat_step": lat_interval[1] - lat_interval[0],
        "lng_step": lng_interval[1] - lng_interval[0],
    }


def build_neighbor_geohashes(geohash: str) -> List[str]:
    bbox = decode_geohash_bbox(geohash)
    precision = len(geohash)
    neighbors: List[str] = []
    for lat_offset in [-1, 0, 1]:
        for lng_offset in [-1, 0, 1]:
            if lat_offset == 0 and lng_offset == 0:
                continue
            target_lat = bbox["center_lat"] + (bbox["lat_step"] * lat_offset)
            target_lng = bbox["center_lng"] + (bbox["lng_step"] * lng_offset)
            candidate = encode_geohash(target_lat, target_lng, precision)
            if candidate != geohash and candidate not in neighbors:
                neighbors.append(candidate)
    return neighbors


def haversine_meter(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
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


def http_get_json(url: str, timeout: float, retries: int, limiter: RateLimiter) -> Dict:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            limiter.wait()
            req = Request(url, headers=DEFAULT_HEADERS, method="GET")
            with urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt + 1 != retries:
                time.sleep(min(1.5 ** attempt, 5.0))
    if last_error is None:
        raise RuntimeError(f"GET 요청 실패: {url}")
    raise RuntimeError(f"GET 요청 실패: {url}") from last_error


def http_post_json(url: str, payload: Dict, timeout: float, retries: int, limiter: RateLimiter) -> Dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = dict(DEFAULT_HEADERS)
    headers["Content-Type"] = "application/json"
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            limiter.wait()
            req = Request(url, data=body, headers=headers, method="POST")
            with urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code == 400:
                raise ApiRequestError(f"POST 요청 400: {url}", error.code) from error
            last_error = error
            if attempt + 1 != retries:
                time.sleep(min(1.5 ** attempt, 5.0))
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt + 1 != retries:
                time.sleep(min(1.5 ** attempt, 5.0))
    if last_error is None:
        raise RuntimeError(f"POST 요청 실패: {url}")
    raise RuntimeError(f"POST 요청 실패: {url}") from last_error


def batched(values: List[int], size: int) -> List[List[int]]:
    result: List[List[int]] = []
    index = 0
    while index < len(values):
        result.append(values[index : index + size])
        index += size
    return result


def fetch_villas_by_geohash(geohash: str, timeout: float, retries: int, limiter: RateLimiter) -> Dict:
    params = {
        "geohash": geohash,
        "salesPriceMin": 0,
        "depositMin": 0,
        "rentMin": 0,
    }
    url = f"{VILLAS_API_URL}?{urlencode(params)}"
    payload = http_get_json(url=url, timeout=timeout, retries=retries, limiter=limiter)
    item_ids: Set[int] = set()
    items = payload.get("items", [])
    if isinstance(items, list):
        for item in items:
            item_id = item.get("id")
            if isinstance(item_id, int):
                item_ids.add(item_id)
    return {"villas_item_count": len(item_ids), "item_ids": sorted(list(item_ids))}


def fetch_items_details_for_geohash(
    geohash: str,
    item_ids: List[int],
    batch_size: int,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> Dict:
    details: List[Dict] = []
    for chunk in batched(item_ids, batch_size):
        stack: List[List[int]] = [chunk]
        while len(stack) > 0:
            current = stack.pop()
            payload = {"itemIds": current}
            try:
                data = http_post_json(
                    url=ITEMS_LIST_API_URL,
                    payload=payload,
                    timeout=timeout,
                    retries=retries,
                    limiter=limiter,
                )
                items = data.get("items", [])
                if isinstance(items, list):
                    for item in items:
                        item_id = item.get("item_id")
                        sales_type = item.get("sales_type")
                        status = item.get("status")
                        if isinstance(item_id, int) is False or isinstance(sales_type, str) is False:
                            continue
                        if status is not True:
                            continue
                        components = parse_jibun_components_from_item(item)
                        if components.get("local2", "") == "" or components.get("local3", "") == "":
                            continue
                        location = item.get("location")
                        location_lat = None
                        location_lng = None
                        if isinstance(location, dict):
                            lat_value = location.get("lat")
                            lng_value = location.get("lng")
                            if isinstance(lat_value, (int, float)) and isinstance(lng_value, (int, float)):
                                location_lat = float(lat_value)
                                location_lng = float(lng_value)
                        details.append(
                            {
                                "item_id": item_id,
                                "sales_type": sales_type,
                                "components": components,
                                "location_lat": location_lat,
                                "location_lng": location_lng,
                            }
                        )
            except ApiRequestError as error:
                if error.status_code != 400:
                    raise
                if len(current) == 1:
                    continue
                midpoint = len(current) // 2
                left = current[:midpoint]
                right = current[midpoint:]
                if len(left) > 0:
                    stack.append(left)
                if len(right) > 0:
                    stack.append(right)

    dedup: Dict[int, Dict] = {}
    for detail in details:
        item_id = detail.get("item_id")
        if isinstance(item_id, int):
            dedup[item_id] = detail
    return {"geohash": geohash, "details": list(dedup.values())}


def evaluate_match(
    row_components: Dict[str, str],
    row_lat: float,
    row_lng: float,
    item: Dict,
    distance_b: float,
    distance_c: float,
) -> Dict:
    components = item.get("components", {})
    if isinstance(components, dict) is False:
        return {"matched": False, "level": "", "reason": "ADDR_INCOMPLETE", "distance_m": None}
    row_local2 = row_components.get("local2", "")
    row_local3 = row_components.get("local3", "")
    row_main_no = row_components.get("main_no", "")
    row_sub_no = row_components.get("sub_no", "")
    item_local2 = components.get("local2", "")
    item_local3 = components.get("local3", "")
    item_main_no = components.get("main_no", "")
    item_sub_no = components.get("sub_no", "")
    if row_local2 == "" or row_local3 == "" or row_main_no == "":
        return {"matched": False, "level": "", "reason": "ADDR_INCOMPLETE", "distance_m": None}
    if item_local2 == "" or item_local3 == "" or item_main_no == "":
        return {"matched": False, "level": "", "reason": "ADDR_INCOMPLETE", "distance_m": None}

    distance_m = None
    item_lat = item.get("location_lat")
    item_lng = item.get("location_lng")
    if isinstance(item_lat, float) and isinstance(item_lng, float):
        distance_m = haversine_meter(row_lat, row_lng, item_lat, item_lng)

    if row_local2 == item_local2 and row_local3 == item_local3 and row_main_no == item_main_no:
        if row_sub_no != "" and item_sub_no != "" and row_sub_no == item_sub_no:
            return {"matched": True, "level": "A", "reason": "FULL_JIBUN_MATCH", "distance_m": distance_m}
        if row_sub_no == "" or item_sub_no == "":
            if distance_m is None or distance_m <= distance_b:
                return {
                    "matched": True,
                    "level": "B",
                    "reason": "MAIN_NO_MATCH_WITH_DISTANCE_B",
                    "distance_m": distance_m,
                }

    if row_local2 == item_local2 and row_local3 == item_local3:
        if distance_m is not None and distance_m <= distance_c:
            return {"matched": True, "level": "C", "reason": "LOCAL_MATCH_WITH_DISTANCE_C", "distance_m": distance_m}
        return {"matched": False, "level": "", "reason": "OUT_OF_DISTANCE", "distance_m": distance_m}
    return {"matched": False, "level": "", "reason": "ADDR_MISMATCH", "distance_m": distance_m}


def build_output_dir(output_root: str) -> Path:
    now_str = datetime.now().strftime("%Y-%m-%d %H-%M")
    output_dir = Path(output_root) / now_str
    if output_dir.exists() is False:
        output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def flush_outputs(
    output_dir: Path,
    matched: List[Dict],
    unmatched: List[Dict],
    geohash_cache: Dict[str, Dict],
    processed_count: int,
) -> None:
    with (output_dir / "matched.json").open("w", encoding="utf-8") as file:
        json.dump({"count": len(matched), "results": matched}, file, ensure_ascii=False, indent=2)
    with (output_dir / "unmatched.json").open("w", encoding="utf-8") as file:
        json.dump({"count": len(unmatched), "results": unmatched}, file, ensure_ascii=False, indent=2)
    with (output_dir / "geohash-cache.json").open("w", encoding="utf-8") as file:
        json.dump(geohash_cache, file, ensure_ascii=False, indent=2)
    with (output_dir / "progress.json").open("w", encoding="utf-8") as file:
        json.dump(
            {
                "processed_count": processed_count,
                "matched_count": len(matched),
                "unmatched_count": len(unmatched),
                "geohash_cache_count": len(geohash_cache),
                "updated_at": datetime.now().isoformat(),
            },
            file,
            ensure_ascii=False,
            indent=2,
        )


def prepare_target_rows(rows: List[Dict], sample_size: int, seed: int) -> List[int]:
    valid_indexes: List[int] = []
    for index, row in enumerate(rows):
        lat = row.get("latitude", "")
        lng = row.get("longitude", "")
        jibun = row.get("지번주소", "")
        if normalize_space(lat) == "" or normalize_space(lng) == "" or normalize_space(jibun) == "":
            continue
        valid_indexes.append(index)
    if sample_size > 0:
        random.seed(seed)
        size = sample_size
        if size > len(valid_indexes):
            size = len(valid_indexes)
        return random.sample(valid_indexes, size)
    return valid_indexes


def parallel_fetch_villas(
    geohashes: List[str],
    workers: int,
    args: argparse.Namespace,
    limiter: RateLimiter,
) -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(fetch_villas_by_geohash, geohash, args.timeout, args.retries, limiter): geohash
            for geohash in geohashes
        }
        done = 0
        total = len(geohashes)
        for future in as_completed(future_map):
            geohash = future_map[future]
            data = future.result()
            with lock:
                result[geohash] = {"villas_item_count": data["villas_item_count"], "item_ids": data["item_ids"], "details": []}
            done += 1
            if done % 5 == 0 or done == total:
                print(f"[phase-a:villas] {done}/{total}")
    return result


def parallel_fetch_list_details(
    geohash_to_item_ids: Dict[str, List[int]],
    workers: int,
    args: argparse.Namespace,
    limiter: RateLimiter,
) -> Dict[str, List[Dict]]:
    targets = [g for g, ids in geohash_to_item_ids.items() if len(ids) > 0]
    result: Dict[str, List[Dict]] = {}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                fetch_items_details_for_geohash,
                geohash,
                geohash_to_item_ids[geohash],
                args.item_id_batch_size,
                args.timeout,
                args.retries,
                limiter,
            ): geohash
            for geohash in targets
        }
        done = 0
        total = len(targets)
        for future in as_completed(future_map):
            geohash = future_map[future]
            data = future.result()
            with lock:
                result[geohash] = data.get("details", [])
            done += 1
            if done % 5 == 0 or done == total:
                print(f"[phase-a:list] {done}/{total}")
    return result


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    if input_path.exists() is False:
        raise SystemExit(f"입력 파일이 없습니다: {input_path}")

    with input_path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    target_indexes = prepare_target_rows(rows, args.sample_size, args.seed)
    if len(target_indexes) == 0:
        raise SystemExit("유효한 (위경도 + 지번주소) 행이 없습니다.")

    limiter = RateLimiter(args.request_per_second)
    output_dir = build_output_dir(args.output_root)

    row_to_geohash: Dict[int, str] = {}
    base_geohashes: List[str] = []
    for source_index in target_indexes:
        row = rows[source_index]
        geohash = encode_geohash(float(row["latitude"]), float(row["longitude"]), args.geohash_precision)
        row_to_geohash[source_index] = geohash
        if geohash not in base_geohashes:
            base_geohashes.append(geohash)

    print(f"[phase-a] base_geohash={len(base_geohashes)}")
    geohash_cache = parallel_fetch_villas(base_geohashes, args.villas_workers, args, limiter)

    base_item_ids_map: Dict[str, List[int]] = {}
    for geohash, data in geohash_cache.items():
        base_item_ids_map[geohash] = data.get("item_ids", [])
    base_details_map = parallel_fetch_list_details(base_item_ids_map, args.list_workers, args, limiter)
    for geohash, details in base_details_map.items():
        geohash_cache[geohash]["details"] = details

    matched: List[Dict] = []
    unmatched: List[Dict] = []
    phase_b_targets: List[int] = []

    total = len(target_indexes)
    for processed_count, source_index in enumerate(target_indexes, start=1):
        row = rows[source_index]
        geohash = row_to_geohash[source_index]
        dataset = geohash_cache.get(geohash, {"villas_item_count": 0, "details": []})
        details = dataset.get("details", [])
        if isinstance(details, list) is False:
            details = []
        row_components = parse_jibun_components_from_text(row.get("지번주소", ""))

        matched_items: List[Dict] = []
        matched_levels: Set[str] = set()
        matched_sales_types: Set[str] = set()
        nearest_distance_m: Optional[float] = None
        unmatched_reason_counts: Dict[str, int] = {}
        has_any_rent_item = False
        has_any_address_info = False

        for item in details:
            sales_type = item.get("sales_type")
            if isinstance(sales_type, str) is False:
                continue
            if sales_type not in TARGET_SALES_TYPES:
                continue
            has_any_rent_item = True
            components = item.get("components", {})
            if isinstance(components, dict):
                if components.get("local2", "") != "" and components.get("local3", "") != "":
                    has_any_address_info = True

            evaluation = evaluate_match(
                row_components=row_components,
                row_lat=float(row["latitude"]),
                row_lng=float(row["longitude"]),
                item=item,
                distance_b=args.distance_b,
                distance_c=args.distance_c,
            )
            reason = evaluation.get("reason", "")
            if isinstance(reason, str) and reason != "":
                previous = unmatched_reason_counts.get(reason, 0)
                unmatched_reason_counts[reason] = previous + 1
            distance_m = evaluation.get("distance_m")
            if isinstance(distance_m, float):
                if nearest_distance_m is None or distance_m < nearest_distance_m:
                    nearest_distance_m = distance_m
            if evaluation.get("matched") is not True:
                continue

            level = evaluation.get("level", "")
            if isinstance(level, str) and level != "":
                matched_levels.add(level)
            matched_sales_types.add(sales_type)
            matched_items.append(
                {
                    "item_id": item.get("item_id"),
                    "sales_type": sales_type,
                    "match_level": level,
                    "match_reason": evaluation.get("reason", ""),
                    "distance_m": distance_m,
                }
            )

        if len(matched_items) > 0:
            matched.append(
                {
                    "row_index": source_index,
                    "업소명": row.get("업소명", ""),
                    "주소": row.get("주소", ""),
                    "지번주소": row.get("지번주소", ""),
                    "latitude": row.get("latitude", ""),
                    "longitude": row.get("longitude", ""),
                    "geohash": geohash,
                    "villas_item_count": dataset.get("villas_item_count", 0),
                    "listing_exists": True,
                    "match_count": len(matched_items),
                    "match_levels": sorted(list(matched_levels)),
                    "matched_sales_types": sorted(list(matched_sales_types)),
                    "matched_items": matched_items,
                    "nearest_distance_m": nearest_distance_m,
                    "fallback_used": False,
                    "unmatched_code": "",
                }
            )
        else:
            if args.disable_neighbor_fallback is False:
                phase_b_targets.append(source_index)
            if dataset.get("villas_item_count", 0) == 0:
                unmatched_code = "NO_VILLAS"
            elif has_any_rent_item is False:
                unmatched_code = "NO_RENT_TYPE"
            elif has_any_address_info is False:
                unmatched_code = "ADDR_INCOMPLETE"
            elif unmatched_reason_counts.get("OUT_OF_DISTANCE", 0) > 0:
                unmatched_code = "OUT_OF_DISTANCE"
            else:
                unmatched_code = "ADDR_MISMATCH"
            unmatched.append(
                {
                    "row_index": source_index,
                    "업소명": row.get("업소명", ""),
                    "주소": row.get("주소", ""),
                    "지번주소": row.get("지번주소", ""),
                    "latitude": row.get("latitude", ""),
                    "longitude": row.get("longitude", ""),
                    "geohash": geohash,
                    "villas_item_count": dataset.get("villas_item_count", 0),
                    "listing_exists": False,
                    "match_count": 0,
                    "match_levels": [],
                    "matched_sales_types": [],
                    "matched_items": [],
                    "nearest_distance_m": nearest_distance_m,
                    "fallback_used": False,
                    "unmatched_code": unmatched_code,
                }
            )

        if processed_count % args.flush_every == 0 or processed_count == total:
            flush_outputs(output_dir, matched, unmatched, geohash_cache, processed_count)
            print(f"[flush-a] {processed_count}/{total} matched={len(matched)} unmatched={len(unmatched)}")

    if args.disable_neighbor_fallback is False and len(phase_b_targets) > 0:
        print(f"[phase-b] fallback_rows={len(phase_b_targets)}")
        neighbor_geohashes: List[str] = []
        for source_index in phase_b_targets:
            base_geohash = row_to_geohash[source_index]
            neighbors = build_neighbor_geohashes(base_geohash)
            for neighbor in neighbors:
                if neighbor not in geohash_cache and neighbor not in neighbor_geohashes:
                    neighbor_geohashes.append(neighbor)

        if len(neighbor_geohashes) > 0:
            print(f"[phase-b] neighbor_geohash={len(neighbor_geohashes)}")
            neighbor_cache = parallel_fetch_villas(neighbor_geohashes, args.villas_workers, args, limiter)
            for geohash, value in neighbor_cache.items():
                geohash_cache[geohash] = value
            neighbor_item_ids_map: Dict[str, List[int]] = {}
            for geohash in neighbor_geohashes:
                dataset = geohash_cache.get(geohash, {})
                neighbor_item_ids_map[geohash] = dataset.get("item_ids", [])
            neighbor_details_map = parallel_fetch_list_details(neighbor_item_ids_map, args.list_workers, args, limiter)
            for geohash, details in neighbor_details_map.items():
                geohash_cache[geohash]["details"] = details

        unmatched_map: Dict[int, Dict] = {entry["row_index"]: entry for entry in unmatched}
        updated_to_matched = 0
        for idx, source_index in enumerate(phase_b_targets, start=1):
            unmatched_entry = unmatched_map.get(source_index)
            if unmatched_entry is None:
                continue
            row = rows[source_index]
            base_geohash = row_to_geohash[source_index]
            detail_candidates: Dict[int, Dict] = {}
            geohashes_for_row = [base_geohash]
            geohashes_for_row.extend(build_neighbor_geohashes(base_geohash))
            villas_item_count_sum = 0
            for geohash in geohashes_for_row:
                dataset = geohash_cache.get(geohash)
                if isinstance(dataset, dict) is False:
                    continue
                villas_item_count_sum += int(dataset.get("villas_item_count", 0))
                details = dataset.get("details", [])
                if isinstance(details, list):
                    for item in details:
                        item_id = item.get("item_id")
                        if isinstance(item_id, int):
                            detail_candidates[item_id] = item

            row_components = parse_jibun_components_from_text(row.get("지번주소", ""))
            matched_items: List[Dict] = []
            matched_levels: Set[str] = set()
            matched_sales_types: Set[str] = set()
            nearest_distance_m: Optional[float] = None
            unmatched_reason_counts: Dict[str, int] = {}
            has_any_rent_item = False
            has_any_address_info = False
            for item in detail_candidates.values():
                sales_type = item.get("sales_type")
                if isinstance(sales_type, str) is False:
                    continue
                if sales_type not in TARGET_SALES_TYPES:
                    continue
                has_any_rent_item = True
                components = item.get("components", {})
                if isinstance(components, dict):
                    if components.get("local2", "") != "" and components.get("local3", "") != "":
                        has_any_address_info = True

                evaluation = evaluate_match(
                    row_components=row_components,
                    row_lat=float(row["latitude"]),
                    row_lng=float(row["longitude"]),
                    item=item,
                    distance_b=args.distance_b,
                    distance_c=args.distance_c,
                )
                reason = evaluation.get("reason", "")
                if isinstance(reason, str) and reason != "":
                    previous = unmatched_reason_counts.get(reason, 0)
                    unmatched_reason_counts[reason] = previous + 1
                distance_m = evaluation.get("distance_m")
                if isinstance(distance_m, float):
                    if nearest_distance_m is None or distance_m < nearest_distance_m:
                        nearest_distance_m = distance_m
                if evaluation.get("matched") is not True:
                    continue

                level = evaluation.get("level", "")
                if isinstance(level, str) and level != "":
                    matched_levels.add(level)
                matched_sales_types.add(sales_type)
                matched_items.append(
                    {
                        "item_id": item.get("item_id"),
                        "sales_type": sales_type,
                        "match_level": level,
                        "match_reason": evaluation.get("reason", ""),
                        "distance_m": distance_m,
                    }
                )

            if len(matched_items) > 0:
                updated_to_matched += 1
                matched.append(
                    {
                        "row_index": source_index,
                        "업소명": row.get("업소명", ""),
                        "주소": row.get("주소", ""),
                        "지번주소": row.get("지번주소", ""),
                        "latitude": row.get("latitude", ""),
                        "longitude": row.get("longitude", ""),
                        "geohash": base_geohash,
                        "villas_item_count": villas_item_count_sum,
                        "listing_exists": True,
                        "match_count": len(matched_items),
                        "match_levels": sorted(list(matched_levels)),
                        "matched_sales_types": sorted(list(matched_sales_types)),
                        "matched_items": matched_items,
                        "nearest_distance_m": nearest_distance_m,
                        "fallback_used": True,
                        "unmatched_code": "",
                    }
                )
                unmatched = [entry for entry in unmatched if entry.get("row_index") != source_index]
            else:
                if villas_item_count_sum == 0:
                    unmatched_code = "NO_VILLAS"
                elif has_any_rent_item is False:
                    unmatched_code = "NO_RENT_TYPE"
                elif has_any_address_info is False:
                    unmatched_code = "ADDR_INCOMPLETE"
                elif unmatched_reason_counts.get("OUT_OF_DISTANCE", 0) > 0:
                    unmatched_code = "OUT_OF_DISTANCE"
                else:
                    unmatched_code = "ADDR_MISMATCH"
                unmatched_entry["villas_item_count"] = villas_item_count_sum
                unmatched_entry["nearest_distance_m"] = nearest_distance_m
                unmatched_entry["fallback_used"] = True
                unmatched_entry["fallback_neighbor_count"] = 8
                unmatched_entry["unmatched_code"] = unmatched_code

            if idx % args.flush_every == 0 or idx == len(phase_b_targets):
                flush_outputs(output_dir, matched, unmatched, geohash_cache, total)
                print(f"[flush-b] {idx}/{len(phase_b_targets)} matched={len(matched)} unmatched={len(unmatched)}")

        print(f"[phase-b] newly_matched={updated_to_matched}")

    flush_outputs(output_dir, matched, unmatched, geohash_cache, total)
    print(f"[완료] output_dir={output_dir}")
    print(f"[완료] total={total} matched={len(matched)} unmatched={len(unmatched)}")


if __name__ == "__main__":
    main()
