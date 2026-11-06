/* 청약 자격 판정 엔진 — 브라우저에서 바로 계산 (서버로 개인 정보를 보내지 않음)
 *
 * window.Elig.evaluate(profile, ctx, params) → [{key, group, label, status, reasons[], route}]
 *   status: ok(가능) · maybe(확인 필요) · later(나중에 가능) · no(불가)
 * window.Elig.forNotice(profile, notice, params, today) → {results, best, label}
 * window.Elig.score(profile, onDate) → 가점 상세
 *
 * 규칙 출처는 data.json guide.elig_sources, 기준값은 guide.elig (tracker/guide.py ELIG_PARAMS).
 * 최종 판단은 항상 입주자모집공고문.
 */
(function () {
  "use strict";
  const DAY = 864e5;
  const iso = (d) => new Date(d.getTime() + 9 * 3600e3).toISOString().slice(0, 10);
  const yearsBetween = (a, b) => (Date.parse(b) - Date.parse(a)) / (365.2425 * DAY);
  const n = (v) => (v === null || v === undefined || v === "" || Number.isNaN(+v) ? null : +v);
  const won = (man) => man == null ? "?" : (Math.abs(man) >= 10000 ? `${(man / 10000).toFixed(2).replace(/\.?0+$/, "")}억` : `${Math.round(man).toLocaleString()}만`);
  const STATUS_RANK = { ok: 0, maybe: 1, later: 2, no: 3 };

  function age(p, on) {
    if (!p.birth) return null;
    const b = new Date(p.birth + "T00:00:00"), d = new Date(on + "T00:00:00");
    let a = d.getFullYear() - b.getFullYear();
    if (d.getMonth() < b.getMonth() || (d.getMonth() === b.getMonth() && d.getDate() < b.getDate())) a--;
    return a;
  }
  const homeless = (p) => (n(p.homes_owned) || 0) === 0;           // 오피스텔은 청약에서 주택 아님(준주택)
  const married = (p, on) => p.marital === "married";
  const engaged = (p) => p.marital === "engaged";
  const marriedYears = (p, on) => (p.marriage_date ? yearsBetween(p.marriage_date, on) : null);
  const hasSpouseIncome = (p) => n(p.spouse_income) != null ? n(p.spouse_income) > 0 : null;

  // ---------------------------------------------------------------- 가점
  function acctPoints(y) {
    if (y == null) return null;
    if (y < 0.5) return 1;
    if (y < 1) return 2;
    return Math.min(17, Math.floor(y) + 2);
  }
  function score(p, on) {
    const out = { items: [], total: null, notes: [] };
    // 무주택 기간: 만 30세부터(그 전에 혼인했으면 혼인신고일부터)
    let hl = 0, hlYears = null;
    if (homeless(p) && p.birth) {
      const b = new Date(p.birth + "T00:00:00");
      const at30 = new Date(b.getFullYear() + 30, b.getMonth(), b.getDate());
      let start = iso(at30);
      if (married(p) && p.marriage_date && p.marriage_date < start) start = p.marriage_date;
      hlYears = Math.max(0, yearsBetween(start, on));
      hl = start > on ? 0 : Math.min(32, 2 * (Math.floor(hlYears) + 1));
      if (start > on) out.notes.push("만 30세 전 미혼이라 무주택 기간 점수 0점");
    } else if (!homeless(p)) out.notes.push("주택 보유 중이라 무주택 기간 0점");
    out.items.push({ label: "무주택 기간", points: hl, max: 32, detail: hlYears == null ? "-" : `${hlYears.toFixed(1)}년` });
    // 부양가족: 배우자 + 자녀 + 3년 이상 부양한 직계존속(부모)
    const dep = (married(p) ? 1 : 0) + (n(p.children) || 0) + ((n(p.parent65_years) || 0) >= 3 ? 2 : 0);
    const depPts = Math.min(35, 5 + 5 * dep);
    out.items.push({ label: "부양가족", points: depPts, max: 35, detail: `${dep}명` });
    const ap = acctPoints(n(p.acct_years));
    out.items.push({ label: "통장 가입기간", points: ap, max: 17, detail: p.acct_years == null ? "입력 필요" : `${p.acct_years}년` });
    let spousePts = 0;
    if (married(p) && n(p.spouse_acct_years) != null) {
      spousePts = Math.min(3, Math.floor((acctPoints(n(p.spouse_acct_years)) || 0) / 2));
      out.items.push({ label: "배우자 통장(50%, 최대 3)", points: spousePts, max: 3, detail: `${p.spouse_acct_years}년` });
    }
    out.total = ap == null ? null : hl + depPts + ap + spousePts;
    out.min = hl + depPts + (ap || 1) + spousePts;
    return out;
  }

  // ---------------------------------------------------------------- 소득·자산
  // 월소득(원)과 기준 비율을 비교. 1·2인 가구는 두 방식(3인 기준 / 2인 기준+가산)을 모두 계산해 엇갈리면 maybe.
  function incomeCheck(P, monthlyWon, size, pct) {
    if (monthlyWon == null) return { st: "maybe", why: "소득 입력 필요" };
    const base = P.income100;
    const sz = Math.max(1, size);
    const lenient = (sz <= 3 ? base["3"] : base["4"]) * pct / 100;
    const strict = sz <= 2 ? base["2"] * (pct / 100 + (P.small_household_plus[String(sz)] || 0)) : lenient;
    const lo = Math.min(lenient, strict), hi = Math.max(lenient, strict);
    const m = Math.round(monthlyWon / 10000);
    if (monthlyWon <= lo) return { st: "ok", why: `월 ${m.toLocaleString()}만원 ≤ ${pct}% 기준 ${Math.round(lo / 1e4).toLocaleString()}만원` };
    if (monthlyWon > hi) return { st: "no", why: `월 ${m.toLocaleString()}만원 > ${pct}% 기준 ${Math.round(hi / 1e4).toLocaleString()}만원` };
    return { st: "maybe", why: `월 ${m.toLocaleString()}만원이 ${pct}% 기준(${Math.round(lo / 1e4).toLocaleString()}~${Math.round(hi / 1e4).toLocaleString()}만원, 가구원 산정 방식에 따라) 사이` };
  }
  const monthly = (manPerYear) => (manPerYear == null ? null : manPerYear * 10000 / 12);
  function bonusCaveat(p, r) {
    if (r.st === "ok" && !p.income_includes_bonus) return { st: "maybe", why: r.why + " — 성과급 포함 전년도 총급여로 다시 확인" };
    return r;
  }
  function totalAssets(p, withSpouse) {
    const parts = [n(p.realestate) || 0, n(p.lease_deposit) || 0, n(p.financial_assets), n(p.car)];
    const unknown = parts.some((x) => x == null);
    let t = parts.reduce((a, b) => a + (b || 0), 0);
    if (withSpouse) t += n(p.spouse_realestate) || 0;
    const net = p.debts == null ? null : t - n(p.debts);
    return { total: t, net, unknown };
  }
  function pubAssetCheck(P, p, withSpouse) {
    const re = (n(p.realestate) || 0) + (withSpouse ? n(p.spouse_realestate) || 0 : 0);
    if (re > P.pub_realestate) return { st: "no", why: `부동산 ${won(re)} > 공공 기준 ${won(P.pub_realestate)}` };
    const a = totalAssets(p, withSpouse);
    if (n(p.car) != null && n(p.car) > P.pub_car) return { st: "no", why: `자동차 ${won(p.car)} > 기준 ${won(P.pub_car)}` };
    if (a.total <= P.pub_total_asset && !a.unknown) return { st: "ok", why: `총자산 ${won(a.total)} ≤ ${won(P.pub_total_asset)}` };
    if (a.total > P.pub_total_asset) {
      if (a.net != null && a.net <= P.pub_total_asset) return { st: "maybe", why: `총자산 ${won(a.total)}(보증금 포함)은 초과, 부채 뺀 순자산 ${won(a.net)}은 이하 — 공고의 총자산/순자산 기준 확인` };
      return { st: a.net == null ? "maybe" : "no", why: `총자산 ${won(a.total)}(전세보증금 포함) > ${won(P.pub_total_asset)}${a.net == null ? " — 부채(전세대출 등)를 입력하면 순자산으로 다시 계산" : ""}` };
    }
    return { st: "maybe", why: `부동산 ${won(re)}는 기준 이하, 금융자산·자동차 입력 필요` };
  }

  // ---------------------------------------------------------------- 1순위·예치금
  function depositNeed(P, p, band) {
    const sido = p.sido || "";
    const row = P.deposit[sido] || (P.metro_cities.includes(sido) ? P.deposit["광역시"] : P.deposit["기타"]);
    const idx = band <= 85 ? 0 : band <= 102 ? 1 : band <= 135 ? 2 : 3;
    return row[idx];
  }
  function rank1(P, p, ctx, publicHousing) {
    const reasons = [];
    let st = "ok";
    const regulated = ctx.reg === "투기과열" || ctx.reg === "조정";
    const y = n(p.acct_years), pay = n(p.acct_payments);
    const needY = regulated ? 2 : (ctx.capital === false ? 0.5 : 1), needPay = regulated ? 24 : (ctx.capital === false ? 6 : 12);
    if (y == null) { st = "maybe"; reasons.push("통장 가입기간 입력 필요"); }
    else if (y < needY) { st = "no"; reasons.push(`통장 ${y}년 < ${needY}년`); }
    else reasons.push(`통장 ${y}년 ✓`);
    if (publicHousing) {
      if (pay == null) { if (st === "ok") st = "maybe"; reasons.push(`납입 ${needPay}회 이상인지 확인`); }
      else if (pay < needPay) { st = "no"; reasons.push(`납입 ${pay}회 < ${needPay}회`); }
    } else {
      const band = Math.min(...(ctx.bands && ctx.bands.length ? ctx.bands : [84]));
      const need = depositNeed(P, p, band);
      const bal = n(p.acct_balance);
      if (bal == null) { if (st === "ok") st = "maybe"; reasons.push(`예치금 ${need}만원 이상인지 확인(내 주소지 ${p.sido || "?"} 기준)`); }
      else if (bal < need) { st = "no"; reasons.push(`예치금 ${bal}만원 < ${need}만원`); }
      else reasons.push(`예치금 ${bal}만원 ✓`);
    }
    if (regulated) {
      if (!p.household_head) { st = "no"; reasons.push("규제지역 1순위는 세대주만 (지금은 2순위)"); }
      else reasons.push("세대주 ✓");
      if (p.won_5y) { st = "no"; reasons.push("최근 5년 내 세대 당첨 이력"); }
      if ((n(p.homes_owned) || 0) >= 2 && !publicHousing) { st = "no"; reasons.push("2주택 이상 세대"); }
    }
    if (publicHousing && !homeless(p)) { st = "no"; reasons.push("국민주택은 세대 전원 무주택"); }
    return { st, reasons };
  }
  function areaPriority(p, ctx) {
    if (!ctx.region) return null;
    if (ctx.region === p.sido) {
      if (ctx.region === "서울" && (ctx.reg === "투기과열")) {
        if (!p.residence_since) return { st: "maybe", why: "서울 해당지역 우선은 보통 2년 이상 계속 거주 — 전입일 입력 필요" };
        const y = yearsBetween(p.residence_since, ctx.date);
        return y >= 2 ? { st: "ok", why: `서울 ${y.toFixed(1)}년 거주 → 해당지역 우선` } : { st: "maybe", why: `서울 거주 ${y.toFixed(1)}년 < 2년 → 기타지역 순서일 수 있음` };
      }
      return { st: "ok", why: `${ctx.region} 거주 → 해당(시·도) 지역` };
    }
    const capital = ["서울", "경기", "인천"];
    if (capital.includes(ctx.region) && capital.includes(p.sido)) return { st: "ok", why: `${p.sido} 거주 → ${ctx.region} 공고에서는 기타지역(해당지역 다음 순서)` };
    return { st: "no", why: `${ctx.region} 공고는 ${p.sido} 거주자 신청 불가(공급지역 밖)` };
  }

  // ---------------------------------------------------------------- 유형별 판정
  function mk(key, group, label, st, reasons, route) { return { key, group, label, status: st, reasons: reasons.filter(Boolean), route: route || "" }; }
  const worst = (...s) => s.reduce((a, b) => (STATUS_RANK[b] > STATUS_RANK[a] ? b : a), "ok");

  const RULES = {
    // ===== 국민주택 =====
    pub_general(P, p, ctx) {
      const r = rank1(P, p, ctx, true);
      const rs = [...r.reasons];
      let st = r.st;
      if (!homeless(p)) return mk("pub_general", "국민", "일반공급", "no", ["세대 전원 무주택이어야 함"]);
      rs.unshift("무주택 ✓ (오피스텔은 주택 수 제외)");
      const small = (ctx.bands || []).some((b) => b <= 60);
      if (small) {
        const inc = bonusCaveat(p, incomeCheck(P, monthly(n(p.income_self)), n(p.household_size) || 1, 100));
        const as = pubAssetCheck(P, p, false);
        rs.push(`60㎡ 이하: 소득 100% — ${inc.why}`, `자산 — ${as.why}`);
        if ((ctx.bands || []).every((b) => b <= 60)) st = worst(st, inc.st, as.st);
        else rs.push("60㎡ 초과 주택형은 소득·자산 기준 없음");
      }
      rs.push("당첨자는 납입 인정금액(회차당 최대 25만원) 많은 순");
      return mk("pub_general", "국민", "일반공급", st, rs, "저축총액 순");
    },
    pub_newlywed(P, p, ctx) {
      const label = ctx.newlywedTown ? "신혼희망타운" : "신혼부부 특공";
      let st, rs = [];
      if (married(p)) {
        const my = marriedYears(p, ctx.date);
        if (my != null && my > 7) return mk("pub_newlywed", "국민", label, "no", [`혼인 ${my.toFixed(1)}년 > 7년`]);
        st = "ok"; rs.push("혼인 7년 이내 ✓");
      } else if (engaged(p)) { st = "ok"; rs.push("예비신혼부부로 신청 가능 (입주 전 혼인관계 증명)"); }
      else return mk("pub_newlywed", "국민", label, "no", ["혼인 7년 이내 또는 예비신혼부부(결혼 예정)만 대상"]);
      if (!homeless(p) || p.spouse_homeless === false) return mk("pub_newlywed", "국민", label, "no", ["두 사람 모두 무주택이어야 함"]);
      rs.push("두 사람 무주택 ✓");
      const si = n(p.spouse_income);
      const dual = hasSpouseIncome(p);
      const size = 2 + (n(p.children) || 0);
      let inc;
      if (si == null) {
        const alone = incomeCheck(P, monthly(n(p.income_self)), size, 130);
        inc = alone.st === "no"
          ? incomeCheck(P, monthly(n(p.income_self)), size, 200) // 맞벌이라면 200%로 완화되므로 본인 소득만으로는 판단 불가
          : alone;
        inc = { st: inc.st === "no" ? "no" : "maybe", why: `배우자(예정) 소득 입력 필요 — 본인 소득만으로 ${alone.why}` };
      } else {
        const pct = dual ? 200 : 130;
        inc = incomeCheck(P, monthly((n(p.income_self) || 0) + si), size, pct);
        inc.why = `${dual ? "맞벌이 200%" : "외벌이 130%"}: ${inc.why}`;
      }
      inc = bonusCaveat(p, inc);
      const as = pubAssetCheck(P, p, true);
      rs.push(`소득(합산) — ${inc.why}`, `자산 — ${as.why}`);
      return mk("pub_newlywed", "국민", label, worst(st, inc.st, as.st), rs);
    },
    pub_firsttime(P, p, ctx) {
      const L = "생애최초 특공";
      if (!married(p) && !(n(p.children) > 0)) return mk("pub_firsttime", "국민", L, engaged(p) ? "later" : "no", ["공공 생애최초는 혼인 중이거나 자녀가 있어야 함(1인 가구 불가)", engaged(p) ? "혼인신고 후 가능" : ""]);
      if (p.ever_owned_home || p.spouse_ever_owned) return mk("pub_firsttime", "국민", L, "no", ["세대원 누구라도 주택 소유 이력 있으면 불가"]);
      const rs = ["주택 소유 이력 없음 ✓"];
      let st = "ok";
      if (n(p.tax_years) == null) { st = "maybe"; rs.push("소득세 5년 이상 납부 확인"); } else if (n(p.tax_years) < 5) { st = "no"; rs.push("소득세 납부 5년 미만"); } else rs.push("소득세 5년 이상 ✓");
      if (n(p.acct_balance) == null) { st = worst(st, "maybe"); rs.push(`통장 저축액 ${P.pub_firsttime_saving}만원 이상 확인`); }
      else if (n(p.acct_balance) < P.pub_firsttime_saving) { st = "no"; rs.push(`저축액 ${p.acct_balance}만원 < ${P.pub_firsttime_saving}만원`); }
      const inc = bonusCaveat(p, incomeCheck(P, monthly((n(p.income_self) || 0) + (n(p.spouse_income) || 0)), 2 + (n(p.children) || 0), 130));
      const as = pubAssetCheck(P, p, true);
      rs.push(`소득 130% — ${inc.why}`, `자산 — ${as.why}`);
      return mk("pub_firsttime", "국민", L, worst(st, inc.st, as.st), rs);
    },
    pub_youth(P, p, ctx) {
      const L = "청년 특공";
      const a = age(p, ctx.date);
      if (married(p)) return mk("pub_youth", "국민", L, "no", ["미혼만 대상"]);
      if (a == null || a < 19 || a > 39) return mk("pub_youth", "국민", L, "no", [`만 19~39세만 (지금 만 ${a}세)`]);
      if (p.ever_owned_home) return mk("pub_youth", "국민", L, "no", ["주택 소유 이력 없어야 함"]);
      const rs = [`만 ${a}세 미혼 ✓`];
      const im = monthly(n(p.income_self));
      let st = "ok";
      if (im == null) { st = "maybe"; rs.push("소득 입력 필요"); }
      else if (im > P.youth_income_monthly) { st = "no"; rs.push(`본인 월 ${Math.round(im / 1e4)}만원 > 기준중위 140% ${Math.round(P.youth_income_monthly / 1e4)}만원`); }
      else rs.push("소득 ✓");
      const t = totalAssets(p, false);
      if (t.total > P.youth_asset_total) { st = "no"; rs.push(`본인 총자산 ${won(t.total)} > ${won(P.youth_asset_total)}`); }
      if (n(p.parent_assets) == null) { st = worst(st, "maybe"); rs.push(`부모 자산 ${won(P.youth_parent_asset)} 이하 확인`); }
      else if (n(p.parent_assets) > P.youth_parent_asset) { st = "no"; rs.push("부모 자산 초과"); }
      if (engaged(p)) rs.push("결혼(혼인신고)하면 대상에서 빠짐");
      return mk("pub_youth", "국민", L, st, rs);
    },
    pub_newborn(P, p) { return n(p.newborn) > 0 ? mk("pub_newborn", "국민", "신생아 특공", "maybe", ["2세 미만 자녀(임신 포함) ✓ — 소득·자산 공고 확인"]) : mk("pub_newborn", "국민", "신생아 특공", "later", ["2세 미만 자녀(임신 포함) 없음", "임신·출산 후 가능"]); },
    pub_multichild(P, p) { return (n(p.children) || 0) >= 2 ? mk("pub_multichild", "국민", "다자녀 특공", "maybe", ["미성년 자녀 2명 이상 ✓"]) : mk("pub_multichild", "국민", "다자녀 특공", "no", ["미성년 자녀 2명 이상(태아 포함)만 대상"]); },
    pub_parent(P, p) { return (n(p.parent65_years) || 0) >= 3 && p.household_head ? mk("pub_parent", "국민", "노부모 부양 특공", "maybe", ["65세 이상 부모 3년 이상 부양 세대주 ✓"]) : mk("pub_parent", "국민", "노부모 부양 특공", "no", ["만 65세 이상 부모를 3년 이상 같은 등본에서 부양한 세대주만"]); },

    // ===== 민영주택 =====
    prv_general(P, p, ctx) {
      const r = rank1(P, p, ctx, false);
      const rs = [...r.reasons];
      const ap = areaPriority(p, ctx);
      if (ap) rs.push(ap.why);
      let st = worst(r.st, ap ? (ap.st === "no" ? "no" : "ok") : "ok");
      if (r.st === "no" && !p.household_head) { st = "maybe"; rs.push("2순위로는 신청 가능(당첨 가능성 낮음)"); }
      const sc = score(p, ctx.date);
      const ratio = P.ratio[ctx.reg === "투기과열" ? "투기과열" : ctx.reg === "조정" ? "조정" : "비규제"];
      const bands = ctx.bands && ctx.bands.length ? ctx.bands : [59, 84];
      const ways = [...new Set(bands)].sort((a, b) => a - b).map((b) => {
        const g = b <= 60 ? ratio[0] : b <= 85 ? ratio[1] : ratio[2];
        return `${b}㎡ 가점 ${g}%·추첨 ${100 - g}%`;
      });
      rs.push(`내 가점 ${sc.total ?? sc.min + "+"}점 / 84점`, ways.join(" · "));
      if (homeless(p)) rs.push(`추첨 물량의 ${P.lottery_homeless_priority}%는 무주택자 우선 ✓`);
      const route = homeless(p) ? (sc.total != null && sc.total >= 60 ? "가점 + 추첨" : "추첨 위주 (가점 낮음)") : "추첨(유주택 후순위)";
      return mk("prv_general", "민영", "일반공급 1순위", st, rs, route);
    },
    prv_newlywed(P, p, ctx) {
      const L = "신혼부부 특공";
      if (!married(p)) return mk("prv_newlywed", "민영", L, engaged(p) ? "later" : "no", ["민영은 혼인신고 후 7년 이내만 (예비신혼부부 불가)", engaged(p) ? "혼인신고 후 가능" : ""]);
      const my = marriedYears(p, ctx.date);
      if (my != null && my > 7) return mk("prv_newlywed", "민영", L, "no", [`혼인 ${my.toFixed(1)}년 > 7년`]);
      if (!homeless(p) || p.spouse_homeless === false) return mk("prv_newlywed", "민영", L, "no", ["혼인신고일부터 계속 무주택이어야 함"]);
      const dual = hasSpouseIncome(p);
      const pct = dual ? 160 : 140;
      let inc = incomeCheck(P, monthly((n(p.income_self) || 0) + (n(p.spouse_income) || 0)), 2 + (n(p.children) || 0), pct);
      if (n(p.spouse_income) == null) inc = { st: "maybe", why: "배우자 소득 입력 필요" };
      inc = bonusCaveat(p, inc);
      const re = (n(p.realestate) || 0) + (n(p.spouse_realestate) || 0);
      const rs = ["혼인 7년 이내 · 무주택 ✓", `소득 ${pct}% — ${inc.why}`];
      if (inc.st === "ok") return mk("prv_newlywed", "민영", L, "ok", rs, "우선·일반 물량");
      if (re <= P.prv_lottery_realestate) { rs.push(`소득 초과여도 부동산 ${won(re)} ≤ ${won(P.prv_lottery_realestate)} → 추첨 30% 물량 가능`); return mk("prv_newlywed", "민영", L, "ok", rs, inc.st === "no" ? "추첨 30%" : "우선·일반 또는 추첨"); }
      rs.push(`부동산 ${won(re)} > ${won(P.prv_lottery_realestate)}`);
      return mk("prv_newlywed", "민영", L, inc.st, rs);
    },
    prv_firsttime(P, p, ctx) {
      const L = "생애최초 특공";
      if (p.ever_owned_home || (married(p) && p.spouse_ever_owned)) return mk("prv_firsttime", "민영", L, "no", ["세대원 누구라도 주택 소유 이력 있으면 불가"]);
      if (!homeless(p)) return mk("prv_firsttime", "민영", L, "no", ["무주택이어야 함"]);
      const rs = ["주택 소유 이력 없음 ✓ (오피스텔은 주택 아님)"];
      let st = "ok";
      const r1 = rank1(P, p, ctx, false);
      if (r1.st !== "ok") { st = worst(st, r1.st === "no" ? "no" : "maybe"); rs.push("일반공급 1순위여야 함: " + r1.reasons.filter((x) => !x.includes("✓")).join(", ")); }
      if (n(p.tax_years) == null) { st = worst(st, "maybe"); rs.push("소득세 5년 이상 납부 확인"); } else if (n(p.tax_years) < 5) { st = "no"; rs.push("소득세 납부 5년 미만"); }
      const single = !married(p) && !(n(p.children) > 0);
      const re = (n(p.realestate) || 0) + (married(p) ? n(p.spouse_realestate) || 0 : 0);
      const inc = incomeCheck(P, monthly((n(p.income_self) || 0) + (married(p) ? n(p.spouse_income) || 0 : 0)), single ? (n(p.household_size) || 1) : 2 + (n(p.children) || 0), 160);
      if (single) {
        rs.push("미혼·자녀 없음 → 1인 가구로 추첨 30% 물량만");
        const okAsset = re <= P.prv_lottery_realestate;
        rs.push(okAsset ? `부동산 ${won(re)} ≤ ${won(P.prv_lottery_realestate)} ✓ (소득 160% 이하 또는 이 기준 중 하나면 됨)` : `소득 160% — ${inc.why}`);
        if (!okAsset) st = worst(st, inc.st);
        if (!p.with_parents) {
          const bands = ctx.bands || [];
          if (bands.length && !bands.some((b) => b <= 60)) { st = "no"; rs.push("단독세대는 전용 60㎡ 이하만 — 이 단지엔 해당 주택형 없음"); }
          else rs.push("단독세대라 전용 60㎡ 이하만 신청");
        }
        if (engaged(p)) rs.push("혼인신고 후에는 우선·일반 물량과 전 면적으로 넓어짐");
        return mk("prv_firsttime", "민영", L, st, rs, "추첨 30% · 60㎡ 이하");
      }
      if (inc.st === "ok") { rs.push(`소득 160% 이하 — ${inc.why}`); return mk("prv_firsttime", "민영", L, worst(st, bonusCaveat(p, inc).st), rs, "우선·일반 물량"); }
      if (re <= P.prv_lottery_realestate) { rs.push(`소득 초과여도 부동산 ${won(re)} ≤ ${won(P.prv_lottery_realestate)} → 추첨 30%`); return mk("prv_firsttime", "민영", L, st, rs, "추첨 30%"); }
      return mk("prv_firsttime", "민영", L, "no", [...rs, inc.why, `부동산 ${won(re)} 초과`]);
    },
    prv_newborn(P, p) { const r = RULES.pub_newborn(P, p); return { ...r, key: "prv_newborn", group: "민영" }; },
    prv_multichild(P, p) { const r = RULES.pub_multichild(P, p); return { ...r, key: "prv_multichild", group: "민영" }; },
    prv_parent(P, p) { const r = RULES.pub_parent(P, p); return { ...r, key: "prv_parent", group: "민영" }; },

    // ===== 남은 물량 =====
    remndr(P, p, ctx) {
      if (!homeless(p)) return mk("remndr", "기타", "무순위(줍줍)", "no", ["2025년 개편 이후 무주택자만"]);
      const rs = ["무주택 ✓ · 통장 필요 없음"];
      let st = "ok";
      if (ctx.region && ctx.region !== p.sido) { st = "maybe"; rs.push(`거주 요건(전국/수도권/해당 시)은 공고마다 다름 — ${ctx.region} 공고`); }
      else rs.push("거주 요건은 공고 확인");
      if (ctx.reg === "투기과열") rs.push("규제지역 단지는 당첨 시 재당첨 제한(최장 10년)");
      return mk("remndr", "기타", "무순위(줍줍)", st, rs);
    },
    opt(P, p, ctx) { return mk("opt", "기타", "임의공급", "ok", ["사업자가 정한 조건(선착순·추첨) — 공고 확인"]); },
  };

  const PUBLIC_KEYS = ["pub_general", "pub_newlywed", "pub_firsttime", "pub_youth", "pub_newborn", "pub_multichild", "pub_parent"];
  const PRIVATE_KEYS = ["prv_general", "prv_newlywed", "prv_firsttime", "prv_newborn", "prv_multichild", "prv_parent"];

  function evaluate(p, ctx, P, keys) {
    return keys.map((k) => RULES[k](P, p, ctx));
  }

  // 공고 → 판정 문맥
  function noticeCtx(s, today) {
    const ev = (s.events || []).find((e) => e.end >= today) || (s.events || [])[0];
    const kind = s.kind || "";
    const name = s.name || "";
    const isPublic = kind.includes("국민") || /공공분양|토지임대부|신혼희망|행복주택/.test(name);
    return {
      date: s.notice_date || today,
      region: s.region, reg: s.regulation || "비규제",
      capital: ["서울", "경기", "인천"].includes(s.region),
      bands: Object.keys(s.bands || {}).map(Number).filter(Boolean),
      newlywedTown: /신혼희망/.test(name),
      isPublic, source: s.source, eventLabel: ev ? ev.label : "",
    };
  }
  function forNotice(p, s, P, today) {
    const ctx = noticeCtx(s, today);
    let keys;
    if (s.source === "remndr") keys = ["remndr"];
    else if (s.source === "opt") keys = ["opt"];
    else if (ctx.newlywedTown) keys = ["pub_newlywed"];
    else keys = ctx.isPublic ? PUBLIC_KEYS : PRIVATE_KEYS;
    const results = evaluate(p, ctx, P, keys);
    const ok = results.filter((r) => r.status === "ok");
    const maybe = results.filter((r) => r.status === "maybe");
    const best = ok.length ? "ok" : maybe.length ? "maybe" : results.some((r) => r.status === "later") ? "later" : "no";
    const label = ok.length ? ok.map((r) => r.label + (r.route ? `(${r.route})` : "")).join(" · ")
      : maybe.length ? "확인 필요: " + maybe.map((r) => r.label).join(" · ") : "지금은 넣을 수 있는 유형 없음";
    return { results, best, label, ctx };
  }

  // 전체 요약: 대표 상황 4가지
  function summary(p, P, today) {
    const ctxs = {
      seoul_prv: { date: today, region: "서울", reg: "투기과열", capital: true, bands: [59, 84] },
      gg_prv: { date: today, region: "경기", reg: "비규제", capital: true, bands: [59, 84] },
      seoul_pub: { date: today, region: "서울", reg: "투기과열", capital: true, bands: [59, 84] },
    };
    return {
      public: evaluate(p, ctxs.seoul_pub, P, PUBLIC_KEYS),
      private_seoul: evaluate(p, ctxs.seoul_prv, P, PRIVATE_KEYS),
      private_gg: evaluate(p, ctxs.gg_prv, P, ["prv_general"]),
      etc: [RULES.remndr(P, p, ctxs.seoul_prv), RULES.opt(P, p, ctxs.seoul_prv)],
    };
  }
  function missing(p) {
    const need = [];
    const add = (cond, label, why) => cond && need.push({ label, why });
    add(p.acct_balance == null, "청약통장 잔액(예치금)", "민영 1순위 예치금(서울 300만원~), 공공 생애최초 저축액 600만원");
    add(p.acct_payments == null, "청약통장 납입 회차", "국민주택 1순위(규제 24회)와 저축총액 순위");
    add(!p.income_includes_bonus, "전년도 총급여(성과급 포함)", "소득 기준은 원천징수영수증 총급여로 판단 — 성과급이 크면 결과가 바뀜");
    add(p.marital !== "single" && p.spouse_income == null, "배우자(예정) 연소득", "신혼 특공은 부부 합산 소득, 맞벌이 여부로 기준(130%/200%, 140%/160%) 달라짐");
    add(!p.residence_since, "현 거주지(서울) 전입일", "서울 투기과열 단지 해당지역 우선은 2년 이상 거주");
    add(p.debts == null, "부채(전세대출 등)", "공공분양 자산 기준이 순자산이면 결과가 바뀜");
    add(p.financial_assets == null, "금융자산(예금·주식 등)", "공공분양·청년특공 총자산 기준");
    add(p.car == null, "자동차 가액", "공공분양 자동차 기준 약 3,700만원");
    add(p.marital !== "single" && p.spouse_acct_years == null, "배우자 통장 가입기간", "혼인 후 가점 최대 +3점");
    return need;
  }

  window.Elig = { evaluate, forNotice, summary, score, missing, age, RULES, PUBLIC_KEYS, PRIVATE_KEYS, STATUS_RANK };
})();
