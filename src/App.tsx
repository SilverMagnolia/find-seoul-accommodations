import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent, TouchEvent } from "react";
import Papa from "papaparse";
import { CustomOverlayMap, Map as KakaoMap, MapMarker, MarkerClusterer, useKakaoLoader } from "react-kakao-maps-sdk";

type AccommodationRow = {
  id: number;
  name: string;
  status: string;
  address: string;
  licenseDate: string;
  latitude: number;
  longitude: number;
  district: string;
};

type RawCsvRow = {
  업소명?: string;
  영업상태?: string;
  주소?: string;
  지방행정데이터인허가일자?: string;
  latitude?: string;
  longitude?: string;
};

const DEFAULT_CSV_PATH = new URL(
  "../crawling-data/seoul-accomodations/20260318-212418-with-latlng.csv",
  import.meta.url
).href;
const SEOUL_CENTER = { lat: 37.5665, lng: 126.978 };
const DEFAULT_LEVEL = 8;
const KAKAO_APP_KEY = "bfa2d9d5c5b90ac2beca52007d6809ea";
const POSITION_DECIMALS = 7;
const CLUSTER_MODAL_MAX_ITEMS = 50;

function extractDistrict(address: string): string {
  const match = address.match(/(?:서울특별시|서울시)\s*([^\s,]+)/);
  if (match === null) {
    return "미상";
  }
  const district = match[1].trim();
  if (district.endsWith("구") === true) {
    return district;
  }
  return "미상";
}

function buildMapLinks(row: AccommodationRow): { kakaoByCoord: string; kakaoByAddress: string; naverByAddress: string } {
  const name = row.name;
  const address = row.address;
  const lat = row.latitude.toFixed(7);
  const lng = row.longitude.toFixed(7);
  const kakaoByCoord = `https://map.kakao.com/link/map/${encodeURIComponent(name)},${lat},${lng}`;
  const kakaoByAddress = `https://map.kakao.com/link/search/${encodeURIComponent(address)}`;
  const naverByAddress = `https://map.naver.com/p/search/${encodeURIComponent(address)}`;
  return { kakaoByCoord, kakaoByAddress, naverByAddress };
}

function toPositionKey(lat: number, lng: number): string {
  return `${lat.toFixed(POSITION_DECIMALS)},${lng.toFixed(POSITION_DECIMALS)}`;
}

async function loadRows(csvPath: string): Promise<AccommodationRow[]> {
  const response = await fetch(csvPath, { cache: "no-store" });
  if (response.ok !== true) {
    throw new Error(`CSV를 불러오지 못했습니다. path=${csvPath} status=${response.status}`);
  }
  const text = await response.text();
  const parsed = Papa.parse<RawCsvRow>(text, { header: true, skipEmptyLines: true });
  if (parsed.errors.length !== 0) {
    throw new Error(`CSV 파싱 실패: ${parsed.errors[0].message}`);
  }

  const rows: AccommodationRow[] = [];
  parsed.data.forEach((raw, index) => {
    const lat = Number(raw.latitude);
    const lng = Number(raw.longitude);
    if (Number.isFinite(lat) !== true || Number.isFinite(lng) !== true) {
      return;
    }

    const address = raw.주소 ?? "";
    rows.push({
      id: index,
      name: raw.업소명 ?? "",
      status: raw.영업상태 ?? "",
      address,
      licenseDate: raw.지방행정데이터인허가일자 ?? "",
      latitude: lat,
      longitude: lng,
      district: extractDistrict(address)
    });
  });
  return rows;
}

export default function App() {
  const stopOverlayEvent = (event: MouseEvent<HTMLDivElement> | TouchEvent<HTMLDivElement>) => {
    event.stopPropagation();
  };

  const closeSingleOverlay = (event: MouseEvent<HTMLDivElement>) => {
    event.stopPropagation();
    setSelectedRowId(null);
  };

  const [kakaoLoading, kakaoError] = useKakaoLoader({
    appkey: KAKAO_APP_KEY,
    libraries: ["clusterer"]
  });

  const mapRef = useRef<any>(null);

  const [rows, setRows] = useState<AccommodationRow[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string>("");
  const [filterOpen, setFilterOpen] = useState<boolean>(false);
  const [keyword, setKeyword] = useState<string>("");
  const [selectedDistricts, setSelectedDistricts] = useState<string[]>([]);
  const [selectedStatuses, setSelectedStatuses] = useState<string[]>([]);
  const [draftKeyword, setDraftKeyword] = useState<string>("");
  const [draftSelectedDistricts, setDraftSelectedDistricts] = useState<string[]>([]);
  const [draftSelectedStatuses, setDraftSelectedStatuses] = useState<string[]>([]);
  const center = SEOUL_CENTER;
  const level = DEFAULT_LEVEL;
  const [selectedRowId, setSelectedRowId] = useState<number | null>(null);
  const [selectedClusterRows, setSelectedClusterRows] = useState<AccommodationRow[]>([]);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      setLoading(true);
      setErrorMessage("");
      try {
        const loadedRows = await loadRows(DEFAULT_CSV_PATH);
        if (cancelled === true) {
          return;
        }
        setRows(loadedRows);
      } catch (error) {
        if (cancelled === true) {
          return;
        }
        const message = error instanceof Error ? error.message : "CSV 로드 중 알 수 없는 오류가 발생했습니다.";
        setRows([]);
        setErrorMessage(message);
      } finally {
        if (cancelled === false) {
          setLoading(false);
        }
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, []);

  const districts = useMemo(() => {
    const unique = new Set<string>();
    rows.forEach((row) => {
      if (row.district.endsWith("구") === true) {
        unique.add(row.district);
      }
    });
    return Array.from(unique).sort((a, b) => a.localeCompare(b, "ko"));
  }, [rows]);

  const statuses = useMemo(() => {
    const unique = new Set<string>();
    rows.forEach((row) => {
      unique.add(row.status);
    });
    return Array.from(unique).sort((a, b) => a.localeCompare(b, "ko"));
  }, [rows]);

  useEffect(() => {
    if (rows.length === 0) {
      setSelectedDistricts([]);
      setSelectedStatuses([]);
      setDraftSelectedDistricts([]);
      setDraftSelectedStatuses([]);
      return;
    }
    if (selectedDistricts.length === 0) {
      setSelectedDistricts(districts);
      setDraftSelectedDistricts(districts);
    }
    if (selectedStatuses.length === 0) {
      setSelectedStatuses(statuses);
      setDraftSelectedStatuses(statuses);
    }
  }, [rows, districts, statuses, selectedDistricts.length, selectedStatuses.length]);

  const filteredRows = useMemo(() => {
    if (rows.length === 0) {
      return [];
    }
    const normalizedKeyword = keyword.trim().toLowerCase();
    return rows.filter((row) => {
      const districtOk = row.district.endsWith("구") === true && selectedDistricts.includes(row.district);
      if (districtOk !== true) {
        return false;
      }
      const statusOk = selectedStatuses.includes(row.status);
      if (statusOk !== true) {
        return false;
      }
      if (normalizedKeyword === "") {
        return true;
      }
      const byName = row.name.toLowerCase().includes(normalizedKeyword);
      const byAddress = row.address.toLowerCase().includes(normalizedKeyword);
      return byName === true || byAddress === true;
    });
  }, [rows, selectedDistricts, selectedStatuses, keyword]);

  const rowsByPosition = useMemo(() => {
    const grouped = new Map<string, AccommodationRow[]>();
    filteredRows.forEach((row) => {
      const key = toPositionKey(row.latitude, row.longitude);
      const current = grouped.get(key);
      if (current === undefined) {
        grouped.set(key, [row]);
        return;
      }
      current.push(row);
    });
    return grouped;
  }, [filteredRows]);

  useEffect(() => {
    const map = mapRef.current;
    if (map === null) {
      return;
    }
    if (typeof window === "undefined" || window.kakao == null || window.kakao.maps == null) {
      return;
    }
    if (filteredRows.length === 0) {
      setSelectedRowId(null);
      setSelectedClusterRows([]);
      map.setCenter(new window.kakao.maps.LatLng(SEOUL_CENTER.lat, SEOUL_CENTER.lng));
      map.setLevel(DEFAULT_LEVEL);
      return;
    }
    const bounds = new window.kakao.maps.LatLngBounds();
    filteredRows.forEach((row) => {
      bounds.extend(new window.kakao.maps.LatLng(row.latitude, row.longitude));
    });
    map.setBounds(bounds);
    setSelectedRowId(null);
    setSelectedClusterRows([]);
  }, [filteredRows]);

  const resetDraftFilter = () => {
    setDraftKeyword("");
    setDraftSelectedDistricts(districts);
    setDraftSelectedStatuses(statuses);
  };

  const openFilterModal = () => {
    setDraftKeyword(keyword);
    setDraftSelectedDistricts(selectedDistricts);
    setDraftSelectedStatuses(selectedStatuses);
    setFilterOpen(true);
  };

  const applyFilter = () => {
    setKeyword(draftKeyword);
    setSelectedDistricts(draftSelectedDistricts);
    setSelectedStatuses(draftSelectedStatuses);
    setFilterOpen(false);
  };

  const selectedRow = useMemo(() => {
    if (selectedRowId === null) {
      return null;
    }
    const row = filteredRows.find((item) => item.id === selectedRowId);
    if (row === undefined) {
      return null;
    }
    return row;
  }, [filteredRows, selectedRowId]);

  const handleClusterClick = (_clusterer: unknown, cluster: any) => {
    if (cluster == null) {
      return;
    }
    if (typeof cluster.getMarkers !== "function") {
      return;
    }

    const markerList = cluster.getMarkers();
    if (Array.isArray(markerList) !== true) {
      return;
    }
    const clusterSize =
      typeof cluster.getSize === "function" && typeof cluster.getSize() === "number"
        ? cluster.getSize()
        : markerList.length;

    const availableByPosition = new Map<string, AccommodationRow[]>();
    rowsByPosition.forEach((value, key) => {
      availableByPosition.set(key, [...value]);
    });

    const clusterRows: AccommodationRow[] = [];
    markerList.forEach((marker: any) => {
      if (marker == null || typeof marker.getPosition !== "function") {
        return;
      }
      const position = marker.getPosition();
      if (position == null || typeof position.getLat !== "function" || typeof position.getLng !== "function") {
        return;
      }
      const lat = position.getLat();
      const lng = position.getLng();
      if (typeof lat !== "number" || typeof lng !== "number") {
        return;
      }
      const key = toPositionKey(lat, lng);
      const bucket = availableByPosition.get(key);
      if (bucket === undefined || bucket.length === 0) {
        return;
      }
      const row = bucket.shift();
      if (row !== undefined) {
        clusterRows.push(row);
      }
    });

    if (clusterRows.length === 0) {
      return;
    }

    if (clusterSize === 1) {
      setSelectedRowId(clusterRows[0].id);
      setSelectedClusterRows([]);
      return;
    }
    if (clusterSize > CLUSTER_MODAL_MAX_ITEMS) {
      setSelectedClusterRows([]);
      setSelectedRowId(null);
      const map = mapRef.current;
      if (map != null && typeof map.getLevel === "function" && typeof map.setLevel === "function") {
        const currentLevel = map.getLevel();
        const nextLevel = currentLevel - 1;
        const center = typeof cluster.getCenter === "function" ? cluster.getCenter() : null;
        if (center != null) {
          map.setLevel(nextLevel, { anchor: center });
        } else {
          map.setLevel(nextLevel);
        }
      }
      return;
    }

    setSelectedRowId(null);
    setSelectedClusterRows(clusterRows);
  };

  const closeClusterModal = () => {
    setSelectedClusterRows([]);
  };

  return (
    <div className="app">
      {kakaoLoading === false && kakaoError == null ? (
        <KakaoMap
          center={center}
          level={level}
          className="map-root"
          onCreate={(map) => {
            mapRef.current = map;
          }}
          onClick={() => {
            setSelectedRowId(null);
            setSelectedClusterRows([]);
          }}
        >
          <MarkerClusterer averageCenter={true} minLevel={1} disableClickZoom={true} onClusterclick={handleClusterClick}>
            {filteredRows.map((row) => (
              <MapMarker
                key={row.id}
                position={{ lat: row.latitude, lng: row.longitude }}
                onClick={() => {
                  setSelectedRowId(row.id);
                  setSelectedClusterRows([]);
                }}
              />
            ))}
          </MarkerClusterer>

          {selectedRow !== null ? (
            <CustomOverlayMap position={{ lat: selectedRow.latitude, lng: selectedRow.longitude }} yAnchor={1.4}>
              <div
                className="overlay-card"
                onClick={closeSingleOverlay}
                onMouseDown={stopOverlayEvent}
                onTouchStart={stopOverlayEvent}
              >
                <div className="overlay-title">{selectedRow.name}</div>
                <div className="overlay-address">{selectedRow.address}</div>
                <div className="overlay-meta">
                  상태: {selectedRow.status} | 인허가일자: {selectedRow.licenseDate}
                </div>
                <div className="overlay-links">
                  <a href={buildMapLinks(selectedRow).kakaoByCoord} target="_blank" rel="noreferrer">
                    카카오맵(좌표)
                  </a>
                  {" | "}
                  <a href={buildMapLinks(selectedRow).kakaoByAddress} target="_blank" rel="noreferrer">
                    카카오맵(주소)
                  </a>
                  {" | "}
                  <a href={buildMapLinks(selectedRow).naverByAddress} target="_blank" rel="noreferrer">
                    네이버지도(주소)
                  </a>
                </div>
              </div>
            </CustomOverlayMap>
          ) : null}
        </KakaoMap>
      ) : (
        <div className="map-root" />
      )}

      <div className="top-bar">
        <span className="badge">
          {loading === true || kakaoLoading === true ? "로딩 중..." : `필터 결과 ${filteredRows.length.toLocaleString()}건`}
        </span>
        <button type="button" className="action-button" onClick={openFilterModal}>
          서울시 구 {selectedDistricts.length.toLocaleString()}개 선택
        </button>
      </div>

      {errorMessage !== "" ? <div className="load-error">{errorMessage}</div> : null}
      {kakaoError != null ? <div className="load-error">카카오맵 SDK 로드 실패: {String(kakaoError)}</div> : null}

      {selectedClusterRows.length > 0 ? (
        <>
          <div className="fullscreen-modal-backdrop" onClick={closeClusterModal} />
          <section className="fullscreen-modal">
            <div className="fullscreen-modal-header">
              <strong>업체 {selectedClusterRows.length.toLocaleString()}건</strong>
              <button type="button" className="small-button" onClick={closeClusterModal}>
                닫기
              </button>
            </div>
            <div className="fullscreen-modal-list">
              {selectedClusterRows.map((row) => (
                <article key={row.id} className="overlay-item">
                  <div className="overlay-item-name">{row.name}</div>
                  <div className="overlay-item-address">{row.address}</div>
                  <div className="overlay-meta">
                    상태: {row.status} | 인허가일자: {row.licenseDate}
                  </div>
                  <div className="overlay-links">
                    <a href={buildMapLinks(row).kakaoByCoord} target="_blank" rel="noreferrer">
                      카카오맵(좌표)
                    </a>
                    {" | "}
                    <a href={buildMapLinks(row).kakaoByAddress} target="_blank" rel="noreferrer">
                      카카오맵(주소)
                    </a>
                    {" | "}
                    <a href={buildMapLinks(row).naverByAddress} target="_blank" rel="noreferrer">
                      네이버지도(주소)
                    </a>
                  </div>
                </article>
              ))}
            </div>
          </section>
        </>
      ) : null}

      {filterOpen === true ? (
        <>
          <div className="filter-modal-backdrop" onClick={() => setFilterOpen(false)} />
          <section className="filter-modal">
            <div className="form-row">
              <label className="form-label" htmlFor="keyword">
                업소명/주소 검색
              </label>
              <input
                id="keyword"
                className="text-input"
                value={draftKeyword}
                onChange={(event) => setDraftKeyword(event.target.value)}
                placeholder="검색어 입력"
              />
            </div>

            <div className="form-row">
              <div className="form-label">서울시 구</div>
              <div className="inline-actions" style={{ marginBottom: 8 }}>
                <button type="button" className="small-button" onClick={() => setDraftSelectedDistricts(districts)}>
                  전체 선택
                </button>
                <button type="button" className="small-button" onClick={() => setDraftSelectedDistricts([])}>
                  전체 해제
                </button>
              </div>
              <div className="check-grid">
                {districts.map((district) => {
                  const checked = draftSelectedDistricts.includes(district);
                  return (
                    <label key={district} className="check-item">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(event) => {
                          if (event.target.checked === true) {
                            setDraftSelectedDistricts((prev) => [...prev, district]);
                            return;
                          }
                          setDraftSelectedDistricts((prev) => prev.filter((item) => item !== district));
                        }}
                      />
                      {district}
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="form-row">
              <div className="form-label">영업상태</div>
              <div className="check-grid">
                {statuses.map((status) => {
                  const checked = draftSelectedStatuses.includes(status);
                  return (
                    <label key={status} className="check-item">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(event) => {
                          if (event.target.checked === true) {
                            setDraftSelectedStatuses((prev) => [...prev, status]);
                            return;
                          }
                          setDraftSelectedStatuses((prev) => prev.filter((item) => item !== status));
                        }}
                      />
                      {status}
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="form-row inline-actions">
              <button type="button" className="small-button" onClick={resetDraftFilter}>
                필터 초기화
              </button>
              <button type="button" className="small-button" onClick={() => setFilterOpen(false)}>
                취소
              </button>
              <button type="button" className="small-button primary" onClick={applyFilter}>
                적용
              </button>
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}
