"""주소 → 좌표 (지도용). 결과는 data/state/geocode.json 에 캐시.

우선순위
  1) Kakao 로컬 API(KAKAO_REST_KEY) — 번지 단위, 가장 정확
  2) VWorld(VWORLD_KEY) — 번지 단위
  3) 키가 없으면 OpenStreetMap(Photon → Nominatim):
       · 단지 이름으로 OSM의 아파트 단지를 찾되, 같은 동 근처 + 이름 일치일 때만 '단지 위치'로 인정
       · 못 찾으면 '동' 중심(같은 동 안의 여러 곳은 겹치지 않게 살짝 벌려 놓음)
  4) 그래도 없으면 시·군·구 중심(대략 위치)
precision: exact(번지·단지) / dong(동 중심) / area(구 중심)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import logging
import os
import re
import time
from pathlib import Path

import requests

from .util import now_kst_iso, read_json, write_json

log = logging.getLogger(__name__)

# 시·군·구 대략 중심 (구청·시청 근처). 정확한 주소를 못 찾았을 때만 쓰며 화면에 '대략 위치'로 표시.
CENTROID = {
    "서울": (37.5665, 126.9780), "경기": (37.2750, 127.0095), "인천": (37.4563, 126.7052),
    "종로구": (37.5735, 126.9790), "중구": (37.5638, 126.9976), "용산구": (37.5324, 126.9906), "성동구": (37.5634, 127.0369),
    "광진구": (37.5385, 127.0823), "동대문구": (37.5744, 127.0400), "중랑구": (37.6063, 127.0927), "성북구": (37.5894, 127.0167),
    "강북구": (37.6396, 127.0257), "도봉구": (37.6688, 127.0471), "노원구": (37.6542, 127.0568), "은평구": (37.6027, 126.9291),
    "서대문구": (37.5791, 126.9368), "마포구": (37.5663, 126.9019), "양천구": (37.5170, 126.8665), "강서구": (37.5509, 126.8495),
    "구로구": (37.4954, 126.8874), "금천구": (37.4569, 126.8955), "영등포구": (37.5264, 126.8962), "동작구": (37.5124, 126.9393),
    "관악구": (37.4784, 126.9516), "서초구": (37.4837, 127.0324), "강남구": (37.5172, 127.0473), "송파구": (37.5145, 127.1059),
    "강동구": (37.5301, 127.1238),
    "수원시": (37.2636, 127.0286), "수원시 장안구": (37.3039, 127.0103), "수원시 권선구": (37.2575, 126.9718),
    "수원시 팔달구": (37.2826, 127.0197), "수원시 영통구": (37.2596, 127.0466),
    "성남시": (37.4200, 127.1265), "성남시 수정구": (37.4500, 127.1456), "성남시 중원구": (37.4306, 127.1376), "성남시 분당구": (37.3826, 127.1189),
    "의정부시": (37.7381, 127.0338), "안양시": (37.3943, 126.9568), "안양시 만안구": (37.3864, 126.9322), "안양시 동안구": (37.3925, 126.9511),
    "부천시": (37.5034, 126.7660), "부천시 원미구": (37.5040, 126.7660), "부천시 소사구": (37.4831, 126.7951), "부천시 오정구": (37.5264, 126.7932),
    "광명시": (37.4786, 126.8646), "평택시": (36.9921, 127.1129), "안산시": (37.3219, 126.8309), "안산시 상록구": (37.3009, 126.8466),
    "안산시 단원구": (37.3199, 126.8113), "고양시": (37.6584, 126.8320), "고양시 덕양구": (37.6375, 126.8320),
    "고양시 일산동구": (37.6587, 126.7748), "고양시 일산서구": (37.6754, 126.7509), "과천시": (37.4292, 126.9876),
    "구리시": (37.5943, 127.1296), "남양주시": (37.6360, 127.2165), "오산시": (37.1499, 127.0775), "시흥시": (37.3800, 126.8030),
    "군포시": (37.3616, 126.9352), "의왕시": (37.3447, 126.9683), "하남시": (37.5393, 127.2148), "용인시": (37.2411, 127.1776),
    "용인시 처인구": (37.2342, 127.2017), "용인시 기흥구": (37.2804, 127.1146), "용인시 수지구": (37.3220, 127.0975),
    "파주시": (37.7599, 126.7802), "이천시": (37.2720, 127.4350), "김포시": (37.6153, 126.7156), "화성시": (37.1995, 126.8312),
    "화성시 만세구": (37.1300, 126.9200), "화성시 효행구": (37.2200, 126.9500), "화성시 병점구": (37.2100, 127.0300),
    "화성시 동탄구": (37.2000, 127.1000), "광주시": (37.4295, 127.2550), "양주시": (37.7853, 127.0458), "안성시": (37.0080, 127.2797),
    "포천시": (37.8949, 127.2003), "여주시": (37.2983, 127.6370), "동두천시": (37.9035, 127.0605),
}


def centroid(region: str | None, sigungu: str | None) -> tuple[float, float] | None:
    for k in (sigungu, (sigungu or "").split(" ")[0], region):
        if k and k in CENTROID:
            return CENTROID[k]
    return None


def _clean_addr(addr: str) -> str:
    s = re.sub(r"\(.*?\)", " ", addr or "")
    s = re.sub(r"(일원|일대|외\s*\d+필지|번지)", " ", s)
    s = re.sub(r"([가-힣]+[동리가])(\d)", r"\1 \2", s)        # '수송동146-12' → '수송동 146-12'
    return re.sub(r"\s+", " ", s).strip()


def _dong_level(addr: str) -> str | None:
    m = re.match(r"(.+?[동읍면리가])(\s|$)", addr)
    return m.group(1) if m else None


class Geocoder:
    def __init__(self, cache_path: Path, max_new: int = 150, nominatim_delay: float = 1.1,
                 session: requests.Session | None = None, max_seconds: float = 240):
        self.path = cache_path
        self.cache: dict = read_json(cache_path, {})
        self.kakao = os.environ.get("KAKAO_REST_KEY", "").strip()
        self.vworld = os.environ.get("VWORLD_KEY", "").strip()
        self.s = session or requests.Session()
        # 주의: requests 기본 UA(python-requests)는 OSM이 403으로 막음 → 반드시 덮어씀
        self.s.headers["User-Agent"] = "cheongyak-tracker/1.0 (personal, non-commercial)"
        self.budget = max_new
        self.deadline = time.time() + max_seconds     # 지도 좌표 찾기가 갱신 전체를 붙잡지 않도록 시간 상한
        self.delay = nominatim_delay
        self._last_nom = 0.0
        self.disabled_nominatim = False
        self._last_ph = 0.0
        self._ph_fail = 0
        self.disabled_photon = False
        if self.kakao or self.vworld:
            self.budget = max(max_new, 3000)      # 키 사용 시 무료 OSM 제한과 무관
        if self.cache.get("_v") != 2:             # 예전 캐시: 구 중심(centroid)으로만 찍힌 항목은 다시 찾음
            self.cache = {k: v for k, v in self.cache.items()
                          if isinstance(v, dict) and v.get("provider") in ("kakao", "vworld")}
            self.cache["_v"] = 2

    def save(self) -> None:
        write_json(self.path, self.cache, compact=True)

    # ------------------------------------------------------------ providers
    def _kakao(self, q: str, keyword: bool = False):
        url = "https://dapi.kakao.com/v2/local/search/" + ("keyword.json" if keyword else "address.json")
        r = self.s.get(url, params={"query": q, "size": 1}, headers={"Authorization": f"KakaoAK {self.kakao}"}, timeout=10)
        docs = r.json().get("documents") or []
        return (float(docs[0]["y"]), float(docs[0]["x"])) if docs else None

    def _vworld(self, q: str):
        for typ in ("PARCEL", "ROAD"):
            r = self.s.get("https://api.vworld.kr/req/address", params={
                "service": "address", "request": "getcoord", "version": "2.0", "crs": "epsg:4326",
                "address": q, "format": "json", "type": typ, "key": self.vworld}, timeout=10)
            res = r.json().get("response", {})
            if res.get("status") == "OK":
                pt = res["result"]["point"]
                return float(pt["y"]), float(pt["x"])
        return None

    def _nominatim(self, q: str):
        """반환 (lat, lng, precision) — 번지까지 맞으면 exact, 동·구역 경계면 dong."""
        if self.disabled_nominatim:
            return None
        wait = self.delay - (time.time() - self._last_nom)
        if wait > 0:
            time.sleep(wait)
        self._last_nom = time.time()
        try:
            r = self.s.get("https://nominatim.openstreetmap.org/search",
                           params={"q": q, "format": "json", "limit": 1, "countrycodes": "kr", "accept-language": "ko"}, timeout=8)
        except requests.RequestException:
            self._nom_fail = getattr(self, "_nom_fail", 0) + 1
            if self._nom_fail >= 5:
                self.disabled_nominatim = True
            raise
        if r.status_code in (403, 429, 503):
            self._nom_fail = getattr(self, "_nom_fail", 0) + 1
            if self._nom_fail >= 3:
                self.disabled_nominatim = True
                log.warning("Nominatim 사용 제한(%s) — 이번 실행은 나머지를 대략 위치로 표시", r.status_code)
            else:
                time.sleep(5 * self._nom_fail)
            raise RuntimeError(f"nominatim {r.status_code}")   # 제한 응답은 '못 찾음'으로 캐시하지 않음
        self._nom_fail = 0
        js = r.json()
        if not js:
            return None
        h = js[0]
        prec = "dong" if h.get("class") in ("place", "boundary") else "exact"
        return float(h["lat"]), float(h["lon"]), prec

    def _photon(self, q: str, bbox: tuple | None = None, limit: int = 1) -> list[dict]:
        if self.disabled_photon:
            return []
        wait = 0.35 - (time.time() - self._last_ph)
        if wait > 0:
            time.sleep(wait)
        self._last_ph = time.time()
        params = {"q": q, "limit": limit}
        if bbox:
            params["bbox"] = ",".join(f"{v:.5f}" for v in bbox)
        try:
            r = self.s.get("https://photon.komoot.io/api/", params=params, timeout=8)
        except requests.RequestException:
            self._ph_fail += 1
            if self._ph_fail >= 5:
                self.disabled_photon = True
                log.warning("Photon 연결 실패가 이어져 이번 실행은 Nominatim으로 대신 찾음")
            raise
        if r.status_code in (403, 429, 503):
            self._ph_fail += 1
            if self._ph_fail >= 3:
                self.disabled_photon = True
                log.warning("Photon 사용 제한(%s) — Nominatim으로 대신 찾음", r.status_code)
            raise RuntimeError(f"photon {r.status_code}")
        self._ph_fail = 0
        return r.json().get("features") or []

    # ------------------------------------------------------------ 동 · 단지 (키 없을 때)
    def _spend(self) -> bool:
        if self.budget <= 0 or time.time() > self.deadline:
            return False
        self.budget -= 1
        return True

    def dong_point(self, dong_q: str) -> dict | None:
        """'서울 강남구 도곡동' → {lat, lng, bbox}. 같은 동은 한 번만 조회(캐시)."""
        dc = self.cache.setdefault("_dong", {})
        if dong_q in dc:
            return dc[dong_q] or None
        if not self._spend():
            return None
        out = None
        try:
            try:
                feats = self._photon(dong_q, limit=3)
            except Exception:
                feats = []
            for f in feats:
                pr = f.get("properties", {})
                if pr.get("countrycode") != "KR":
                    continue
                if pr.get("osm_key") in ("boundary", "place") or pr.get("type") in ("district", "locality", "city"):
                    lng, lat = f["geometry"]["coordinates"]
                    ext = pr.get("extent")   # [minLon, maxLat, maxLon, minLat]
                    out = {"lat": round(lat, 6), "lng": round(lng, 6), "src": "osm",
                           "bbox": [ext[0], ext[3], ext[2], ext[1]] if ext and len(ext) == 4 else None}
                    break
            if out is None:
                hit = self._nominatim(dong_q)
                if hit:
                    out = {"lat": round(hit[0], 6), "lng": round(hit[1], 6), "src": "osm", "bbox": None}
        except Exception as e:
            log.debug("동 좌표 실패 %s: %s", dong_q, e)
            return None          # 네트워크 오류는 캐시하지 않음(다음에 다시)
        dc[dong_q] = out or False
        return out

    def complex_point(self, name: str, near: dict) -> tuple[float, float] | None:
        """단지 이름으로 OSM의 아파트 단지를 찾는다. 이름이 같고 동 중심에서 2km 안일 때만 인정."""
        key = _name_key(name)
        if len(key) < 2 or not self._spend():
            return None
        lat0, lng0 = near["lat"], near["lng"]
        bbox = (lng0 - 0.03, lat0 - 0.025, lng0 + 0.03, lat0 + 0.025)
        try:
            feats = self._photon(f"{name} 아파트" if "아파트" not in name else name, bbox=bbox, limit=5)
        except Exception as e:
            log.debug("단지 좌표 실패 %s: %s", name, e)
            return None
        for f in feats:
            pr = f.get("properties", {})
            if pr.get("osm_key") not in ("landuse", "building", "place", "amenity") and pr.get("osm_value") != "apartments":
                continue
            if _name_key(pr.get("name") or "") != key:
                continue
            lng, lat = f["geometry"]["coordinates"]
            if _km(lat, lng, lat0, lng0) <= 2.0:
                return lat, lng
        return None

    # ------------------------------------------------------------ public
    def locate(self, key: str, address: str | None, region: str | None, sigungu: str | None,
               name: str | None = None, stage: str = "all") -> dict | None:
        """key: 캐시 키(보통 항목 id). 반환 {lat, lng, precision: exact|dong|area, provider}.
        stage='dong' 이면 단지 이름 검색은 건너뜀(1차: 모두 동 단위로 빠르게 → 2차: 남은 예산으로 단지 검색)."""
        c = self.cache.get(key)
        if c and c.get("lat") and c.get("precision") == "exact":
            return c
        if c and c.get("precision") == "dong" and (stage == "dong" or c.get("name_tried") or self.kakao or self.vworld):
            return c
        addr = _clean_addr(address or "")
        full = (addr if addr.startswith(("서울", "경기", "인천")) else f"{region or ''} {addr}".strip()) if addr else ""
        dong_q = _dong_level(full) if full else None
        if dong_q and dong_q.count(" ") < 1 and sigungu:          # '동탄' 처럼 동만 있으면 구를 붙임
            dong_q = f"{region or ''} {sigungu} {dong_q}".strip()

        # 1) 키가 있으면 번지 단위
        if self.kakao or self.vworld:
            queries = [(full, "exact", False)] if full else []
            if dong_q and dong_q != full:
                queries.append((dong_q, "dong", False))
            if name and self.kakao:
                queries.insert(1, (f"{sigungu or region or ''} {name}".strip(), "exact", True))
            for q, prec, kw in queries:
                if not self._spend():
                    break
                try:
                    hit = self._kakao(q, keyword=kw) if self.kakao else (self._vworld(q) if not kw else None)
                except Exception as e:
                    log.debug("지오코딩 실패 %s: %s", q, e)
                    hit = None
                if hit:
                    out = {"lat": round(hit[0], 6), "lng": round(hit[1], 6), "precision": prec,
                           "provider": "kakao" if self.kakao else "vworld", "q": q}
                    if prec == "dong":
                        out["lat"], out["lng"] = _spread(out["lat"], out["lng"], key)
                    self.cache[key] = out
                    return out

        # 2) 키 없음: 동 중심 → (2차) 단지 이름
        else:
            dp = self.dong_point(dong_q) if dong_q else None
            if dp:
                tried = stage != "dong" and bool(name) and self.budget > 0
                if tried:
                    hit = self.complex_point(name, dp)
                    if hit:
                        out = {"lat": round(hit[0], 6), "lng": round(hit[1], 6), "precision": "exact",
                               "provider": "osm", "q": name}
                        self.cache[key] = out
                        return out
                lat, lng = _spread(dp["lat"], dp["lng"], key)
                out = {"lat": lat, "lng": lng, "precision": "dong", "provider": "osm", "q": dong_q,
                       "name_tried": tried}
                self.cache[key] = out
                return out
            if c and c.get("precision") in ("dong",):
                return c

        cen = centroid(region, sigungu)
        if cen:
            lat, lng = _spread(cen[0], cen[1], key, radius_m=600)
            out = {"lat": lat, "lng": lng, "precision": "area", "provider": "centroid", "tried_at": now_kst_iso()[:10]}
            self.cache[key] = out
            return out
        return None


def _name_key(name: str) -> str:
    s = re.sub(r"\(.*?\)", "", name or "")
    s = re.sub(r"(아파트|apt|APT|\s)", "", s)
    return s


def _km(a1, o1, a2, o2) -> float:
    return math.hypot((a1 - a2) * 111.0, (o1 - o2) * 88.2)


def _spread(lat: float, lng: float, key: str, radius_m: int = 250) -> tuple[float, float]:
    """같은 중심점에 여러 곳이 겹치지 않도록 id 해시로 반경 안에 고르게 흩뜨림(항상 같은 자리)."""
    h = int(hashlib.md5(key.encode()).hexdigest()[:8], 16)
    ang = (h % 3600) / 3600 * 2 * math.pi
    r = (0.35 + 0.65 * ((h >> 12) % 1000) / 1000) * radius_m / 1000
    return round(lat + r * math.sin(ang) / 111.0, 6), round(lng + r * math.cos(ang) / 88.2, 6)


def attach(items: list[dict], geo: Geocoder, addr_key: str = "address", stage: str = "all") -> None:
    for it in items:
        g = geo.locate(it["id"], it.get(addr_key), it.get("region"), it.get("sigungu"), it.get("name"), stage=stage)
        if g:
            it["lat"], it["lng"], it["geo"] = g["lat"], g["lng"], g["precision"]
