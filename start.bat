@echo off
REM ──────────────────────────────────────────────────────────────
REM  start.bat — MeetBot'u tek tıkla kurar ve başlatır (Windows)
REM  İlk çalıştırmada .venv oluşturur; requirements.txt her
REM  değiştiğinde bağımlılıkları otomatik günceller.
REM  Ek argümanlar main.py'ye aktarılır:  start.bat --doctor
REM ──────────────────────────────────────────────────────────────
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [MeetBot] Sanal ortam olusturuluyor...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [MeetBot] Python 3.10+ bulunamadi. https://www.python.org/downloads/ adresinden kurun.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
)

REM Kurulu bagimliliklar requirements.txt ile ayni degilse (ilk kurulum veya guncelleme) yukle
fc /b "requirements.txt" ".venv\requirements.installed.txt" >nul 2>&1
if errorlevel 1 (
    echo [MeetBot] Bagimliliklar yukleniyor / guncelleniyor...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [MeetBot] Bagimliliklar yuklenemedi.
        pause
        exit /b 1
    )
    copy /y "requirements.txt" ".venv\requirements.installed.txt" >nul
)

if not exist ".env" if exist ".env.example" (
    copy ".env.example" ".env" >nul
    echo [MeetBot] .env dosyasi olusturuldu - ayarlari oradan degistirebilirsiniz.
)

".venv\Scripts\python.exe" main.py %*
pause
