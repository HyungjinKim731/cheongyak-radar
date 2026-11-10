"""python -m unittest discover tests"""
import datetime as dt
import json
import math
import tempfile
import unittest
from pathlib import Path

from tracker import applyhome, pipeline, run
from tracker.util import to_iso

ROOT = Path(__file__).resolve().parent.parent
FIX = json.loads((ROOT / "tests" / "fixtures" / "applyhome_sample.json").read_text("utf-8"))


class DateTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(to_iso("20261102"), "2026-11-02")
        self.assertEqual(to_iso("2026-11-02"), "2026-11-02")
        self.assertEqual(to_iso("2026.11.2"), "2026-11-02")
        self.assertIsNone(to_iso("20260231"))
        self.assertIsNone(to_iso("-"))


class ApplyhomeTest(unittest.TestCase):
    def client(self):
        return applyhome.ApplyhomeClient("k", "https://x", getter=run.fixture_getter(FIX))

    def test_collect_filters_regions_and_prices(self):
        items, warns = applyhome.collect(self.client(), ["apt", "remndr", "opt"], ["서울", "경기"],
                                         dt.date(2026, 9, 1), {}, fetch_prices=True)
        names = {i["name"] for i in items}
        self.assertNotIn("트리븐센텀 2단지", names)      # 부산 제외
        self.assertNotIn("청라 아크원 푸르지오", names)  # 인천 제외
        g = next(i for i in items if i["name"].startswith("고덕강일"))
        self.assertEqual((g["price_min"], g["price_max"]), (29600, 39000))
        self.assertEqual([e["label"] for e in g["events"]][:3], ["1순위 해당", "1순위 기타", "2순위"])
        r = next(i for i in items if i["source"] == "remndr")
        self.assertEqual(r["events"][0]["label"], "청약접수")  # SUBSCRPT_RCEPT_* 사용
        o = next(i for i in items if i["source"] == "opt")
        self.assertEqual(o["events"][0]["end"], "2026-11-03")  # GNRL_RCEPT_* 사용
        self.assertEqual(warns, [])

    def test_lookback(self):
        items, _ = applyhome.collect(self.client(), ["apt"], [], dt.date(2026, 10, 20), {}, fetch_prices=False)
        self.assertTrue(all(i["notice_date"] >= "2026-10-20" for i in items))


class PipelineTest(unittest.TestCase):
    def test_stage_alias(self):
        self.assertEqual(pipeline.normalize_stage("관리처분계획인가"), "관리처분")
        self.assertEqual(pipeline.normalize_stage("조합설립인가"), "조합설립")

    def test_seoul_file(self):
        f = pipeline.latest_local_seoul(ROOT / "data" / "raw" / "seoul")
        if not f:
            self.skipTest("서울 파일 없음")
        rows = pipeline.parse_seoul_xlsx(f)
        self.assertGreater(len(rows), 100)
        self.assertTrue(all(r["region"] == "서울" for r in rows))
        self.assertTrue(any(r["stage"] == "착공" for r in rows))

    def test_watchlist_merge(self):
        seoul = [{"id": "seoul:1", "name": "A", "stage": "관리처분", "stage_order": 6, "region": "서울"}]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.csv"
            p.write_text("id,region,name,stage,expected,memo\n"
                         "seoul:1,서울,A,,2027년 상반기,메모\n"
                         ",경기,B,분양예정,,\n", encoding="utf-8")
            merged = pipeline.merge_pipeline(seoul, pipeline.load_watchlist(p))
        a = next(x for x in merged if x["id"] == "seoul:1")
        self.assertEqual((a["expected"], a["memo"], a["stage"]), ("2027년 상반기", "메모", "관리처분"))
        self.assertEqual(len(merged), 2)


class ChangeTest(unittest.TestCase):
    def test_detect(self):
        s = [{"id": "apt:1:1", "name": "X", "region": "서울", "kind": "APT", "units": 10,
              "events": [{"label": "청약접수", "start": "2026-11-01", "end": "2026-11-01"}]}]
        p = [{"id": "seoul:1", "name": "A", "stage": "착공", "sigungu": "종로구"}]
        self.assertEqual(run.detect_changes(None, s, p)[0]["kind"], "init")
        prev = {"schedules": {}, "pipeline": {"seoul:1": "관리처분"}}
        kinds = {c["kind"] for c in run.detect_changes(prev, s, p)}
        self.assertEqual(kinds, {"new_notice", "stage_change"})


if __name__ == "__main__":
    unittest.main()


class StatsTest(unittest.TestCase):
    def test_summarize(self):
        from tracker.stats import summarize
        st = summarize([{"value": 16.88, "source": "a"}] * 3 + [{"value": 7.85, "source": "b"}])
        self.assertEqual((st["n"], st["mode"], st["mode_n"], st["agreement"]), (4, 16.88, 3, "대체로 일치"))
        self.assertEqual(st["outliers"], ["b"])
        self.assertEqual(summarize([{"value": 24, "source": x} for x in "abc"])["agreement"], "일치")


class NewsTest(unittest.TestCase):
    def test_extract(self):
        arts = [{"title": "아크메르 동탄, 1순위 최고 16.88대 1", "text": "", "source": "A", "url": "u", "date": "d"},
                {"title": "아크메르 동탄 1순위 평균 1.02대 1", "text": "3.3㎡당 1,575만원", "source": "B", "url": "u", "date": "d"}]
        m = news.extract(arts)
        self.assertEqual([v["value"] for v in m["rate_max"]], [16.88])
        self.assertEqual([v["value"] for v in m["rate_avg"]], [1.02])
        self.assertEqual([v["value"] for v in m["pyeong"]], [1575])


class MarketTest(unittest.TestCase):
    def test_sigungu(self):
        self.assertEqual(market.parse_sigungu("경기도 화성시 향남읍 상신리"), "화성시 만세구")
        self.assertEqual(market.parse_sigungu("경기도 수원시 권선구 서둔동"), "수원시 권선구")
        self.assertEqual(market.parse_sigungu("서울특별시 강동구 강일동"), "강동구")

    def test_analyze_and_rules(self):
        n = {"name": "테스트 단지", "region": "경기", "sigungu": "수원시 권선구", "bands": {"84": {"median": 80000}},
             "source": "apt", "kind": "APT · 민영", "flags": {"overheat": "N", "adjust": "N"}, "price_min": 70000, "price_max": 90000}
        comps = [{"target": "테스트", "kind": "실거래", "price_manwon": str(p), "date": "2099-01", "comp_name": c, "source": "s"}
                 for c, p in (("A", 88000), ("B", 90000), ("C", 86000))]
        trends = [{"region": "수원시 권선구", "metric": "3개월", "change_pct": "4.0", "source": "s", "url": "u", "date": "d"}]
        m = market.analyze(n, None, comps, trends, {})
        self.assertEqual(m["expected"], 88000)
        self.assertEqual(m["margin_pct"], 10.0)
        self.assertEqual(m["score"], 14.0)
        self.assertEqual(m["confidence"], "참고용")   # 거리 근거 없는 수동 비교값은 참고용(순위 제외)
        titles = [c["title"] for c in rules.conditions(n)]
        self.assertIn("가점 · 추첨 비율", titles)
        self.assertEqual(rules.regulation({"region": "경기", "sigungu": "수원시 영통구"})[0], "투기과열")
        self.assertEqual(rules.regulation({"region": "경기", "sigungu": "수원시 권선구"})[0], "비규제")


from tracker import market, news, rules  # noqa: E402


class UpcomingGeoTest(unittest.TestCase):
    def test_expected_and_match(self):
        from tracker import upcoming
        self.assertEqual(upcoming.expected_key("2026년 11월"), "2026-11")
        self.assertEqual(upcoming.expected_key("2027년 상반기"), "2027-06")
        self.assertEqual(upcoming.expected_key(None), "9999")
        sch = [{"id": "a", "name": "디에이치 반포124(재건축)"}]
        self.assertEqual(upcoming.match_notice("디에이치 반포124", sch)["id"], "a")
        self.assertIsNone(upcoming.match_notice("경희궁 에피트", sch))

    def test_geo_fallback(self):
        from tracker import geo
        self.assertEqual(geo._clean_addr("종로구 수송동146-12 일원"), "종로구 수송동 146-12")
        self.assertEqual(geo.centroid("경기", "화성시 동탄구"), geo.CENTROID["화성시 동탄구"])
        self.assertEqual(geo.centroid("서울", None), geo.CENTROID["서울"])


class EligEngineTest(unittest.TestCase):
    """docs/app/elig.js 를 node 로 실행해 대표 사례를 확인 (node 없으면 건너뜀)."""
    def test_profile_cases(self):
        import shutil, subprocess
        if not shutil.which("node"):
            self.skipTest("node 없음")
        from tracker import guide
        script = """
global.window = {}; require('./docs/app/elig.js'); const E = window.Elig;
const P = JSON.parse(process.argv[1]);
const base = {birth:'1993-12-15', marital:'engaged', household_head:true, household_size:1, homes_owned:0, officetel:1,
  ever_owned_home:false, sido:'서울', acct_years:15, acct_balance:500, acct_payments:180, income_self:9000, tax_years:6,
  realestate:15000, lease_deposit:20000, children:0, newborn:0, parent65_years:0, spouse_homeless:true};
const s = E.summary(base, P, '2026-11-02');
const st = (arr, k) => arr.find(r => r.key === k).status;
const out = {
  score: E.score(base, '2026-11-02').total,
  youth: st(s.public, 'pub_youth'), prv_new: st(s.private_seoul, 'prv_newlywed'),
  prv_first: st(s.private_seoul, 'prv_firsttime'), pub_first: st(s.public, 'pub_firsttime'),
  remndr: s.etc[0].status, rank1: st(s.private_seoul, 'prv_general'),
  rank1_nohead: st(E.summary({...base, household_head:false}, P, '2026-11-02').private_seoul, 'prv_general'),
  remndr_owner: E.summary({...base, homes_owned:1}, P, '2026-11-02').etc[0].status,
};
console.log(JSON.stringify(out));
"""
        r = subprocess.run(["node", "-e", script, json.dumps(guide.ELIG_PARAMS, ensure_ascii=False)],
                           cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["score"], 28)                 # 무주택 6 + 부양 5 + 통장 17
        self.assertEqual(out["youth"], "no")               # 소득·자산 초과
        self.assertEqual(out["prv_new"], "later")          # 민영 신혼은 혼인신고 후
        self.assertEqual(out["pub_first"], "later")        # 공공 생애최초는 혼인 후
        self.assertIn(out["prv_first"], ("ok", "maybe"))   # 1인 가구 추첨 30%
        self.assertEqual(out["remndr"], "ok")              # 오피스텔은 주택 아님
        self.assertIn(out["rank1"], ("ok", "maybe"))
        self.assertEqual(out["rank1_nohead"], "maybe")     # 규제지역 세대원 → 2순위만
        self.assertEqual(out["remndr_owner"], "no")


class PortfolioTest(unittest.TestCase):
    def test_trade_reduces_shares(self):
        import tempfile, shutil
        from tracker import portfolio as pf
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.json"
            f.write_text(json.dumps({"holdings": [{"ticker": "AAA", "avg": 10, "shares": 10, "currency": "USD"}], "trades": []}), encoding="utf-8")
            old = pf.FILE
            pf.FILE = f
            try:
                r = pf.add_trade({"ticker": "AAA", "qty": 4, "price": 12, "fx": 1300})
                doc = pf.load()
                self.assertEqual(doc["holdings"][0]["shares"], 6.0)
                self.assertEqual(r["avg"], 10)
                with self.assertRaises(ValueError):
                    pf.add_trade({"ticker": "ZZZ", "qty": 1, "price": 1})
                self.assertTrue(pf.delete_trade(r["id"]))
            finally:
                pf.FILE = old


class SourcesTest(unittest.TestCase):
    def test_official_only(self):
        from tracker import sources
        self.assertTrue(sources.is_official("https://www.korea.kr/news/x"))
        self.assertTrue(sources.is_official("https://news.seoul.go.kr/a"))
        self.assertTrue(sources.is_official("https://apply.lh.or.kr/"))
        self.assertTrue(sources.is_official("https://www.applyhome.co.kr/ai/x"))
        self.assertFalse(sources.is_official("https://mhb-blog.com/x"))
        self.assertFalse(sources.is_official("https://www.hankyung.com/a"))
        self.assertFalse(sources.is_official("https://fakego.kr.example.com/"))
        d = {"news": [{"title": "a", "url": "https://news.google.com/x", "source_url": "https://www.korea.kr"},
                      {"title": "b", "url": "https://news.google.com/y", "source_url": "https://www.mt.co.kr"}],
             "vals": [{"value": 1, "url": "https://blog.example.com"}, {"value": 2, "url": "https://www.law.go.kr/x"}],
             "p": {"map_url": "https://map.naver.com/x", "homepage": "https://dev.example.com", "url": "https://www.applyhome.co.kr/"}}
        out = sources.sanitize(d)
        self.assertEqual([n["title"] for n in out["news"]], ["a"])
        self.assertEqual([v["value"] for v in out["vals"]], [2])
        self.assertNotIn("map_url", out["p"])
        self.assertIn("homepage", out["p"])

    def test_guide_links_official(self):
        from tracker import guide, sources
        g = guide.build_guide()
        urls = [x["url"] for x in g["sources"]] + [x["url"] for x in g["sites"]] + [x["url"] for x in g["elig_sources"]]
        urls += [v["url"] for f in g["facts"] for v in f["values"]]
        urls += [t["law"][1] for t in g["terms"]["items"]]
        self.assertTrue(all(t["cat"] in g["terms"]["categories"] for t in g["terms"]["items"]))
        self.assertTrue(urls)
        self.assertEqual([u for u in urls if not sources.is_official(u)], [])


class BandsTest(unittest.TestCase):
    def test_bands_pyeong(self):
        r = applyhome.summarize_models([
            {"HOUSE_TY": "059.9800A", "SUPLY_AR": "84.5", "LTTOT_TOP_AMOUNT": "90000", "SUPLY_HSHLDCO": "10"},
            {"HOUSE_TY": "059.9500B", "SUPLY_AR": "83.9", "LTTOT_TOP_AMOUNT": "88000", "SUPLY_HSHLDCO": "5"},
            {"HOUSE_TY": "084.9900A", "LTTOT_TOP_AMOUNT": "120000"}])
        b59, b84 = r["bands"]["59"], r["bands"]["84"]
        self.assertEqual((b59["min"], b59["max"], b59["pyeong"], b59["units"]), (88000, 90000, 25, 15))
        self.assertFalse(b59["supply_estimated"])
        self.assertTrue(b84["supply_estimated"])        # 공급면적 없음 → 전용×1.33 추정
        self.assertEqual(b84["pyeong"], 34)
        self.assertAlmostEqual(b59["pyeong_price"], 3479, delta=30)


class GeoSpreadTest(unittest.TestCase):
    def test_spread_stable_and_close(self):
        from tracker import geo
        a = geo._spread(37.5, 127.0, "seoul:1")
        self.assertEqual(a, geo._spread(37.5, 127.0, "seoul:1"))
        self.assertNotEqual(a, geo._spread(37.5, 127.0, "seoul:2"))
        self.assertLess(geo._km(a[0], a[1], 37.5, 127.0), 0.3)
        self.assertEqual(geo._name_key("길훈 아파트(재건축)"), "길훈")


class RestrictInvestTest(unittest.TestCase):
    GK = {"name": "광교 자연앤 센트레빌(A17BL) 공공분양주택", "region": "경기", "sigungu": "수원시 영통구", "kind": "APT · 국민",
          "source": "apt", "move_in": "202910",
          "flags": {"overheat": "Y", "adjust": "Y", "price_cap": "Y", "redevelop": "N", "public_zone": "N", "large_site": "Y"}}

    def test_gwanggyo_matches_notice(self):
        # 공고문: 전매제한 3년, 거주의무 5년, 재당첨 10년
        r = rules.restrictions(dict(self.GK, market={"price_rep": 94890, "expected": 93700}))
        self.assertEqual((r["resale_years"], r["reside_min"], r["reside_max"], r["rewin_years"]), (3.0, 3.0, 5.0, 10))  # 법령 추정은 범위
        self.assertFalse(r["confirmed"])
        r = rules.restrictions(dict(self.GK, house_manage_no="2026000528"))       # data/notice_rules.csv 공고문 값
        self.assertEqual((r["resale_years"], r["reside_min"], r["reside_max"], r["rewin_years"], r["confirmed"]), (3.0, 5.0, 5.0, 10, True))

    def test_private_nonregulated(self):
        r = rules.restrictions({"name": "Y", "region": "경기", "sigungu": "평택시", "kind": "APT · 민영", "source": "apt",
                                "flags": {"overheat": "N", "adjust": "N", "price_cap": "N", "public_zone": "N", "large_site": "N"}})
        self.assertEqual((r["resale_years"], r["reside_max"], r["rewin_years"]), (0.5, 0.0, 0))

    def test_distance_comps_and_invest(self):
        class FakeGeo:
            POS = {"N": (37.2900, 127.0500), "가까운A": (37.2930, 127.0500), "가까운B": (37.2900, 127.0540),
                   "가까운C": (37.2870, 127.0480), "먼D": (37.3300, 127.0500)}
            def locate(self, key, address, region, sigungu, name=None, stage="all"):
                k = "N" if not key.startswith("cx:") else name
                lat, lng = self.POS[k]
                return {"lat": lat, "lng": lng, "precision": "exact"}
        n = dict(self.GK, id="apt:1", address="경기도 수원시 영통구 원천동 633", dong="원천동",
                 bands={"84": {"median": 94890, "area_min": 84.6, "area_max": 84.8}}, price_min=93860, price_max=94930)
        mk = lambda apt, by, price, k: [{"apt": apt, "dong": "원천동", "jibun": str(k), "build_year": by, "area": 84.7,
                                         "price": price, "date": "2099-10-01", "deal": "중개거래"}] * 4
        trades = (mk("가까운A", 2019, 165000, 1) + mk("가까운B", 2018, 150000, 2) + mk("가까운C", 2023, 135000, 3)
                  + mk("먼D", 2020, 300000, 4)                      # 4.4km — 제외
                  + [{"apt": "가까운A", "dong": "원천동", "jibun": "1", "build_year": 2019, "area": 84.7, "price": 50000,
                      "date": "2099-10-01", "deal": "직거래"}])     # 직거래 — 제외
        m = market.analyze(n, trades, [], [], {}, geo=FakeGeo())
        self.assertEqual(m["method"], "distance")
        self.assertEqual(m["expected"], 150000)                    # 1km 안 3개 단지 값(16.5억·15억·13.5억)의 중앙값
        self.assertEqual(m["confidence"], "보통")
        self.assertEqual(m["nearby"]["radius_km"], 1.0)
        self.assertNotIn("먼D", [c["apt"] for c in m["nearby"]["complexes"]])
        n["market"] = m
        n["restrict"] = rules.restrictions(n)
        iv = market.invest(n)
        self.assertAlmostEqual(iv["lock_years"], round(max(3.0, iv["years_to_move_in"] + 5.0), 1))   # 입주까지 + 실거주 5년
        self.assertAlmostEqual(iv["score"], round(iv["margin_pct"] / iv["lock_years"], 1))
        # 손실이면 기간으로 나누지 않음
        neg = market.invest(dict(n, market=dict(m, margin_pct=-20.0)))
        self.assertEqual(neg["score"], -20.0)
        # 실거주의무가 없으면 양도세 기준 최소 2년 보유
        nr = market.invest(dict(n, restrict=dict(n["restrict"], reside_max=0.0, resale_years=0.5)))
        self.assertAlmostEqual(nr["lock_years"], round(nr["years_to_move_in"] + 2.0, 1))
        # 단지 간 값 차이가 크면(퍼짐 > 50%) 신뢰도 낮음 → 순위 제외
        wide = mk("가까운A", 2019, 200000, 1) + mk("가까운B", 2018, 150000, 2) + mk("가까운C", 2023, 90000, 3)
        mw = market.analyze(n, wide, [], [], {}, geo=FakeGeo())
        self.assertEqual(mw["confidence"], "낮음")
        self.assertIsNone(market.invest(dict(n, market=mw)))
        # 거래 1건짜리 단지만 있으면 근거 부족
        one = [dict(t) for t in (mk("가까운A", 2019, 165000, 1)[:1] + mk("가까운B", 2018, 150000, 2)[:1] + mk("가까운C", 2023, 135000, 3)[:1])]
        self.assertNotEqual(market.analyze(n, one, [], [], {}, geo=FakeGeo())["method"], "distance")
        # 토지임대부는 순위 제외
        self.assertIsNone(market.invest(dict(n, name="고덕강일 토지임대부", market=m)))
        # 거리 근거가 없으면 순위 제외
        m2 = market.analyze(n, trades, [], [], {})
        self.assertNotEqual(m2["method"], "distance")
        self.assertIsNone(market.invest(dict(n, market=m2)))


class NotifyTest(unittest.TestCase):
    def test_new_and_d1(self):
        import os
        from unittest import mock
        from tracker import notify
        from tracker.util import today_kst
        tmr = (today_kst() + dt.timedelta(days=1)).isoformat()
        s = {"id": "remndr:1:1", "source": "remndr", "source_label": "무순위·잔여", "name": "테스트 줍줍", "region": "서울",
             "sigungu": "강남구", "units": 3, "price_min": 150000, "price_max": 180000, "url": "https://www.applyhome.co.kr/x",
             "events": [{"label": "청약접수", "start": tmr, "end": tmr}]}
        sent_texts = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(notify, "STATE", Path(d) / "n.json"), \
                mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}), \
                mock.patch.object(notify.requests, "post") as post:
            post.return_value.status_code = 200
            post.side_effect = lambda url, json=None, **k: (sent_texts.append(json.get("text") or json.get("body")), post.return_value)[1]
            self.assertEqual(notify.run([s], {"notify": {"github_issue": False}}, ["서울"]), [])   # 첫 실행: 기록만
            s2 = dict(s, id="remndr:2:2", name="새 줍줍")
            out = notify.run([s, s2], {"notify": {"github_issue": False}}, ["서울"])
            self.assertEqual(out, ["new:새 줍줍", "d1:새 줍줍"])       # 새 공고 + 내일 접수 알림
            self.assertIn("new:새 줍줍", out)
            self.assertNotIn("new:테스트 줍줍", out)                   # 이미 본 공고는 다시 안 보냄
            self.assertTrue(any("새 줍줍" in t and "applyhome" in t for t in sent_texts))
            self.assertEqual(notify.run([s, s2], {"notify": {"github_issue": False}}, ["서울"]), [])   # 중복 없음


class HistoryMatchTest(unittest.TestCase):
    def test_sold_by_address(self):
        from tracker import upcoming
        hist = {"a": {"name": "아크로 드 서초", "sigungu": "서초구", "address": "서울특별시 서초구 서초동 1333번지 일원",
                      "notice_date": "2026-03-27", "url": "u"}}
        self.assertEqual(upcoming.match_history({"name": "서초신동아", "sigungu": "서초구", "address": "서초구 서초동1333"}, hist)["name"],
                         "아크로 드 서초")                          # 조합명과 분양 브랜드가 달라도 지번으로 짝지음
        self.assertIsNone(upcoming.match_history({"name": "방배신동아", "sigungu": "서초구", "address": "서초구 방배동988-1"}, hist))
        p = {"id": "seoul:1301", "source": "seoul_xlsx", "name": "서초신동아", "region": "서울", "sigungu": "서초구",
             "address": "서초구 서초동1333", "stage": "착공", "milestones": {"d_start": today_iso()}}
        self.assertEqual(upcoming.candidates([p], [], set(), history=hist), [])
        self.assertEqual(p["sold"]["name"], "아크로 드 서초")


def today_iso():
    from tracker.util import today_kst
    return today_kst().isoformat()


class MarketMapTest(unittest.TestCase):
    def _t(self, seq, date, price, area=84.9, deal="중개거래"):
        return {"apt": seq, "dong": "반포동", "jibun": "1", "seq": seq, "deal": deal, "build_year": 2016,
                "area": area, "price": price, "date": date, "floor": 10}

    def test_clean_and_stats(self):
        from tracker import marketmap as mm
        today = dt.date(2026, 11, 10)
        ts = [self._t("A", f"2026-{m:02d}-15", 300000 + m * 1000) for m in range(1, 11)]
        ts += [self._t("A", "2026-05-01", 100000)]                     # 단지 중앙값의 1/3 → 이상치
        ts += [self._t("A", "2026-06-01", 200000, deal="직거래")]       # 직거래 제외
        clean, direct, outl = mm.clean_trades(ts)
        self.assertEqual((len(clean), direct, outl), (10, 1, 1))
        s = mm.band_stats(clean, today)
        self.assertEqual(s["last"][1], 310000)
        self.assertEqual(s["peak"][1], 309000)                           # 연속 3건 중앙값 최고(308·309·310 → 309)
        self.assertEqual(s["n12"], 10)
        self.assertEqual(s["ppa12"], round(st_median([t["price"] / t["area"] * 3.3058 for t in clean])))

    def test_same_complex_yoy_and_index(self):
        from tracker import marketmap as mm
        today = dt.date(2026, 11, 10)
        ts = []
        for k, base in (("A", 200000), ("B", 100000), ("C", 150000), ("D", 120000), ("E", 90000)):
            ts += [self._t(k, "2025-07-10", base), self._t(k, "2025-08-10", base)]            # 1년 전 6개월
            ts += [self._t(k, "2026-08-10", base * 1.1), self._t(k, "2026-09-10", base * 1.1)]  # 최근 6개월 +10%
        ts += [self._t("Z", "2026-09-20", 900000), self._t("Z", "2026-10-20", 900000)]           # 최근에만 거래된 비싼 단지
        yoy, n = mm.same_complex_yoy(ts, today)
        self.assertEqual((yoy, n), (10.0, 5))      # 비싼 단지 Z가 섞여도 같은 단지끼리만 비교 → +10%
        self.assertEqual(mm.band_of(59.9), "59")
        self.assertEqual(mm.band_of(84.99), "84")


def st_median(xs):
    import statistics
    return statistics.median(xs)


class MarketHistoryTest(unittest.TestCase):
    def _t(self, seq, date, price, area=84.9):
        return {"apt": seq, "dong": "반포동", "jibun": "1", "seq": seq, "deal": "중개거래", "build_year": 2010,
                "area": area, "price": price, "date": date, "floor": 10}

    def test_history_stats_cagr_and_drawdown(self):
        from tracker import marketmap as mm
        today = dt.date(2026, 11, 10)
        # 2010년 10억 → 해마다 +10%, 단 2022년은 고점(30억)·2023년은 급락(21억)
        price = {y: round(100000 * 1.1 ** (y - 2010)) for y in range(2010, 2027)}
        price[2022], price[2023] = 300000, 210000
        ts = [self._t("A", f"{y}-{m:02d}-15", price[y]) for y in range(2010, 2027) for m in (2, 5, 8, 10) for _ in (0, 1)]
        clean, _, outl = mm.clean_trades(ts)
        self.assertEqual(outl, 0)                                        # 긴 기간의 상승을 이상치로 지우지 않음
        h = mm.history_stats(clean, 2010, today)
        self.assertEqual(h["y0"], 2010)
        self.assertAlmostEqual(h["since"], (price[2026] / 100000 - 1) * 100, places=0)
        self.assertTrue(9.0 <= h["cagr"] <= 11.0, h["cagr"])
        self.assertEqual(h["mdd"][0], -30.0)                             # 30억 → 21억
        self.assertTrue(h["mdd"][1].startswith("2022") and h["mdd"][2].startswith("2023"))
        q = mm.band_history(clean)["84"]
        self.assertEqual(q[0], ["2010Q1", 100000, 2])

    def test_chain_index_same_complex(self):
        from tracker import marketmap as mm
        ts = []
        for i in range(6):                                               # 단지 6곳, 해마다 +10%
            for y in (2020, 2021, 2022):
                p = round((100000 + i * 50000) * 1.1 ** (y - 2020))
                ts += [self._t(f"C{i}", f"{y}-03-01", p), self._t(f"C{i}", f"{y}-09-01", p)]
        ts += [self._t("NEW", "2022-05-01", 900000), self._t("NEW", "2022-06-01", 900000)]   # 비싼 새 단지가 끼어도 지수는 그대로
        idx = mm.chain_index(ts)
        self.assertEqual([r[0] for r in idx], [2020, 2021, 2022])
        self.assertAlmostEqual(idx[1][1], 110.0, places=0)
        self.assertAlmostEqual(idx[2][1], 121.0, places=0)
        self.assertEqual(idx[2][2], 6)


class CatalystTest(unittest.TestCase):
    def test_only_official_rows_survive(self):
        import tempfile
        from pathlib import Path
        from tracker import catalysts
        rows = ("kind,line,name,lat,lng,stage,notice_no,notice_date,open_target,source_url,memo\n"
                "지하철역,가상선,공식역,37.5,127.0,기본계획,고시 제1호,2026-01-01,2030,https://www.molit.go.kr/x,\n"
                "지하철역,가상선,블로그역,37.5,127.0,착공,,,,https://blog.example.com/x,\n"
                "지하철역,가상선,좌표없음역,,,착공,,,,https://www.molit.go.kr/x,\n"
                "지하철역,가상선,단계오류역,37.5,127.0,추진중,,,,https://www.molit.go.kr/x,\n")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.csv"
            f.write_text(rows, encoding="utf-8")
            items, dropped = catalysts.load(f)
        self.assertEqual([x["name"] for x in items], ["공식역"])
        self.assertEqual(items[0]["stage_i"], 2)
        self.assertEqual(len(dropped), 3)


class ForecastTest(unittest.TestCase):
    def _trades(self, seed=1):
        """단지 12곳(수준이 서로 다름), 2006~2026 분기마다 거래. 참 지수: 반기마다 +2%, 2015년부터는 비싼 단지만 거래가 늘어남."""
        import random
        rng = random.Random(seed)
        ts = []
        for y in range(2006, 2027):
            for q in range(4):
                true = 0.01 * ((y - 2006) * 4 + q)
                for c in range(12):
                    n = 3 if (c < 6 or y < 2015) else 9                      # 구성 변화(비싼 단지 거래 비중 ↑)
                    for _ in range(n):
                        lv = math.log(1000 + c * 300)
                        price = math.exp(lv + true + rng.gauss(0, 0.02)) * 84.9
                        ts.append({"seq": f"C{c}", "dong": "가동", "jibun": str(100 + c), "area": 84.9, "price": round(price),
                                   "date": f"{y}-{q * 3 + 2:02d}-15", "deal": "중개거래", "build_year": 2000, "floor": 5})
        return ts

    def test_fe_index_ignores_composition(self):
        from tracker import forecast as fc, marketmap as mm
        idx = fc.fe_index(self._trades(), mm.ckey, mm.band_of)
        self.assertEqual(fc._qname(idx["q0"]), "2006Q1")
        self.assertAlmostEqual(idx["logidx"][-1], 0.01 * 83, delta=0.01)       # 참값 0.83 — 단순 중앙값이면 구성 변화로 더 크게 나옴
        h0, p = fc.halves(idx)
        self.assertEqual(len(p), 42)

    def test_fit_recovers_momentum_and_reversion(self):
        import random
        from tracker import forecast as fc
        rng = random.Random(7)
        series = {}
        for k in range(30):                                                    # ρ=0.5, κ=0.15, g=0.02 로 만든 가짜 지수
            p, d = [0.0, 0.02], 0.02
            for t in range(2, 40):
                d = 0.02 + 0.5 * (d - 0.02) + 0.15 * (0.02 * (t - 1) - p[-1]) + rng.gauss(0, 0.02)
                p.append(p[-1] + d)
            series[str(k)] = p
        m = fc.fit_pooled(series)
        self.assertAlmostEqual(m["rho"], 0.5, delta=0.12)
        self.assertAlmostEqual(m["kappa"], 0.15, delta=0.1)
        bt = fc.backtest(series, {c: 4012 for c in series})
        self.assertTrue(60 <= bt["y1"]["cover80"] <= 95, bt)
        self.assertLessEqual(bt["y1"]["err_model"], bt["y1"]["err_nochange"])

    def test_event_study_finds_jump(self):
        from tracker import forecast as fc, marketmap as mm
        from tracker.upcoming import _jibun_key
        ts = self._trades()
        for t in ts:                                                           # C3 단지만 2016-07 이후 +10%
            if t["seq"] == "C3" and t["date"] >= "2016-07-01":
                t["price"] = round(t["price"] * 1.10)
        idx = fc.fe_index(ts, mm.ckey, mm.band_of)
        zones = [{"name": "가동 재건축", "type": "재건축", "sigungu": "X구", "address": "X구 가동103",
                  "milestones": {"d_union": "2016-07-01"}}]
        rows = fc.event_rows(ts, idx, zones, _jibun_key)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(math.exp(rows[0]["jump"]) - 1, 0.10, delta=0.03)
        self.assertAlmostEqual(rows[0]["run_up"], 0.0, delta=0.03)

    def test_feed_classify(self):
        from tracker import catalysts as c
        pl = ["강남구", "분당구", "과천시"]
        a = c.classify("위례신사선 도시철도 기본계획 승인…강남구 구간 포함", pl)
        self.assertEqual((a["kind"], a["stage"], a["places"]), ("철도·지하철", "기본계획", ["강남구"]))
        self.assertEqual(c.classify("GTX-C 노선 착공식 개최", pl)["stage"], "착공")
        self.assertEqual(c.classify("세종-포천 고속도로 수도권 구간 개통", pl)["kind"], "도로")
        self.assertIsNone(c.classify("부산 도시철도 사상-하단선 개통", pl))          # 서울·경기 밖
        self.assertIsNone(c.classify("강남구 지하철역 환경 개선 캠페인", pl))          # 결정이 아닌 글
        self.assertIsNone(c.classify("올해 수도권 공공주택 6만호 이상 착공", pl))      # 교통이 아님
        self.assertIsNone(c.classify("○○ 의원, 9호선 연장 조기 착공 촉구", pl))       # 요구·희망
        self.assertEqual(c.classify("용문~홍천 광역철도 예비타당성조사 통과", pl), None)  # 홍천(강원)
