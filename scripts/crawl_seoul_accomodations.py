import argparse
import csv
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Tuple

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://safestay.visitkorea.or.kr"
LIST_URL = f"{BASE_URL}/usr/mbkinfo/map/mapSelectDetail.kto"
DETAIL_URL = f"{BASE_URL}/usr/mbkinfo/mbkinfo/mbkInfoSelectDetail.kto"

CSV_HEADERS = [
    "업소명",
    "민박업소형태",
    "주소",
    "지방행정데이터인허가번호",
    "지방행정데이터인허가일자",
    "영업상태",
]

DETAIL_LABEL_MAP = {
    "민박업소명": "업소명",
    "민박업소형태": "민박업소형태",
    "주소": "주소",
    "지방행정데이터인허가번호": "지방행정데이터인허가번호",
    "지방행정데이터인허가일자": "지방행정데이터인허가일자",
    "영업상태": "영업상태",
}

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/usr/mbkinfo/map/mapSelectDetail.kto",
}

thread_local = threading.local()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def default_output_path() -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%d")
    return os.path.join("crawling-data", "seoul-accomodations", f"{timestamp}.csv")


def get_session() -> requests.Session:
    session = getattr(thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        thread_local.session = session
    return session


def retry_post(url: str, data: Dict[str, str], timeout: int, retries: int) -> str:
    last_error = None
    for attempt in range(retries):
        try:
            response = get_session().post(url, data=data, timeout=timeout)
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text
        except Exception as error:
            last_error = error
            if attempt + 1 != retries:
                time.sleep(min(0.5 * (2**attempt), 3.0))
    raise RuntimeError(f"POST 실패: {url} / {data}") from last_error


def list_payload(page_index: int) -> Dict[str, str]:
    return {
        "currentMenuSn": "105",
        "searchGubun1": "STD01",
        "searchGubun2": "",
        "searchGubun3": "2",
        "searchGubun4": "",
        "searchLodgeSidoCd": "STD01",
        "searchLodgeGugunCd": "",
        "lodgeSidoCdNm": "서울특별시",
        "lodgeGugunCdNm": "",
        "mcdVal": "",
        "lodgeSn": "",
        "pageIndex": str(page_index),
        "searchText": "",
    }


def detail_payload(lodge_sn: str) -> Dict[str, str]:
    return {
        "currentMenuSn": "16",
        "pageIndex": "1",
        "pageUnit": "10",
        "searchGubun": "",
        "searchText": "",
        "searchLodgeTypeCd": "",
        "searchKqmark": "",
        "searchLodgeStateCd": "",
        "searchLodgeSidoCd": "STD01",
        "searchLodgeGugunCd": "",
        "searchLodgePermitStdDy": "",
        "searchLodgePermitEndDy": "",
        "lodgeSn": lodge_sn,
        "lodgeSidoCdNm": "서울특별시",
        "lodgeGugunCdNm": "",
    }


def parse_total_pages(html: str) -> Tuple[int, int]:
    count_match = re.search(r"총 게시물\s*<strong>\s*([\d,]+)\s*</strong>", html)
    page_match = re.search(r"/\s*(\d+)page", html)
    total_count = int(count_match.group(1).replace(",", "")) if count_match else 0
    total_pages = int(page_match.group(1)) if page_match else 0
    return total_count, total_pages


def parse_lodge_ids(html: str) -> List[str]:
    ids = re.findall(r"fnSelectDetail\((\d+)\)", html)
    unique_ids = list(dict.fromkeys(ids))
    return unique_ids


def parse_detail_html(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    result = {header: "" for header in CSV_HEADERS}
    container = soup.select_one(".check-info-box ul")
    if container is None:
        return result

    for li in container.select("li"):
        label_tag = li.find("strong")
        value_tag = li.find("p")
        if label_tag is None or value_tag is None:
            continue
        label = normalize_text(label_tag.get_text(" ", strip=True))
        value = normalize_text(value_tag.get_text(" ", strip=True))
        mapped_key = DETAIL_LABEL_MAP.get(label, "")
        if mapped_key != "":
            result[mapped_key] = value

    return result


def fetch_all_lodge_ids(
    timeout: int, retries: int, list_workers: int
) -> Tuple[List[str], int, int]:
    first_html = retry_post(LIST_URL, list_payload(1), timeout=timeout, retries=retries)
    total_count, total_pages = parse_total_pages(first_html)
    if total_pages == 0:
        raise RuntimeError("총 페이지 수를 파싱하지 못했습니다.")

    all_ids: List[str] = parse_lodge_ids(first_html)
    print(f"[목록] 1/{total_pages} 페이지 수집 완료 (누적 {len(all_ids)}개)", flush=True)

    if total_pages >= 2:
        with ThreadPoolExecutor(max_workers=list_workers) as executor:
            future_map = {
                executor.submit(
                    retry_post,
                    LIST_URL,
                    list_payload(page),
                    timeout,
                    retries,
                ): page
                for page in range(2, total_pages + 1)
            }
            done_count = 1
            for future in as_completed(future_map):
                done_count += 1
                html = future.result()
                ids = parse_lodge_ids(html)
                all_ids.extend(ids)
                if done_count % 50 == 0 or done_count == total_pages:
                    print(
                        f"[목록] {done_count}/{total_pages} 페이지 수집 완료 "
                        f"(누적 {len(all_ids)}개)",
                        flush=True,
                    )

    unique_ids = list(dict.fromkeys(all_ids))
    return unique_ids, total_count, total_pages


def fetch_one_detail(lodge_sn: str, timeout: int, retries: int) -> Dict[str, str]:
    html = retry_post(DETAIL_URL, detail_payload(lodge_sn), timeout=timeout, retries=retries)
    return parse_detail_html(html)


def fetch_all_details(
    lodge_ids: List[str], workers: int, timeout: int, retries: int
) -> Tuple[List[Dict[str, str]], List[str]]:
    results: List[Dict[str, str]] = []
    failed: List[str] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(fetch_one_detail, lodge_sn, timeout, retries): lodge_sn
            for lodge_sn in lodge_ids
        }
        done_count = 0
        total = len(lodge_ids)
        for future in as_completed(future_map):
            lodge_sn = future_map[future]
            done_count += 1
            try:
                detail = future.result()
                results.append(detail)
            except Exception:
                failed.append(lodge_sn)
            if done_count % 200 == 0 or done_count == total:
                print(
                    f"[상세] {done_count}/{total} 완료 "
                    f"(성공 {len(results)} / 실패 {len(failed)})",
                    flush=True,
                )

    return results, failed


def write_csv(output_path: str, rows: List[Dict[str, str]]) -> None:
    output_dir = os.path.dirname(output_path)
    if output_dir != "":
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_HEADERS})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="서울 민박업소(세이프스테이) 상세정보 크롤링 후 CSV 저장"
    )
    parser.add_argument(
        "--output",
        default=default_output_path(),
        help=(
            "출력 CSV 파일 경로 "
            "(기본값: crawling-data/seoul-accomodations/yyyymmdd-HHmmdd.csv)"
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=24, help="상세 요청 병렬 스레드 수 (기본값: 24)"
    )
    parser.add_argument(
        "--list-workers",
        type=int,
        default=32,
        help="목록 요청 병렬 스레드 수 (기본값: 32)",
    )
    parser.add_argument("--timeout", type=int, default=20, help="요청 타임아웃(초)")
    parser.add_argument("--retries", type=int, default=4, help="요청 재시도 횟수")
    args = parser.parse_args()

    started_at = time.time()
    print("[시작] 서울 민박 목록 수집", flush=True)
    lodge_ids, total_count, total_pages = fetch_all_lodge_ids(
        timeout=args.timeout,
        retries=args.retries,
        list_workers=args.list_workers,
    )
    print(
        f"[목록 완료] 사이트 표기 건수 {total_count}건 / "
        f"수집한 고유 lodgeSn {len(lodge_ids)}개 / 총 페이지 {total_pages}",
        flush=True,
    )

    print(f"[시작] 상세 수집 (workers={args.workers})", flush=True)
    rows, failed_ids = fetch_all_details(
        lodge_ids,
        workers=args.workers,
        timeout=args.timeout,
        retries=args.retries,
    )

    if len(failed_ids) > 0:
        print(f"[재시도] 실패 {len(failed_ids)}건 단건 재수집 시도", flush=True)
        recovered = 0
        for lodge_sn in failed_ids:
            try:
                rows.append(fetch_one_detail(lodge_sn, timeout=args.timeout, retries=args.retries))
                recovered += 1
            except Exception:
                pass
        print(
            f"[재시도 완료] 복구 {recovered}건 / 미복구 {len(failed_ids) - recovered}건",
            flush=True,
        )

    write_csv(args.output, rows)
    elapsed = time.time() - started_at

    print(f"[완료] CSV 저장: {args.output}", flush=True)
    print(f"[완료] 총 레코드: {len(rows)}", flush=True)
    print(f"[완료] 소요시간: {elapsed:.1f}초", flush=True)


if __name__ == "__main__":
    main()
