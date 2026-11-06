@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 이 설치되어 있지 않습니다. https://www.python.org 에서 설치하세요 ^(Add to PATH 체크^). & pause & exit /b 1)
if not exist .venv (
  echo [준비] 가상환경 만드는 중...
  python -m venv .venv || (pause & exit /b 1)
  .venv\Scripts\python -m pip install -q -r requirements.txt qrcode
)
.venv\Scripts\python serve.py %*
pause
