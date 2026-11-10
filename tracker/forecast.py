"""Scenario(시나리오 예측) — 실거래가만으로 만드는 기준 예측·백테스트·재건축 단계 사건 연구.

1) 지수: 단지×면적대 고정효과 지수 (같은 단지·같은 면적대끼리만 비교 → 어떤 집이 팔렸는지에 따른 착시 제거)
       log(전용㎡당 가격) = α(단지·면적대) + δ(분기) + ε.  반복매매지수(Bailey·Muth·Nourse 1963, Case·Shiller)와 같은 원리.
2) 기준 예측: 관성 + 평균 회귀 (Capozza·Hendershott·Mack 2004 의 식)
       Δp_t − g = ρ·(Δp_{t−1} − g) + κ·(p*_{t−1} − p_{t−1}) + ε      (반기 단위)
       p* = 그 지역 로그 지수의 장기 추세선(기울기 g). ρ = 관성, κ = 추세로 되돌아가는 속도. ρ·κ 는 모든 지역을 묶어 추정.
       미래 경로는 그 지역의 과거 오차 ε 를 다시 뽑아(부트스트랩) 2,000번 모의 → 1~5년 뒤 범위(10·25·50·75·90%).
3) 백테스트: 과거 각 시점까지의 자료만으로 같은 방법을 돌려 1년·3년 뒤를 맞혔는지. '변화 없음'·'추세 연장'과 오차 비교,
       80% 범위 안에 실제가 들어온 비율(이상적이면 80%).
4) 재건축 단계 사건 연구: 서울시 정비사업 현황의 단계별 인가일 전후 12개월, 그 단지의 가격을 같은 구 지수와 비교한 초과 변화.

한계: 장기 추세선은 임대료·소득 같은 기초 여건이 아니라 과거 가격만으로 그은 것. 금리·주식·지하철 시나리오는 아직 없음.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import random
import statistics as st
from collections import defaultdict

from .util import ROOT, now_kst_iso, write_json

log = logging.getLogger(__name__)
OUT = ROOT / "docs" / "data" / "forecast"
MIN_HALVES = 24            # 예측하려면 반기 지수가 최소 12년치
MIN_Q_TRADES = 8           # 분기 지수 한 점에 필요한 거래 수
N_PATHS = 2000
YEARS = [1, 2, 3, 4, 5]
QS = [0.10, 0.25, 0.50, 0.75, 0.90]
EVENTS = {"d_zone": "구역지정", "d_union": "조합설립인가", "d_impl": "사업시행인가", "d_mgmt": "관리처분인가"}


def _qi(date: str) -> int:
    return int(date[:4]) * 4 + (int(date[5:7]) - 1) // 3


def _qname(qi: int) -> str:
    return f"{qi // 4}Q{qi % 4 + 1}"


def _quant(xs: list[float], p: float) -> float:
    s = sorted(xs)
    i = (len(s) - 1) * p
    lo = int(i)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (i - lo)


# ------------------------------------------------------------------ 1) 고정효과 지수
def fe_index(trades: list[dict], key_of, band_of) -> dict:
    """{q0, logidx[분기], n[분기], alpha{단위: 수준}} — logidx 는 첫 유효 분기 = 0, 거래가 부족한 분기는 None."""
    cells: dict[tuple, list] = defaultdict(lambda: [0.0, 0])
    for t in trades:
        if not t.get("area") or not t.get("price"):
            continue
        c = cells[((key_of(t), band_of(t["area"])), _qi(t["date"]))]
        c[0] += math.log(t["price"] / t["area"])
        c[1] += 1
    if not cells:
        return {"q0": None, "logidx": [], "n": [], "alpha": {}}
    obs = [(u, q, s / n, n) for (u, q), (s, n) in cells.items()]
    alpha: dict = defaultdict(float)
    delta: dict = defaultdict(float)
    for _ in range(60):                                   # 번갈아 평균 빼기(두 방향 고정효과의 최소제곱 해로 수렴)
        su, nu = defaultdict(float), defaultdict(int)
        for u, q, y, n in obs:
            su[u] += (y - delta[q]) * n
            nu[u] += n
        for u in su:
            alpha[u] = su[u] / nu[u]
        sq, nq = defaultdict(float), defaultdict(int)
        for u, q, y, n in obs:
            sq[q] += (y - alpha[u]) * n
            nq[q] += n
        move = 0.0
        for q in sq:
            v = sq[q] / nq[q]
            move = max(move, abs(v - delta[q]))
            delta[q] = v
        if move < 1e-6:
            break
    q0, q1 = min(nq), max(nq)
    valid = [q for q in range(q0, q1 + 1) if nq.get(q, 0) >= MIN_Q_TRADES]
    if not valid:
        return {"q0": q0, "logidx": [], "n": [], "alpha": {}}
    base = delta[valid[0]]
    q0 = valid[0]
    return {"q0": q0, "logidx": [delta[q] - base if nq.get(q, 0) >= MIN_Q_TRADES else None for q in range(q0, q1 + 1)],
            "n": [nq.get(q, 0) for q in range(q0, q1 + 1)], "alpha": {u: a + base for u, a in alpha.items()}}


def halves(idx: dict) -> tuple[int | None, list[float]]:
    """분기 로그 지수 → 반기 평균(잡음 줄이기). 비는 반기는 앞뒤를 이어 메움. (h0, [p_h])  h = 연도×2 + (0|1)"""
    if not idx["logidx"]:
        return None, []
    by: dict[int, list] = defaultdict(list)
    for i, v in enumerate(idx["logidx"]):
        if v is not None:
            q = idx["q0"] + i
            by[(q // 4) * 2 + (q % 4) // 2].append(v)
    h0, h1 = min(by), max(by)
    p = [st.fmean(by[h]) if h in by else None for h in range(h0, h1 + 1)]
    for i, v in enumerate(p):                              # 선형 보간
        if v is None:
            a = max(j for j in range(i) if p[j] is not None)
            b = min(j for j in range(i + 1, len(p)) if p[j] is not None)
            p[i] = p[a] + (p[b] - p[a]) * (i - a) / (b - a)
    return h0, p


# ------------------------------------------------------------------ 2) 관성 + 평균 회귀
def _trend(p: list[float]) -> tuple[float, float]:
    n = len(p)
    mx, my = (n - 1) / 2, st.fmean(p)
    sxx = sum((i - mx) ** 2 for i in range(n))
    g = sum((i - mx) * (v - my) for i, v in enumerate(p)) / sxx
    return my - g * mx, g                                   # p*_t = c + g·t


def fit_pooled(series: dict[str, list[float]]) -> dict | None:
    """여러 지역을 묶어 ρ(관성)·κ(평균 회귀)를 추정. series = {code: 반기 로그 지수}"""
    s11 = s12 = s22 = s1y = s2y = 0.0
    rows, tr = {}, {}
    for code, p in series.items():
        if len(p) < 8:
            continue
        c, g = _trend(p)
        tr[code] = (c, g)
        r = []
        for t in range(2, len(p)):
            y = (p[t] - p[t - 1]) - g
            x1 = (p[t - 1] - p[t - 2]) - g
            x2 = (c + g * (t - 1)) - p[t - 1]
            r.append((y, x1, x2))
            s11 += x1 * x1; s12 += x1 * x2; s22 += x2 * x2; s1y += x1 * y; s2y += x2 * y
        rows[code] = r
    det = s11 * s22 - s12 * s12
    n = sum(len(r) for r in rows.values())
    if n < 30 or abs(det) < 1e-12:
        return None
    rho_raw, kap_raw = (s1y * s22 - s2y * s12) / det, (s2y * s11 - s1y * s12) / det
    rho, kap = min(0.9, max(0.0, rho_raw)), min(0.5, max(0.0, kap_raw))
    resid = {code: [y - rho * x1 - kap * x2 for y, x1, x2 in r] for code, r in rows.items()}
    return {"rho": rho, "kappa": kap, "rho_raw": rho_raw, "kappa_raw": kap_raw, "n": n, "trend": tr, "resid": resid}


def simulate(p: list[float], c: float, g: float, rho: float, kap: float, resid: list[float], steps: int,
             paths: int, rng: random.Random) -> list[list[float]]:
    """[경로][단계] = 시작점 대비 누적 로그 변화. 오차는 과거 오차를 2개씩 묶어 다시 뽑음(변동성이 몰리는 성질 유지)."""
    out, T = [], len(p)
    for _ in range(paths):
        lvl, d, path = p[-1], p[-1] - p[-2], []
        for s in range(steps):
            if s % 2 == 0:
                k = rng.randrange(len(resid))
            e = resid[(k + s % 2) % len(resid)]
            d = g + rho * (d - g) + kap * ((c + g * (T - 1 + s)) - lvl) + e
            lvl += d
            path.append(lvl - p[-1])
        out.append(path)
    return out


def _summ(paths: list[list[float]], step: int) -> tuple[list[float], float]:
    xs = [pp[step] for pp in paths]
    return [_quant(xs, q) for q in QS], st.pstdev(xs)


def backtest(series: dict[str, list[float]], h0s: dict[str, int], min_train: int = 16, paths: int = 300) -> dict:
    """과거 시점마다(그때까지의 자료만) 예측 → 1년(2반기)·3년(6반기) 뒤 실제와 비교."""
    rec = {2: [], 6: []}
    T = {c: len(p) for c, p in series.items()}
    h_all = sorted({h0s[c] + i for c, p in series.items() for i in range(len(p))})
    for origin in h_all:
        train = {c: p[: origin - h0s[c] + 1] for c, p in series.items() if min_train <= origin - h0s[c] + 1 <= T[c] - 2}
        if len(train) < 1:
            continue
        m = fit_pooled(train)
        if not m:
            continue
        for c, p in train.items():
            cc, g = m["trend"][c]
            res = m["resid"][c] if len(m["resid"][c]) >= 8 else [e for r in m["resid"].values() for e in r]
            sims = simulate(p, cc, g, m["rho"], m["kappa"], res, 6, paths, random.Random(hash((c, origin)) & 0xFFFF))
            full = series[c]
            for h in (2, 6):
                if len(p) - 1 + h >= len(full):
                    continue
                actual = full[len(p) - 1 + h] - p[-1]
                q, _ = _summ(sims, h - 1)
                rec[h].append({"code": c, "model": q[2], "lo": q[0], "hi": q[4], "trend": g * h, "actual": actual})
    out = {}
    for h, rows in rec.items():
        if len(rows) < 10:
            out[f"y{h // 2}"] = None
            continue
        ape = lambda pred: st.median(abs(math.exp(r[pred] - r["actual"]) - 1) for r in rows) * 100
        out[f"y{h // 2}"] = {
            "n": len(rows),
            "err_model": round(ape("model"), 1),
            "err_nochange": round(st.median(abs(math.exp(-r["actual"]) - 1) for r in rows) * 100, 1),
            "err_trend": round(ape("trend"), 1),
            "cover80": round(sum(r["lo"] <= r["actual"] <= r["hi"] for r in rows) / len(rows) * 100),
            "dir_hit": round(sum((r["model"] > 0) == (r["actual"] > 0) for r in rows) / len(rows) * 100),
            # 보정 배율: 과거 예측에서 '실제 오차 ÷ 모의 범위의 반폭'의 80번째 백분위. 1보다 크면 모의 범위가 너무 좁았다는 뜻
            # (추세·계수 추정의 불확실성이 모의에 안 들어가기 때문) → 예측 범위를 이만큼 넓혀 과거 기준 80%가 들어오게 맞춤.
            "scale": round(max(1.0, _quant([abs(r["actual"] - r["model"]) / max(1e-9, (r["hi"] - r["lo"]) / 2) for r in rows], 0.8)), 2),
        }
    return out


# ------------------------------------------------------------------ 4) 재건축 단계 사건 연구
def _add_months(d: dt.date, m: int) -> str:
    y, mm = divmod(d.year * 12 + d.month - 1 + m, 12)
    return f"{y:04d}-{mm + 1:02d}-{min(d.day, 28):02d}"


def event_rows(trades: list[dict], idx: dict, zones: list[dict], jibun_key) -> list[dict]:
    """정비구역(재건축)과 같은 동·본번의 거래를 찾아, 단계 인가일 전후 12개월의 '구 지수 대비 수준' 변화를 잰다."""
    if not idx["logidx"]:
        return []
    by_lot: dict[tuple, list] = defaultdict(list)
    for t in trades:
        by_lot[(t.get("dong"), str(t.get("jibun") or "").split("-")[0])].append(t)
    q0, li = idx["q0"], idx["logidx"]

    def level(ts, a, b):
        xs = []
        for t in ts:
            if a <= t["date"] < b:
                i = _qi(t["date"]) - q0
                if 0 <= i < len(li) and li[i] is not None:
                    xs.append(math.log(t["price"] / t["area"]) - li[i])
        return st.fmean(xs) if len(xs) >= 3 else None

    out = []
    for z in zones:
        if z.get("type") != "재건축":
            continue
        ts = by_lot.get(jibun_key(z.get("address")) or (None, None))
        if not ts:
            continue
        for k, label in EVENTS.items():
            d = (z.get("milestones") or {}).get(k)
            if not d:
                continue
            try:
                e = dt.date.fromisoformat(d)
            except ValueError:
                continue
            pre2, pre = level(ts, _add_months(e, -24), _add_months(e, -12)), level(ts, _add_months(e, -12), d)
            post, post2 = level(ts, d, _add_months(e, 12)), level(ts, _add_months(e, 12), _add_months(e, 24))
            if pre is None or post is None:
                continue
            out.append({"zone": z.get("name"), "gu": z.get("sigungu"), "event": label, "date": d,
                        "run_up": None if pre2 is None else pre - pre2, "jump": post - pre,
                        "follow": None if post2 is None else post2 - post})
    return out


def idio_sd(trades: list[dict], idx: dict, key_of, band_of) -> float | None:
    """단지 고유 변동: (단지·면적대) 수준이 구 지수 대비 1년에 얼마나 따로 움직이는가 — 로그 차이의 강건 표준편차(MAD×1.4826)."""
    if not idx["logidx"]:
        return None
    by: dict[tuple, list] = defaultdict(list)
    q0, li = idx["q0"], idx["logidx"]
    for t in trades:
        i = _qi(t["date"]) - q0
        if 0 <= i < len(li) and li[i] is not None:
            by[((key_of(t), band_of(t["area"])), int(t["date"][:4]))].append(math.log(t["price"] / t["area"]) - li[i])
    lv = {k: st.fmean(v) for k, v in by.items() if len(v) >= 2}
    diffs = [lv[(u, y)] - lv[(u, y - 1)] for (u, y) in lv if (u, y - 1) in lv]
    if len(diffs) < 30:
        return None
    m = st.median(diffs)
    return 1.4826 * st.median(abs(d - m) for d in diffs)


# ------------------------------------------------------------------ 묶기
def prepare(code: str, gu: str, label: str, region: str, trades: list[dict], zones: list[dict], hist_from: str) -> dict:
    """한 지역의 거래(전체 기간)를 지수·사건 표로 줄여서 돌려준다(거래 원본은 붙들지 않음)."""
    from .marketmap import band_of, ckey
    from .upcoming import _jibun_key
    idx = fe_index(trades, ckey, band_of)
    h0, p = halves(idx)
    return {"code": code, "gu": gu, "label": label, "region": region, "hist_from": hist_from,
            "q0": idx["q0"], "logidx": idx["logidx"], "nq": idx["n"], "h0": h0, "p": p,
            "sigma": idio_sd(trades, idx, ckey, band_of),
            "events": event_rows(trades, idx, zones, _jibun_key)}


def finalize(preps: list[dict], backfill_left: int = 0) -> dict:
    ok = {g["code"]: g for g in preps if len(g["p"]) >= MIN_HALVES}
    series = {c: g["p"] for c, g in ok.items()}
    model = fit_pooled(series) if series else None
    bt = backtest(series, {c: g["h0"] for c, g in ok.items()}) if model else {}
    s1 = (bt.get("y1") or {}).get("scale", 1.0)
    s3 = (bt.get("y3") or {}).get("scale", s1)
    scale = {1: s1, 2: (s1 + s3) / 2, 3: s3, 4: s3, 5: s3}
    gus = []
    for g in preps:
        o = {"code": g["code"], "gu": g["gu"], "label": g["label"], "region": g["region"], "hist_from": g["hist_from"],
             "q0": _qname(g["q0"]) if g["q0"] is not None else None,
             "idx": [None if v is None else round(math.exp(v) * 100, 1) for v in g["logidx"]],
             "sigma": None if g["sigma"] is None else round(g["sigma"], 4), "ready": False}
        if model and g["code"] in ok:
            p = g["p"]
            c, gr = model["trend"][g["code"]]
            res = model["resid"][g["code"]]
            if len(res) < 8:
                res = [e for r in model["resid"].values() for e in r]
            sims = simulate(p, c, gr, model["rho"], model["kappa"], res, 10, N_PATHS, random.Random(int(g["code"])))
            fc = []
            for y in YEARS:                                   # 백테스트 보정 배율로 중앙값 둘레의 범위를 넓힘
                q, sd = _summ(sims, y * 2 - 1)
                fc.append(([q[2] + (v - q[2]) * scale[y] for v in q], sd * scale[y]))
            o.update(ready=True,
                     trend_pct=round((math.exp(gr * 2) - 1) * 100, 1),                       # 장기 추세(연율)
                     gap_pct=round((math.exp(p[-1] - (c + gr * (len(p) - 1))) - 1) * 100, 1),   # 지금 수준이 추세선보다 몇 % 위/아래
                     last_pct=round((math.exp(p[-1] - p[-3]) - 1) * 100, 1) if len(p) >= 3 else None,   # 최근 1년
                     vol_pct=round(st.pstdev(res) * math.sqrt(2) * 100, 1),                  # 예측 오차의 연율 변동성
                     fc=[[round((math.exp(v) - 1) * 100, 1) for v in q] for q, _ in fc],
                     fc_sd=[round(sd, 4) for _, sd in fc], fc_med_log=[round(q[2], 4) for q, _ in fc])
        gus.append(o)
    ev = [e for g in preps for e in g["events"]]
    events = {}
    for label in EVENTS.values():
        rows = [e for e in ev if e["event"] == label]
        def q3(k):
            xs = [e[k] for e in rows if e[k] is not None]
            return None if len(xs) < 5 else {"n": len(xs), "q": [round((math.exp(_quant(xs, p)) - 1) * 100, 1) for p in (.25, .5, .75)],
                                              "pos": round(sum(x > 0 for x in xs) / len(xs) * 100)}
        events[label] = {"n": len(rows), "run_up": q3("run_up"), "jump": q3("jump"), "follow": q3("follow")}
    out = {
        "generated_at": now_kst_iso(), "years": YEARS, "quantiles": QS, "ready": bool(model),
        "backfill_left": backfill_left, "min_years": MIN_HALVES // 2,
        "model": None if not model else {
            "rho": round(model["rho"], 3), "kappa": round(model["kappa"], 3),
            "rho_raw": round(model["rho_raw"], 3), "kappa_raw": round(model["kappa_raw"], 3), "n": model["n"],
            "areas": len(series), "step": "반기",
            "half_life_years": None if model["kappa"] <= 0 else round(math.log(2) / model["kappa"] / 2, 1)},
        "backtest": bt, "scale": scale,
        "events": events,
        "event_examples": sorted(({"zone": e["zone"], "gu": e["gu"], "event": e["event"], "date": e["date"],
                                   "jump": round((math.exp(e["jump"]) - 1) * 100, 1)} for e in ev),
                                 key=lambda e: e["date"], reverse=True)[:40],
        "gus": gus,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "summary.json", out, compact=True)
    log.info("시나리오 예측: 지역 %d 중 %d 예측 가능, 재건축 사건 %d건", len(preps), len(series), len(ev))
    return out
