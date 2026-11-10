"""전체 갱신 실행: python -m tracker.run

환경변수
  DATA_GO_KR_KEY   공공데이터포털 '청약홈 분양정보 조회 서비스' 일반 인증키(Decoding 키)
옵션
  --demo           API 키 없이 tests/fixtures 샘플로 화면 확인
  --no-download    서울 정비사업 파일 자동 다운로드를 건너뛰고 data/raw/seoul 의 파일 사용
  --skip-prices    주택형별 분양가 조회 생략
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import tomllib

from . import notify, sources
from . import applyhome, geo, guide, market, news, pipeline, rules, upcoming
from .util import ROOT, now_kst_iso, read_json, today_kst, write_json

log = logging.getLogger("tracker")

STATE = ROOT / "data" / "state"
OUT = ROOT / "docs" / "data" / "data.json"
FIXTURE = ROOT / "tests" / "fixtures" / "applyhome_sample.json"


def load_config() -> dict:
    with (ROOT / "config.toml").open("rb") as f:
        return tomllib.load(f)


# ------------------------------------------------------------------ 청약 일정
def fixture_getter(fixture: dict):
    """샘플 JSON을 API처럼 돌려주는 getter (데모·테스트용)."""
    def _get(_session, url, params):
        op = url.rsplit("/", 1)[-1]
        rows = fixture.get(op, [])
        if "Mdl" in op:
            pb = params.get("cond[PBLANC_NO::EQ]")
            rows = [r for r in rows if str(r.get("PBLANC_NO")) == str(pb)]
        page = int(params.get("page", 1))
        per = int(params.get("perPage", 100))
        chunk = rows[(page - 1) * per: page * per]
        return {"data": chunk, "currentCount": len(chunk), "matchCount": len(rows), "totalCount": len(rows),
                "page": page, "perPage": per}
    return _get


def collect_schedules(cfg: dict, args, prev: dict, warnings: list[str]) -> tuple[list[dict], bool]:
    g, a = cfg["general"], cfg["applyhome"]
    since = today_kst() - dt.timedelta(days=g["lookback_days"])
    cache = read_json(STATE / "price_cache.json", {})
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    sample = False
    if args.demo:
        client = applyhome.ApplyhomeClient("demo", a["base_url"],
                                           getter=fixture_getter(json.loads(FIXTURE.read_text("utf-8"))))
        sample = True
    elif key:
        client = applyhome.ApplyhomeClient(key, a["base_url"], a["per_page"], a["max_pages"])
    else:
        warnings.append("DATA_GO_KR_KEY 가 없어 청약 일정은 지난 실행 결과를 그대로 둡니다.")
        return prev.get("schedules", []), prev.get("sample", False)

    items, warns = applyhome.collect(client, a["sources"], g["regions"], since, cache,
                                     fetch_prices=a["fetch_prices"] and not args.skip_prices)
    warnings.extend(warns)
    if not sample:
        write_json(STATE / "price_cache.json", _trim_cache(cache, {i["id"] for i in items}))

    if not sample:
        try:
            update_history(client, items, g["regions"], cfg)
        except applyhome.ApiError:
            raise
        except Exception as e:
            warnings.append(f"과거 공고 이력 갱신 실패: {e}")
    cutoff = (today_kst() - dt.timedelta(days=g["keep_past_days"])).isoformat()
    keep = [i for i in items if (i["last_date"] or i["notice_date"] or "9999") >= cutoff]
    return keep, sample


HISTORY = STATE / "notice_history.json"


def update_history(client, items: list[dict], regions: list[str], cfg: dict) -> None:
    """과거 APT 공고 이력(기본 4년) — '이미 분양한 정비구역'을 주소로 가려내는 데 씀.
    처음 한 번은 길게 받고, 그다음부턴 마지막 공고일 30일 전부터만 받음."""
    hist = read_json(HISTORY, {"items": {}})
    years = cfg.get("pipeline", {}).get("history_years", 4)
    last = max((v.get("notice_date") or "" for v in hist["items"].values()), default="")
    since = (dt.date.fromisoformat(last) - dt.timedelta(days=30)) if last else today_kst() - dt.timedelta(days=365 * years)
    rows = client.fetch_rows(applyhome.SOURCES["apt"], since)
    for r in rows:
        n = applyhome.normalize(r, applyhome.SOURCES["apt"])
        if n and (not regions or n["region"] in regions):
            hist["items"][n["id"]] = {k: n.get(k) for k in ("name", "region", "sigungu", "address", "notice_date", "url")}
    for n in items:
        if n.get("source") == "apt":
            hist["items"][n["id"]] = {k: n.get(k) for k in ("name", "region", "sigungu", "address", "notice_date", "url")}
    write_json(HISTORY, hist, compact=True)
    log.info("과거 공고 이력 %d건 (이번에 %s 이후 %d건 확인)", len(hist["items"]), since, len(rows))


def _trim_cache(cache: dict, live_ids: set[str]) -> dict:
    # 지금 보이는 공고 + 최근 것만 남겨 파일이 무한정 커지지 않게
    if len(cache) <= 2000:
        return cache
    return {k: v for k, v in cache.items() if k in live_ids}


# ------------------------------------------------------------------ 공고별 부가 정보
def enrich_schedules(schedules: list[dict], cfg: dict, args, warnings: list[str]) -> None:
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    a = cfg["applyhome"]
    # 경쟁률·당첨가점: 1순위 접수가 끝난 공고만
    if key and not args.demo and a.get("fetch_competition", True):
        cache = read_json(STATE / "competition_cache.json", {})
        client = applyhome.ApplyhomeClient(key, a["base_url"])
        today = today_kst().isoformat()
        fail = 0
        for s in schedules:
            done = [e for e in s["events"] if e["label"] not in ("당첨자 발표", "계약") and e["end"] < today]
            if not done:
                continue
            c = cache.get(s["id"])
            final = c and c.get("available") and (s["events"][-1]["end"] < today)
            if not final and fail < 2:
                try:
                    c = applyhome.fetch_competition(client, s["source"], s["house_manage_no"])
                    cache[s["id"]] = c
                except Exception as e:
                    fail += 1
                    if fail == 2:
                        warnings.append(f"경쟁률 API 조회 실패({e}) — data.go.kr '청약홈 청약접수 경쟁률' 활용신청을 확인하세요.")
            if c:
                s["competition"] = c
        write_json(STATE / "competition_cache.json", cache)
    mcfg = cfg.get("market", {})
    gcfg = cfg.get("geo", {})
    g = geo.Geocoder(STATE / "geocode.json", max_new=0 if args.no_geo else gcfg.get("max_new_per_run", 150),
                     nominatim_delay=gcfg.get("nominatim_delay", 1.1), max_seconds=gcfg.get("comps_max_seconds", 300))
    try:
        market.enrich(schedules, None if args.demo else key, mcfg, warnings, geo=g)
    finally:
        g.save()
        _merge_geo_status(geo.status(g))
    if not any(g.key_set.values()):
        warnings.append("지도 키(KAKAO_REST_KEY 등)가 없어 주변 단지까지의 실제 거리를 잴 수 없습니다 — 추천 순위는 거리 근거가 있는 공고만 매깁니다.")
    for prov, msg in g.key_error.items():
        warnings.append(f"{prov} 지도 키가 거절됐습니다({msg}). 카카오는 REST API 키인지, 앱의 [카카오맵] 사용 설정이 ON인지 확인하세요.")
    if not args.no_news and cfg.get("news", {}).get("enabled", True):
        ncache = read_json(STATE / "news_cache.json", {})
        news.enrich(schedules, ncache, warnings)
        live = {s["name"] for s in schedules}
        write_json(STATE / "news_cache.json", {k: v for k, v in ncache.items() if k in live})
    else:
        for s in schedules:
            s.setdefault("news", []); s["crosscheck"] = news.cross_check(s, {})
    for s in schedules:
        s["restrict"] = rules.restrictions(s)
        s["conditions"] = rules.conditions(s)
        s["regulation"] = rules.regulation(s)[0]


# ------------------------------------------------------------------ 지도 좌표
GEO_STATUS: dict[str, str] = {}


def _merge_geo_status(st: dict) -> None:
    rank = lambda v: 3 if v.startswith("rejected") else 2 if v.startswith("ok") else 1 if v == "unused" else 0
    for k, v in st.items():
        if rank(v) >= rank(GEO_STATUS.get(k, "none")):
            GEO_STATUS[k] = v


def locate_all(schedules: list[dict], pipe: list[dict], cfg: dict, args) -> None:
    gcfg = cfg.get("geo", {})
    g = geo.Geocoder(STATE / "geocode.json", max_new=0 if args.no_geo else gcfg.get("max_new_per_run", 150),
                     nominatim_delay=gcfg.get("nominatim_delay", 1.1), max_seconds=gcfg.get("max_seconds", 240))
    for s in schedules:
        s.setdefault("sigungu", None)
    # 분양 가까운 것부터: 관심·분양예정 → 관리처분·착공 → 나머지
    order = sorted(pipe, key=lambda p: (0 if p.get("watched") or p.get("stage") == "분양예정" else
                                        1 if p.get("stage") in ("관리처분", "착공") else 2))
    try:
        geo.attach(schedules, g, stage="dong")    # 1차: 전부 '동' 단위로 빠르게
        geo.attach(order, g, stage="dong")
        geo.attach(schedules, g)                  # 2차: 남은 예산으로 단지 위치 찾기
        geo.attach(order, g)
    finally:
        g.save()
        _merge_geo_status(geo.status(g))
    from collections import Counter
    cnt = Counter(x.get("geo") for x in schedules + pipe)
    log.info("지도 좌표: 단지·번지 %d / 동 %d / 구 중심 %d (남은 조회 %d)",
             cnt.get("exact", 0), cnt.get("dong", 0), cnt.get("area", 0), g.budget)


# ------------------------------------------------------------------ 정비사업
def collect_pipeline(cfg: dict, args, warnings: list[str]) -> tuple[list[dict], str | None]:
    sp = cfg["seoul_pipeline"]
    raw_dir = ROOT / "data" / "raw" / "seoul"
    seoul: list[dict] = []
    as_of = None
    if sp.get("enabled", True):
        if not args.no_download and not args.demo:
            try:
                pipeline.download_seoul_latest(sp, raw_dir)
            except Exception as e:
                warnings.append(f"서울 정비사업 파일 자동 다운로드 실패(기존 파일 사용): {e}")
        path = pipeline.latest_local_seoul(raw_dir)
        if path:
            try:
                seoul = pipeline.parse_seoul_xlsx(path)
                as_of = seoul[0]["as_of"] if seoul else None
            except Exception as e:
                warnings.append(f"서울 정비사업 파일 파싱 실패 {path.name}: {e}")
        else:
            warnings.append("서울 정비사업 파일이 없습니다. data/raw/seoul/ 에 xlsx를 넣어 주세요.")
    watch = pipeline.load_watchlist(ROOT / "data" / "watchlist.csv")
    return pipeline.merge_pipeline(seoul, watch), as_of


# ------------------------------------------------------------------ 변경 감지
def _sched_sig(s: dict) -> str:
    return "|".join(f"{e['label']}:{e['start']}~{e['end']}" for e in s.get("events", []))


def detect_changes(prev_snap: dict | None, schedules: list[dict], pipe: list[dict]) -> list[dict]:
    now = now_kst_iso()
    if prev_snap is None:
        return [{"at": now, "kind": "init", "title": "기준 데이터 생성",
                 "detail": f"청약 {len(schedules)}건, 정비사업 {len(pipe)}곳"}]
    out = []
    ps, pp = prev_snap.get("schedules", {}), prev_snap.get("pipeline", {})
    for s in schedules:
        old = ps.get(s["id"])
        label = f"[{s['region']}] {s['name']}"
        if old is None:
            first = s["events"][0] if s["events"] else None
            out.append({"at": now, "kind": "new_notice", "ref": s["id"], "title": f"새 공고 {label}",
                        "detail": f"{s['kind']} · {s.get('units') or '-'}세대"
                                  + (f" · {first['label']} {first['start']}" if first else "")})
        elif old != _sched_sig(s):
            out.append({"at": now, "kind": "schedule_change", "ref": s["id"], "title": f"일정 변경 {label}",
                        "detail": "정정공고 가능성 — 원문 확인"})
    for p in pipe:
        old = pp.get(p["id"])
        label = f"[{p.get('sigungu') or p.get('region')}] {p['name']}"
        if old is None:
            out.append({"at": now, "kind": "new_zone", "ref": p["id"], "title": f"신규 등록 {label}",
                        "detail": f"{p.get('type') or ''} · {p.get('stage') or '-'}"})
        elif old != (p.get("stage") or ""):
            out.append({"at": now, "kind": "stage_change", "ref": p["id"], "title": f"단계 변경 {label}",
                        "detail": f"{old or '-'} → {p.get('stage')}"})
    return out


def snapshot(schedules: list[dict], pipe: list[dict]) -> dict:
    return {"schedules": {s["id"]: _sched_sig(s) for s in schedules},
            "pipeline": {p["id"]: p.get("stage") or "" for p in pipe}}


# ------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--skip-prices", action="store_true")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--no-geo", action="store_true", help="지도 좌표 조회 생략(캐시만 사용)")
    ap.add_argument("--no-marketmap", action="store_true", help="시세 지도 데이터 생략")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config()
    prev = read_json(OUT, {})
    warnings: list[str] = []

    try:
        schedules, sample = collect_schedules(cfg, args, prev, warnings)
        rules.learn_from(schedules)
    except applyhome.ApiError as e:
        log.error("청약홈 API 인증/요청 오류: %s", e)
        warnings.append(f"청약홈 API 오류: {e} — 인증키(Decoding 키)와 활용신청 승인 여부를 확인하세요.")
        schedules, sample = prev.get("schedules", []), prev.get("sample", False)

    enrich_schedules(schedules, cfg, args, warnings)
    pipe, seoul_as_of = collect_pipeline(cfg, args, warnings)
    locate_all(schedules, pipe, cfg, args)
    try:
        up = upcoming.build(pipe, schedules, with_news=not args.no_news and cfg.get("news", {}).get("enabled", True))
    except Exception as e:
        warnings.append(f"분양 예정 정리 실패: {e}")
        up = {"planned": [], "candidates": [], "news": []}

    snap_path = STATE / ("snapshot_demo.json" if args.demo else "snapshot.json")
    changes_path = STATE / ("changes_demo.json" if args.demo else "changes.json")
    prev_snap = read_json(snap_path, None)
    new_changes = detect_changes(prev_snap, schedules, pipe)
    try:
        notify.run(schedules, cfg, cfg["general"].get("regions", []))
    except Exception as e:                       # 알림 실패가 갱신을 막지 않도록
        log.warning("알림 실패: %s", e)
    changes = (new_changes + read_json(changes_path, []))[:500]
    write_json(snap_path, snapshot(schedules, pipe))
    write_json(changes_path, changes)

    data = {
        "generated_at": now_kst_iso(),
        "sample": sample,
        "regions": cfg["general"]["regions"],
        "stages": pipeline.STAGES,
        "default_min_stage": cfg["pipeline"]["default_min_stage"],
        "seoul_as_of": seoul_as_of,
        "schedules": schedules,
        "pipeline": pipe,
        "changes": changes[:200],
        "warnings": warnings,
        "ranking": market.rank(schedules),
        "guide": guide.build_guide(),
        "upcoming": up,
        "geo_status": GEO_STATUS,
        "official_sources": {"seoul_pipeline": cfg.get("seoul_pipeline", {}).get("dataset_page"),
                             "policy": "정부·지자체·공공기관(청약홈·LH·SH·GH·한국부동산원) 발표만 근거로 링크합니다."},
    }
    fcfg = cfg.get("focus", {})
    for x in schedules + pipe + up.get("planned", []) + up.get("candidates", []):
        x["core"] = rules.is_core(x, fcfg)
    data["focus"] = {"core_only": fcfg.get("core_only", True), "min_expected_eok": fcfg.get("min_expected_eok", 10),
                     "definition": "핵심지역 = 정부 지정 규제지역(투기과열지구·조정대상지역, 10·15 대책 서울 전역 + 경기 12곳)"
                                   + (f" + {', '.join(fcfg['include'])}" if fcfg.get("include") else "")
                                   + (f" − {', '.join(fcfg['exclude'])}" if fcfg.get("exclude") else ""),
                     "source": "https://www.korea.kr/news/policyNewsView.do?newsId=148952155"}
    dropped: list[str] = []
    data = sources.sanitize(data, dropped=dropped)       # 비공식 링크·근거 제거 (최종 관문)
    if dropped:
        log.info("공식 출처가 아닌 링크·근거 %d개 제외 (예: %s)", len(dropped), ", ".join(dropped[:3]))
    write_json(OUT, data, compact=True)
    for w in warnings:
        log.warning(w)
    log.info("완료: 청약 %d건, 정비사업 %d곳, 새 변경 %d건 → %s",
             len(schedules), len(pipe), len(new_changes), OUT.relative_to(ROOT))
    try:                                       # 예정 호재 (data/catalysts.csv → docs/data/catalysts.json)
        from . import catalysts
        from .marketmap import DEFAULT_AREAS
        places = sorted({w for a in (cfg.get("marketmap", {}).get("areas") or DEFAULT_AREAS) for w in a["sigungu"].split()}
                        | {"분당", "판교", "광교", "동탄", "위례", "과천"})
        catalysts.build(places, with_feed=not (args.no_news or args.demo))
    except Exception as e:
        log.warning("예정 호재 목록 갱신 실패: %s", e)
    if not (args.demo or args.no_marketmap):
        try:                                   # 실거래가 분석 지도 (docs/market) — 실패해도 청약 레이더 갱신은 그대로
            from . import marketmap
            mm = cfg.get("marketmap", {})
            gm = geo.Geocoder(STATE / "geocode.json", max_new=0 if args.no_geo else mm.get("geocode_per_run", 4000),
                              max_seconds=mm.get("geocode_max_seconds", 600))
            try:
                marketmap.build(cfg, os.environ.get("DATA_GO_KR_KEY", "").strip() or None, gm, pipeline=pipe)
            finally:
                gm.save()
        except Exception as e:
            log.warning("시세 지도 갱신 실패: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
