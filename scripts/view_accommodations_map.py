import html
import re
from pathlib import Path
from urllib.parse import quote

import folium
import pandas as pd
import streamlit as st
from folium.plugins import FastMarkerCluster
from streamlit_folium import folium_static


DEFAULT_CSV_PATH = "crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv"


def extract_district(address: str) -> str:
    if address is None:
        return "미상"
    cleaned = str(address).strip()
    if cleaned == "":
        return "미상"

    match = re.search(r"(서울특별시|서울시)\s*([^\s,]+)", cleaned)
    if match is not None:
        district = match.group(2).strip()
        if district.endswith("구") is True:
            return district
    return "미상"


def build_popup_html(row: pd.Series) -> str:
    name = html.escape(str(row.get("업소명", "")))
    address = html.escape(str(row.get("주소", "")))
    status = html.escape(str(row.get("영업상태", "")))
    license_date = html.escape(str(row.get("지방행정데이터인허가일자", "")))
    lat = row.get("latitude", "")
    lng = row.get("longitude", "")

    lat_str = f"{lat:.7f}"
    lng_str = f"{lng:.7f}"
    kakao_map_link = f"https://map.kakao.com/link/map/{quote(name)},{lat_str},{lng_str}"
    kakao_search_link = f"https://map.kakao.com/link/search/{quote(address)}"
    naver_search_link = f"https://map.naver.com/p/search/{quote(address)}"

    return (
        f"<div style='width: 320px;'>"
        f"<b>{name}</b><br/>"
        f"<span>{address}</span><br/>"
        f"<span>상태: {status}</span><br/>"
        f"<span>인허가일자: {license_date}</span><br/>"
        f"<span>좌표: {lat_str}, {lng_str}</span><br/><br/>"
        f"<a href='{kakao_map_link}' target='_blank'>카카오맵(좌표)</a> | "
        f"<a href='{kakao_search_link}' target='_blank'>카카오맵(주소)</a> | "
        f"<a href='{naver_search_link}' target='_blank'>네이버지도(주소)</a>"
        f"</div>"
    )


@st.cache_data(show_spinner=False)
def load_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    if "latitude" not in df.columns or "longitude" not in df.columns:
        raise ValueError("CSV에 latitude, longitude 컬럼이 없습니다.")

    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df["주소"] = df["주소"].astype(str)
    df["업소명"] = df["업소명"].astype(str)
    df["영업상태"] = df["영업상태"].astype(str)
    df["구"] = df["주소"].apply(extract_district)
    return df


def build_map(filtered: pd.DataFrame) -> folium.Map:
    center_lat = float(filtered["latitude"].mean())
    center_lng = float(filtered["longitude"].mean())
    fmap = folium.Map(location=[center_lat, center_lng], zoom_start=11, tiles="OpenStreetMap")
    points = []
    for _, row in filtered.iterrows():
        points.append([float(row["latitude"]), float(row["longitude"]), build_popup_html(row)])

    callback = """
    function (row) {
        var marker = L.marker(new L.LatLng(row[0], row[1]));
        marker.bindPopup(row[2], {maxWidth: 360});
        return marker;
    };
    """
    FastMarkerCluster(data=points, callback=callback).add_to(fmap)
    return fmap


def main() -> None:
    st.set_page_config(page_title="서울 외국인관광도시민박업 지도", layout="wide")
    st.title("서울 외국인관광도시민박업 지도")
    st.caption("로컬 전용 뷰어 - CSV의 위경도 정보를 기반으로 지도에서 업체를 확인합니다.")

    default_path = str(Path(DEFAULT_CSV_PATH))
    csv_path = st.sidebar.text_input("CSV 경로", value=default_path)
    st.sidebar.markdown("예시: `crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv`")

    if Path(csv_path).exists() is False:
        st.error(f"CSV 파일을 찾을 수 없습니다: {csv_path}")
        st.stop()

    try:
        df = load_data(csv_path)
    except Exception as error:
        st.error(f"CSV 로드 실패: {error}")
        st.stop()

    valid = df[df["latitude"].notna() & df["longitude"].notna()].copy()
    if len(valid) == 0:
        st.warning("유효한 위경도 데이터가 없습니다.")
        st.stop()

    districts = sorted(valid["구"].dropna().unique().tolist())
    statuses = sorted(valid["영업상태"].dropna().unique().tolist())

    selected_districts = st.sidebar.multiselect("자치구", districts, default=districts)
    selected_statuses = st.sidebar.multiselect("영업상태", statuses, default=statuses)
    keyword = st.sidebar.text_input("업소명/주소 검색", value="").strip()

    filtered = valid[valid["구"].isin(selected_districts) & valid["영업상태"].isin(selected_statuses)]
    if keyword != "":
        mask = filtered["업소명"].str.contains(keyword, case=False, na=False) | filtered["주소"].str.contains(
            keyword, case=False, na=False
        )
        filtered = filtered[mask]

    st.subheader("현황")
    col1, col2 = st.columns(2)
    col1.metric("전체 업체 수(유효 좌표)", f"{len(valid):,}")
    col2.metric("현재 필터 결과", f"{len(filtered):,}")

    if len(filtered) == 0:
        st.info("필터 조건에 맞는 업체가 없습니다.")
        st.stop()

    with st.spinner("지도를 생성 중입니다. 데이터가 많으면 몇 초 걸릴 수 있습니다."):
        fmap = build_map(filtered)
    folium_static(fmap, width=None, height=760)

    st.subheader("필터 결과 테이블")
    table_cols = ["업소명", "주소", "영업상태", "지방행정데이터인허가일자", "latitude", "longitude", "구"]
    st.dataframe(filtered[table_cols], use_container_width=True, height=320)


if __name__ == "__main__":
    main()
