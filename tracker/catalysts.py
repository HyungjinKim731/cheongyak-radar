"""예정 호재(Catalyst) — 법적 효력이 있는 고시·공식 발표만 싣는다.

data/catalysts.csv 를 사람이 관리한다(고시는 API가 없음). source_url 이 정부·지자체·공공기관 주소가 아닌 줄,
좌표나 단계가 빠진 줄은 버린다. 결과는 docs/data/catalysts.json → 실거래가 분석 지도와 첫 화면이 읽는다.

단계(stage) — 뒤로 갈수록 확실:
  계획      국가철도망 구축계획·광역교통시행계획·도시철도망 구축계획에 반영(고시)
  예타통과  예비타당성조사 통과(기획재정부 발표)
  기본계획  기본계획 고시 — 역 위치가 이때 정해짐
  착공      실시계획 승인·착공
  개통      운영 시작
"""
from __future__ import annotations

import csv
import logging

from . import sources
from .util import ROOT, now_kst_iso, write_json

log = logging.getLogger(__name__)
CSV = ROOT / "data" / "catalysts.csv"
OUT = ROOT / "docs" / "data" / "catalysts.json"
STAGES = ["계획", "예타통과", "기본계획", "착공", "개통"]
KINDS = ["지하철역", "철도역", "도로", "정비사업", "기타"]


def load(path=CSV) -> tuple[list[dict], list[str]]:
    items, dropped = [], []
    if not path.exists():
        return items, dropped
    with path.open(encoding="utf-8-sig") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            name = (r.get("name") or "").strip()
            if not name or name.startswith("#"):
                continue
            try:
                lat, lng = float(r["lat"]), float(r["lng"])
            except (TypeError, ValueError, KeyError):
                dropped.append(f"{i}행 {name}: 좌표 없음")
                continue
            stage = (r.get("stage") or "").strip()
            if stage not in STAGES:
                dropped.append(f"{i}행 {name}: 단계는 {'/'.join(STAGES)} 중 하나")
                continue
            url = (r.get("source_url") or "").strip()
            if not sources.is_official(url):
                dropped.append(f"{i}행 {name}: 공식 출처 주소가 아님")
                continue
            items.append({
                "kind": (r.get("kind") or "기타").strip(), "line": (r.get("line") or "").strip(), "name": name,
                "lat": lat, "lng": lng, "stage": stage, "stage_i": STAGES.index(stage),
                "notice_no": (r.get("notice_no") or "").strip(), "notice_date": (r.get("notice_date") or "").strip(),
                "open_target": (r.get("open_target") or "").strip(), "source_url": url, "org": sources.org(url),
                "memo": (r.get("memo") or "").strip(),
            })
    return items, dropped


# ------------------------------------------------------------------ 교통 호재 소식 자동 수집
# 정부·지자체·공공기관이 낸 보도자료만(뉴스 검색 결과 중 공식 출처만 남김) 모아 제목의 낱말로 종류·단계·지역을 붙인다.
# 좌표가 없으므로 지도 핀이 되지는 않는다 — 핀은 고시 원문을 확인해 catalysts.csv 에 적은 것만.
FEED_STATE = ROOT / "data" / "state" / "catalyst_feed.json"
# 교통 사업을 직접 결정·시행하는 기관의 누리집만: 국토교통부, 정책브리핑(부처 보도자료), 서울시, 경기도, 기획재정부(예타)
FEED_SITES = "(site:molit.go.kr OR site:korea.kr OR site:seoul.go.kr OR site:gg.go.kr OR site:moef.go.kr)"
QUERIES = ["intitle:착공 (철도 OR 지하철 OR GTX OR 고속도로 OR 전철)", "intitle:개통 (철도 OR 지하철 OR GTX OR 고속도로 OR 호선 OR 전철)",
           "intitle:기본계획 (철도 OR 도시철도 OR 광역철도) (승인 OR 고시 OR 확정)", "intitle:예비타당성 (철도 OR 고속도로 OR 지하철)",
           "intitle:국가철도망 구축계획", "intitle:광역교통 (시행계획 OR 개선대책) 확정", "intitle:연장 (호선 OR 지하철) (승인 OR 확정 OR 착공)"]
KIND_WORDS = [("철도·지하철", ("지하철", "도시철도", "호선", "경전철", "GTX", "광역급행", "광역철도", "철도", "전철", "KTX", "SRT", "트램",
                         "신사선", "과천선", "면목선", "서부선", "동북선", "강북횡단선", "신분당선", "신안산선", "월곶", "인덕원")),
              ("도로", ("고속도로", "고속화도로", "순환로", "순환도로", "나들목", "하이패스IC", "지하도로", "지하고속도로"))]
STAGE_WORDS = [("예타통과", ("예타 통과", "예비타당성조사 통과", "예비타당성 조사 통과", "예비타당성 통과", "타당성 통과", "예타 면제")),
               ("기본계획", ("기본계획",)),
               ("개통", ("개통",)), ("착공", ("착공", "기공식", "실시계획 승인", "첫 삽")),
               ("계획", ("국가철도망", "도시철도망", "광역교통", "구축계획"))]
# 결정·사실이 아닌 글(요구·희망·행사·해명·입찰 등)은 뺀다
NEG_WORDS = ("촉구", "건의", "요청", "해야", "의원", "환영", "기원", "염원", "서명", "토론회", "간담회", "공청회", "설명회", "유치",
             "반영을", "총력", "앞장", "점검", "캠페인", "수칙", "가이드", "기념", "설명자료", "설명]", "팩트", "입찰", "용역", "양식",
             "협력하겠", "모집", "국지도", "국도", "지방도", "개통될", "개통 예정", "개통 목표", "블로그", "카드/한컷", "멀티미디어", "연구", "영향평가", "주년", "대응", "지연", "우려", "주택", "택지", "신도시 입주")
# 서울·경기 밖 사업은 뺀다(제목에 수도권·서울·경기가 함께 있으면 남김)
OTHER_REGIONS = ("새만금", "부산", "대구", "광주", "대전", "울산", "세종", "강원", "충북", "충남", "충청", "전북", "전남", "전라", "경북", "경남",
                 "경상", "제주", "가덕도", "군산", "전주", "포항", "창원", "청주", "천안", "춘천", "원주", "강릉", "목포", "여수", "순천", "영천", "홍천")
CAPITAL = ("수도권", "서울", "경기", "GTX")


def classify(title: str, places: list[str]) -> dict | None:
    if any(w in title for w in NEG_WORDS):
        return None
    kind = next((k for k, ws in KIND_WORDS if any(w in title for w in ws)), None)
    stage = next((s for s, ws in STAGE_WORDS if any(w in title for w in ws)), None)
    if not kind or not stage:
        return None
    if any(w in title for w in OTHER_REGIONS) and not any(w in title for w in CAPITAL):
        return None
    hit = [p for p in places if p in title or (len(p) > 2 and p[:-1] in title)]      # '분당구' → '분당' 도 인정
    return {"kind": kind, "stage": stage, "stage_i": STAGES.index(stage), "places": hit}


def feed(places: list[str], session=None, limit_days: int = 730) -> list[dict]:
    """공식 보도자료를 찾아 누적(data/state/catalyst_feed.json). 실패해도 지난 목록은 유지."""
    import time
    from . import news as news_mod
    from .util import read_json, today_kst
    state = read_json(FEED_STATE, {})
    fails = 0
    for q in QUERIES:
        try:
            arts = news_mod.fetch_query(f"{q} {FEED_SITES} when:365d", session, limit=25)
        except Exception as e:
            fails += 1
            log.warning("교통 호재 소식 조회 실패 %s: %s", q, e)
            if fails >= 4:
                break
            continue
        for a in arts:
            c = classify(a["title"], places)
            if c and a.get("date") and sources.is_official(a.get("source_url")):
                state[a["title"]] = {**c, "title": a["title"], "date": a["date"], "org": a.get("source"),
                                     "url": a["url"], "source_url": a["source_url"]}
        time.sleep(0.6)
    import datetime as dt
    cut = (today_kst() - dt.timedelta(days=limit_days)).isoformat()
    state = {k: v for k, v in state.items() if v["date"] >= cut}
    FEED_STATE.parent.mkdir(parents=True, exist_ok=True)
    write_json(FEED_STATE, state)
    return sorted(state.values(), key=lambda x: x["date"], reverse=True)[:300]


def build(places: list[str] | None = None, with_feed: bool = False) -> dict:
    items, dropped = load()
    for d in dropped:
        log.warning("호재 목록에서 제외: %s", d)
    fd: list[dict] = []
    try:
        if with_feed:
            fd = feed(places or [])
        else:
            from .util import read_json
            fd = sorted(read_json(FEED_STATE, {}).values(), key=lambda x: x["date"], reverse=True)[:300]
    except Exception as e:
        log.warning("교통 호재 소식 수집 실패: %s", e)
    out = {"generated_at": now_kst_iso(), "stages": STAGES, "items": items, "dropped": len(dropped), "feed": fd}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    write_json(OUT, out)
    log.info("예정 호재 %d건 (제외 %d), 교통 소식 %d건", len(items), len(dropped), len(fd))
    return out
