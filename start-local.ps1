# 红盒子供销平台 - 本地启动脚本（端口 8050，完全离线运行）
# 用法：在项目根目录右键"使用 PowerShell 运行"，或执行 .\start-local.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 数据库：本地 SQLite 文件（本项目唯一数据库）
$env:DATABASE_URL = "sqlite:///$root/data/local/reconciliation.db"
# 端口：8050（唯一运行端口；与 NAS/8010 无关）
# 绑定 0.0.0.0 = 局域网模式：本机 http://127.0.0.1:8050，同网段 http://<本机IP>:8050
$env:APP_PORT = "8050"

# 1. 数据库迁移
& "$root\.venv\Scripts\python.exe" "$root\scripts\migrate.py"
if ($LASTEXITCODE -ne 0) { throw "数据库迁移失败" }

# 2. 启动服务
Set-Location "$root\backend"
& "$root\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8050
