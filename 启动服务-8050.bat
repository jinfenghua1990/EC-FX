@echo off
chcp 936 >nul
echo === bat fired %date% %time% >> C:\Users\jinfe\Documents\默认项目\EC-HX\data\local\bat-trace.log
cd /d C:\Users\jinfe\Documents\默认项目\EC-HX\backend
echo cwd=%cd% >> C:\Users\jinfe\Documents\默认项目\EC-HX\data\local\bat-trace.log
set DATABASE_URL=sqlite:///C:/Users/jinfe/Documents/默认项目/EC-HX/data/local/reconciliation.db
start "ECFX-8050" /min "C:\Users\jinfe\Documents\默认项目\EC-HX\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8050
