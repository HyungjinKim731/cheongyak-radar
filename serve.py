"""내 PC에서 청약·정비 레이더를 띄우는 실행 파일.

    python serve.py                 # 데이터 갱신 후 http://<내PC IP>:8000 으로 서비스, 매일 06:10 자동 갱신
    python serve.py --port 8080     # 포트 변경
    python serve.py --no-update     # 갱신 없이 지금 데이터로만 띄우기
    python serve.py --demo          # 인증키 없이 샘플로 확인
    python serve.py --tunnel        # 집 밖(모바일 데이터)에서도 열리는 https 주소 만들기 (cloudflared 필요)
    python serve.py --every 1       # 1시간마다 자동 갱신 (기본 3시간 + 매일 06:10, 웹을 열 때 1시간 넘었으면 자동 갱신)

내 정보(profile.local.json)는 같은 PC·같은 와이파이에서만 읽고 저장할 수 있습니다. 터널(외부) 접속에는 보이지 않습니다.

같은 와이파이의 휴대폰은 화면에 출력되는 http://192.168.x.x:8000 주소(또는 QR)로 접속합니다.
인증키는 환경변수 DATA_GO_KR_KEY 또는 이 폴더의 .env 파일(DATA_GO_KR_KEY=키값)에 둡니다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import http.server
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
KST = dt.timezone(dt.timedelta(hours=9))
_lock = threading.Lock()
_status = {"last_run": None, "ok": None, "running": False, "started": None}
_extra: list[str] = []
PROFILE = ROOT / "profile.local.json"
# 내부망 + Tailscale(100.64.0.0/10, fd7a:) — 이 주소에서 온 요청만 개인정보(내 정보·포트폴리오) 접근 허용
PRIVATE_NET = re.compile(r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|::1|fe80:|fc|fd)")
TAILNET = re.compile(r"^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.")


# ---------------------------------------------------------------- 설정
def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:  # 기본 경로의 IP (실제 패킷은 보내지 않음)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    ts = shutil.which("tailscale")
    if ts:
        try:
            out = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout
            ips += [x.strip() for x in out.splitlines() if x.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    priv = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)")
    uniq = list(dict.fromkeys(i for i in ips if not i.startswith("127.")))
    return sorted(uniq, key=lambda i: (not priv.match(i), bool(TAILNET.match(i)), i))


# ---------------------------------------------------------------- 데이터 갱신
def update(extra: list[str]) -> bool:
    if not _lock.acquire(blocking=False):
        print("[갱신] 이미 진행 중")
        return False
    try:
        _status.update(running=True, started=dt.datetime.now(KST).isoformat(timespec="seconds"))
        print(f"[갱신] 시작 {dt.datetime.now(KST):%Y-%m-%d %H:%M}")
        r = subprocess.run([sys.executable, "-m", "tracker.run", *extra], cwd=ROOT)
        _status.update(last_run=dt.datetime.now(KST).isoformat(timespec="minutes"), ok=r.returncode == 0, running=False)
        print("[갱신] 완료" if r.returncode == 0 else f"[갱신] 실패 (종료코드 {r.returncode})")
        return r.returncode == 0
    finally:
        _status["running"] = False
        _lock.release()


def data_generated_at() -> str | None:
    try:
        import json
        with (DOCS / "data" / "data.json").open(encoding="utf-8") as f:
            head = f.read(200)
        m = re.search(r'"generated_at":"([^"]+)"', head)
        return m.group(1) if m else json.loads((DOCS / "data" / "data.json").read_text("utf-8")).get("generated_at")
    except Exception:
        return None


def start_update(min_gap_min: int = 10) -> bool:
    """중복·과도한 갱신 방지: 진행 중이거나 최근 min_gap_min 분 안에 끝났으면 시작하지 않음."""
    if _status["running"]:
        return False
    g = data_generated_at()
    if g and min_gap_min:
        try:
            age = dt.datetime.now(KST) - dt.datetime.fromisoformat(g)
            if age < dt.timedelta(minutes=min_gap_min):
                return False
        except ValueError:
            pass
    threading.Thread(target=update, args=(_extra,), daemon=True).start()
    return True


def every(hours: float) -> None:
    while True:
        time.sleep(hours * 3600)
        start_update(min_gap_min=30)


def scheduler(at: str, extra: list[str]) -> None:
    hh, mm = (int(x) for x in at.split(":"))
    while True:
        now = dt.datetime.now(KST)
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        print(f"[예약] 다음 자동 갱신 {nxt:%m-%d %H:%M}")
        time.sleep((nxt - now).total_seconds())
        update(extra)


# ---------------------------------------------------------------- 웹 서버
class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".json": "application/json; charset=utf-8", ".webmanifest": "application/manifest+json",
                      ".js": "text/javascript; charset=utf-8", ".html": "text/html; charset=utf-8"}

    def end_headers(self):
        # data.json·index.html은 늘 최신으로, 아이콘 등은 캐시
        if self.path.split("?")[0].endswith((".json", ".html", "/")):
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    # ------------------------------------------------------------ 보조
    def _json(self, code: int, obj) -> None:
        import json
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_local(self) -> bool:
        """같은 PC·같은 와이파이만 True. cloudflared 터널(외부)로 들어온 요청은 False → 내 정보 비공개."""
        if any(self.headers.get(h) for h in ("Cf-Ray", "Cf-Connecting-Ip", "X-Forwarded-For", "Forwarded")):
            return False
        return bool(PRIVATE_NET.match(self.client_address[0]))

    def _body_json(self):
        import json
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 200000:
            raise ValueError("크기 오류")
        obj = json.loads(self.rfile.read(n).decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("JSON 객체가 아님")
        return obj

    def _portfolio_post(self, path: str):
        from tracker import portfolio as pf
        if not self._is_local():
            return self._json(403, {"error": "포트폴리오는 같은 PC·와이파이에서만 볼 수 있습니다"})
        try:
            if path == "/api/portfolio/refresh":
                return self._json(200, pf.bundle(max_age_s=0) or {"error": "portfolio.local.json 없음"})
            body = self._body_json()
            if path == "/api/portfolio/trade":
                return self._json(200, {"trade": pf.add_trade(body), **(pf.bundle(max_age_s=86400) or {})})
            if path == "/api/portfolio/trade/delete":
                return self._json(200, {"deleted": pf.delete_trade(str(body.get("id"))), **(pf.bundle(max_age_s=86400) or {})})
            if path == "/api/portfolio":
                doc = pf.load()
                if doc is None:
                    return self._json(404, {"error": "portfolio.local.json 없음"})
                # 화면에서 고칠 수 있는 값만 반영 (보유수량·평단·목표가·체크리스트·올해 기실현 손익)
                byk = {h["ticker"]: h for h in doc["holdings"]}
                for u in body.get("holdings", []):
                    h = byk.get(u.get("ticker"))
                    if not h:
                        continue
                    for k in ("shares", "avg", "target"):
                        if k in u:
                            v = u[k]
                            h[k] = None if v in (None, "") else float(v)
                if "checklist" in body:
                    done = {c.get("key"): bool(c.get("done")) for c in body["checklist"]}
                    for c in doc.get("checklist", []):
                        c["done"] = done.get(c.get("key"), c.get("done"))
                if "prior_realized_krw_2026" in body:
                    v = body["prior_realized_krw_2026"]
                    doc["prior_realized_krw_2026"] = None if v in (None, "") else float(v)
                pf.save(doc)
                return self._json(200, {"saved": True, **(pf.bundle(max_age_s=86400) or {})})
        except (ValueError, KeyError) as e:
            return self._json(400, {"error": str(e)})
        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/portfolio"):
            return self._portfolio_post(path)
        if path == "/api/refresh":
            force = "force=1" in self.path and self._is_local()
            started = start_update(min_gap_min=0 if force else 10)
            return self._json(200, {"started": started, "running": _status["running"], "generated_at": data_generated_at()})
        if path == "/api/profile":
            if not self._is_local():
                return self._json(403, {"error": "내 정보 저장은 같은 와이파이/이 PC에서만 가능합니다"})
            import json
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 20000:
                return self._json(400, {"error": "크기 오류"})
            try:
                obj = json.loads(self.rfile.read(n).decode("utf-8"))
                assert isinstance(obj, dict)
            except Exception:
                return self._json(400, {"error": "JSON 형식 오류"})
            PROFILE.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
            return self._json(200, {"saved": True})
        self.send_error(404)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/healthz", "/api/status"):
            return self._json(200, {"ok": True, "can_refresh": True, "running": _status["running"],
                                    "started": _status["started"], "generated_at": data_generated_at(),
                                    "last_run_ok": _status["ok"], "profile_editable": self._is_local(),
                                    "portfolio": self._is_local() and (ROOT / "portfolio.local.json").exists()})
        if path == "/api/portfolio":
            if not self._is_local():
                return self.send_error(404)
            from tracker import portfolio as pf
            b = pf.bundle(max_age_s=120)
            return self._json(200, b) if b else self._json(404, {"error": "portfolio.local.json 없음 — portfolio.example.json 을 복사해 만드세요"})
        if path == "/profile.json":
            # 내 정보는 같은 PC·와이파이에서만. 외부(터널)에는 없는 것처럼 404
            if not self._is_local() or not PROFILE.exists():
                return self.send_error(404)
            body = PROFILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 모바일 데이터 절약: html·json은 gzip으로 (data.json 460KB → 약 50KB)
        path = Path(self.translate_path(self.path))
        if path.is_dir():
            path = path / "index.html"
        if (path.suffix in (".json", ".html", ".webmanifest") and path.is_file()
                and "gzip" in self.headers.get("Accept-Encoding", "")):
            import gzip
            body = gzip.compress(path.read_bytes(), compresslevel=6)
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(str(path)))
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        if os.environ.get("SERVE_QUIET") != "1":
            sys.stderr.write(f"[접속] {self.address_string()} {fmt % args}\n")


def print_qr(url: str) -> None:
    try:
        import qrcode  # pip install qrcode (선택)
    except ImportError:
        print("  (pip install qrcode 를 설치하면 여기에 QR 코드가 나옵니다)")
        return
    q = qrcode.QRCode(border=1)
    q.add_data(url)
    q.make(fit=True)
    q.print_ascii(invert=True)


def start_tunnel(port: int) -> subprocess.Popen | None:
    exe = shutil.which("cloudflared")
    if not exe:
        print("\n[터널] cloudflared 가 없습니다. 설치 방법은 README '집 밖에서 접속'을 보세요.")
        return None
    p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

    def watch():
        for line in p.stdout:  # type: ignore[union-attr]
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                print(f"\n[터널] 외부 접속 주소 (모바일 데이터에서도 열림): {m.group(0)}")
                print_qr(m.group(0))
    threading.Thread(target=watch, daemon=True).start()
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="청약·정비 레이더 로컬 서버")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default="0.0.0.0", help="127.0.0.1 로 두면 이 PC에서만 접속")
    ap.add_argument("--no-update", action="store_true", help="시작할 때 데이터 갱신 안 함")
    ap.add_argument("--daily", default="06:10", help="매일 자동 갱신 시각(KST), 'off'면 끔")
    ap.add_argument("--every", type=float, default=3, help="N시간마다 자동 갱신(기본 3, 0이면 끔)")
    ap.add_argument("--demo", action="store_true", help="인증키 없이 샘플 데이터")
    ap.add_argument("--tunnel", action="store_true", help="cloudflared 로 외부 https 주소 생성")
    args = ap.parse_args()

    load_env()
    os.chdir(ROOT)
    extra = ["--demo"] if args.demo else []
    _extra[:] = extra
    if not os.environ.get("DATA_GO_KR_KEY") and not args.demo:
        print("[안내] DATA_GO_KR_KEY 가 없어 청약 일정은 기존 데이터를 씁니다. .env 파일에 키를 넣거나 --demo 로 실행하세요.")

    if not args.no_update or not (DOCS / "data" / "data.json").exists():
        # 서버를 먼저 띄우고 갱신은 뒤에서 (첫 갱신이 1~3분 걸릴 수 있음)
        threading.Thread(target=update, args=(extra,), daemon=True).start()
    if args.daily != "off":
        threading.Thread(target=scheduler, args=(args.daily, extra), daemon=True).start()
    if args.every and args.every > 0:
        threading.Thread(target=every, args=(args.every,), daemon=True).start()

    handler = functools.partial(Handler, directory=str(DOCS))
    try:
        httpd = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as e:
        print(f"[오류] {args.port}번 포트를 열 수 없습니다({e}). --port 8080 처럼 다른 포트로 실행하세요.")
        return 1
    httpd.daemon_threads = True

    print("\n" + "=" * 56)
    print("  청약·정비 레이더 실행 중 (끄려면 Ctrl+C)")
    print(f"  이 PC:            http://localhost:{args.port}")
    ips = lan_ips() if args.host in ("0.0.0.0", "") else []
    lan = [i for i in ips if not TAILNET.match(i)]
    tail = [i for i in ips if TAILNET.match(i)]
    for ip in lan:
        print(f"  휴대폰·다른 기기: http://{ip}:{args.port}   (같은 와이파이만)")
    for ip in tail:
        print(f"  Tailscale:        http://{ip}:{args.port}   (어디서나 · 내 기기만, 포트폴리오도 열림)")
    if not tail and not args.tunnel:
        print("  집 밖(LTE·다른 와이파이)에서 보려면: README '집 밖에서 접속' (Tailscale 또는 --tunnel)")
    print("=" * 56)
    if lan:
        print_qr(f"http://{lan[0]}:{args.port}")
    tunnel = start_tunnel(args.port) if args.tunnel else None
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        httpd.server_close()
        if tunnel:
            tunnel.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
