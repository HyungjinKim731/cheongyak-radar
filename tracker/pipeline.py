"""정비사업(재건축·재개발) 파이프라인.

1) 서울: 열린데이터광장 '서울시 정비사업 추진현황'(OA-22856) 엑셀, 분기 갱신.
   데이터셋 페이지에서 파일 목록을 찾아 가장 최신 기준월 파일을 내려받는다.
   실패하면 data/raw/seoul/ 에 있는 최신 xlsx 를 쓴다.
2) 관심목록: data/watchlist.csv — 경기 등 통합 데이터가 없는 곳, 메모·분양예정 시기 등을 직접 관리.
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import openpyxl
import requests

from . import sources
from .util import clean, to_int, to_iso

log = logging.getLogger(__name__)

STAGES = ["구역지정", "추진위", "조합설립", "건축심의", "사업시행", "관리처분", "착공", "분양예정"]
STAGE_ORDER = {s: i + 1 for i, s in enumerate(STAGES)}
_STAGE_ALIASES = {
    "정비구역지정": "구역지정", "구역지정": "구역지정", "정비계획": "구역지정",
    "추진위원회": "추진위", "추진위": "추진위",
    "조합설립인가": "조합설립", "조합설립": "조합설립", "사업시행자지정": "조합설립",
    "건축심의": "건축심의",
    "사업시행인가": "사업시행", "사업시행": "사업시행",
    "관리처분인가": "관리처분", "관리처분": "관리처분", "이주": "관리처분", "철거": "관리처분",
    "착공": "착공", "공사중": "착공",
    "분양예정": "분양예정", "분양": "분양예정", "공고예정": "분양예정",
}


def normalize_stage(raw: Any) -> str | None:
    s = re.sub(r"\s+", "", str(raw or ""))
    if not s:
        return None
    for k, v in _STAGE_ALIASES.items():
        if k in s:
            return v
    return s


# ---------------------------------------------------------------- 서울 엑셀 다운로드
_FNAME_RE = re.compile(r"(\d{2})\s*년\s*(\d{1,2})\s*월")


def _decode_filename(cd: str) -> str:
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', cd)
    name = m.group(1) if m else "seoul.xlsx"
    try:  # requests는 헤더를 latin-1로 디코딩한다 → UTF-8로 복원
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _as_of_key(fname: str) -> tuple[int, int]:
    m = _FNAME_RE.search(fname)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def download_seoul_latest(cfg: dict, raw_dir: Path, session: requests.Session | None = None) -> Path | None:
    """최신 기준월 파일을 raw_dir에 저장하고 경로를 돌려준다. 이미 있으면 재다운로드하지 않는다."""
    s = session or requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 cheongyak-tracker/1.0"
    raw_dir.mkdir(parents=True, exist_ok=True)
    page = s.get(cfg["dataset_page"], timeout=30).text
    seqs = sorted(set(re.findall(r"downloadFile\('(\d+)'\)", page)), key=int, reverse=True)
    best: tuple[tuple[int, int], str, bytes] | None = None
    for seq in seqs:
        try:
            r = s.post(cfg["download_url"], data={"infId": cfg["inf_id"], "seqNo": "", "seq": seq,
                                                  "infSeq": "2"}, timeout=60)
        except requests.RequestException as e:
            log.warning("서울 파일 seq=%s 다운로드 실패: %s", seq, e)
            continue
        fname = _decode_filename(r.headers.get("Content-Disposition", ""))
        if r.status_code != 200 or not r.content.startswith(b"PK"):
            continue
        key = _as_of_key(fname)
        if best is None or key > best[0]:
            best = (key, fname, r.content)
    if not best:
        return None
    yy, mm = best[0]
    out = raw_dir / f"seoul_{2000 + yy}-{mm:02d}.xlsx"
    if not out.exists():
        out.write_bytes(best[2])
        log.info("서울 정비사업 파일 저장: %s (%s)", out.name, best[1])
    return out


def latest_local_seoul(raw_dir: Path) -> Path | None:
    files = sorted(raw_dir.glob("*.xlsx"))
    return files[-1] if files else None


# ---------------------------------------------------------------- 서울 엑셀 파싱
def _header_paths(ws, n_header_rows: int) -> list[str]:
    """병합 셀을 풀어서 열마다 '상위>하위' 헤더 경로 문자열을 만든다."""
    grid = [[ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
            for r in range(1, n_header_rows + 1)]
    for mr in ws.merged_cells.ranges:
        if mr.min_row > n_header_rows:
            continue
        v = ws.cell(mr.min_row, mr.min_col).value
        for r in range(mr.min_row, min(mr.max_row, n_header_rows) + 1):
            for c in range(mr.min_col, mr.max_col + 1):
                grid[r - 1][c - 1] = v
    paths = []
    for c in range(ws.max_column):
        parts = []
        for r in range(n_header_rows):
            v = grid[r][c]
            if v is not None:
                t = re.sub(r"\s+", "", str(v))
                if not parts or parts[-1] != t:
                    parts.append(t)
        paths.append(">".join(parts))
    return paths


def _find_col(paths: list[str], *needles: str, exclude: tuple[str, ...] = ()) -> int | None:
    for i, p in enumerate(paths):
        if all(n in p for n in needles) and not any(x in p for x in exclude):
            return i
    return None


def parse_seoul_xlsx(path: Path) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    # 헤더 행 수: 첫 열이 숫자인 첫 행 직전까지
    n_header = 1
    for r in range(1, 15):
        if isinstance(ws.cell(r, 1).value, (int, float)):
            n_header = r - 1
            break
    P = _header_paths(ws, n_header)
    col = {
        "code": _find_col(P, "CODE"),
        "gu": _find_col(P, "자치구"),
        "name": _find_col(P, "구역명"),
        "addr": _find_col(P, "위치1") if _find_col(P, "위치1") is not None else _find_col(P, "위치"),
        "road": _find_col(P, "위치2"),
        "public": _find_col(P, "공공/민간"),
        "zone": _find_col(P, "재촉"),
        "btype": _find_col(P, "사업유형"),
        "stage": _find_col(P, "사업추진단계"),
        "existing": _find_col(P, "기존가구수"),
        "d_zone": _find_col(P, "구역지정", "최초"),
        "d_zone2": _find_col(P, "구역지정", "변경"),
        "d_comm": _find_col(P, "추진위원회"),
        "d_union": _find_col(P, "조합설립"),
        "d_review": _find_col(P, "건축심의"),
        "d_impl": _find_col(P, "사업시행", "최초", exclude=("사업추진",)),
        "d_impl2": _find_col(P, "사업시행", "변경", exclude=("사업추진",)),
        "d_mgmt": _find_col(P, "관리처분", "최초"),
        "d_mgmt2": _find_col(P, "관리처분", "변경"),
        "d_move_s": _find_col(P, "이주시작"),
        "d_move_e": _find_col(P, "이주종료"),
        "d_start": _find_col(P, "착공"),
        "hh_total": _find_col(P, "총합계"),
        "hh_sale": _find_col(P, "분양", exclude=("관리처분",)),
        "hh_rent": _find_col(P, "임대"),
    }
    missing = [k for k in ("name", "stage") if col[k] is None]
    if missing:
        raise ValueError(f"{path.name}: 필수 열을 찾지 못함 {missing}. 헤더: {P}")

    m = re.search(r"(\d{4})-(\d{2})", path.name)
    as_of = f"{m.group(1)}-{m.group(2)}" if m else None

    def g(row, k):
        i = col.get(k)
        return row[i] if i is not None and i < len(row) else None

    out = []
    for row in ws.iter_rows(min_row=n_header + 1, values_only=True):
        name = clean(g(row, "name"))
        if not name:
            continue
        btype = clean(g(row, "btype")) or ""
        stage = normalize_stage(g(row, "stage"))
        milestones = {k: to_iso(g(row, k)) for k in
                      ("d_zone", "d_zone2", "d_comm", "d_union", "d_review", "d_impl", "d_impl2",
                       "d_mgmt", "d_mgmt2", "d_move_s", "d_move_e", "d_start")}
        addr = clean(g(row, "addr"))
        gu = clean(g(row, "gu"))
        code = clean(g(row, "code")) or f"{gu}-{name}"
        out.append({
            "id": f"seoul:{code}",
            "source": "seoul_xlsx",
            "as_of": as_of,
            "region": "서울",
            "sigungu": gu,
            "name": name,
            "type": "재건축" if "재건축" in btype else ("재개발" if "재개발" in btype else btype or None),
            "type_detail": btype or None,
            "public": clean(g(row, "public")),
            "zone": clean(g(row, "zone")),
            "stage": stage,
            "stage_order": STAGE_ORDER.get(stage or "", 0),
            "milestones": {k: v for k, v in milestones.items() if v},
            "existing_households": to_int(g(row, "existing")),
            "households": to_int(g(row, "hh_total")),
            "sale_households": to_int(g(row, "hh_sale")),
            "rent_households": to_int(g(row, "hh_rent")),
            "address": " ".join(x for x in (gu, addr) if x) or None,
            "road_address": clean(g(row, "road")),
        })
    return out


# ---------------------------------------------------------------- 관심목록 CSV
WATCHLIST_FIELDS = ["id", "region", "sigungu", "name", "type", "stage", "households",
                    "general_supply", "expected", "builder", "source_url", "basis", "memo", "updated", "address"]


def load_watchlist(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            r = {k: clean(v) for k, v in r.items() if k}
            if not r.get("name") and not r.get("id"):
                continue
            stage = normalize_stage(r.get("stage"))
            rows.append({
                "id": r.get("id") or f"watch:{r.get('region', '')}:{r.get('name')}",
                "source": "watchlist",
                "region": r.get("region"),
                "sigungu": r.get("sigungu"),
                "name": r.get("name"),
                "type": r.get("type"),
                "stage": stage,
                "stage_order": STAGE_ORDER.get(stage or "", 0),
                "households": to_int(r.get("households")),
                "general_supply": to_int(r.get("general_supply")),
                "expected": r.get("expected"),
                "builder": r.get("builder"),
                "source_url": r.get("source_url") if sources.is_official(r.get("source_url")) else None,
                "basis": "공식" if (r.get("basis") == "공식" and sources.is_official(r.get("source_url"))) else "보도",
                "memo": r.get("memo"),
                "updated": to_iso(r.get("updated")),
                "address": r.get("address"),
                "_line": i,
            })
    return rows


def merge_pipeline(seoul: list[dict], watch: list[dict]) -> list[dict]:
    """관심목록의 id가 서울 데이터 id(seoul:CODE)와 같으면 메모·예정시기 등을 덧씌운다."""
    by_id = {p["id"]: dict(p) for p in seoul}
    for w in watch:
        base = by_id.get(w["id"])
        if base:
            for k in ("expected", "general_supply", "builder", "source_url", "memo", "updated"):
                if w.get(k) is not None:
                    base[k] = w[k]
            if w.get("name") and w["name"] != base["name"]:
                base["alias"] = base["name"]   # 공식 구역명은 별칭으로, 화면에는 분양 단지명(예: 디에이치 반포124)
                base["name"] = w["name"]
            # 관심목록에서 단계를 직접 적었다면 공식 데이터보다 최근 정보로 본다
            if w.get("stage") and w["stage_order"] > base.get("stage_order", 0):
                base["stage"], base["stage_order"] = w["stage"], w["stage_order"]
                base["stage_note"] = "관심목록에서 갱신"
            base["watched"] = True
        else:
            w = {k: v for k, v in w.items() if k != "_line"}
            w["watched"] = True
            by_id[w["id"]] = w
    return sorted(by_id.values(), key=lambda p: (-p.get("stage_order", 0), p.get("region") or "",
                                                 p.get("sigungu") or "", p.get("name") or ""))
