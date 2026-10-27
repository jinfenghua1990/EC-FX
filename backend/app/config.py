from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：backend/app/config.py -> parents[0]=app, [1]=backend, [2]=项目根
_ROOT = Path(__file__).resolve().parents[2]


def _detect_version() -> str:
    """版本号单一来源：优先读 VERSION 文件，避免 VERSION / config / manifest 三处不同步。"""
    here = Path(__file__).resolve()
    for candidate in (Path("/app/VERSION"), here.parents[2] / "VERSION" if len(here.parents) > 2 else None):
        if candidate is None:
            continue
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return "0.0.0"


class Settings(BaseSettings):
    app_name: str = "红盒子供销平台"
    app_port: int = 8050
    app_version: str = _detect_version()
    database_url: str = "sqlite:///./reconciliation.db"
    wdt_sid: str = ""
    wdt_appkey: str = ""
    wdt_appsecret: str = ""
    wdt_base_url: str = "https://api.wangdian.cn/openapi2"
    wdt_timeout_seconds: int = 30
    # 分销订单推送旺店通用：店铺编号（sales_trade_push.php 的 shop_no）与仓库编号（可空=自动）
    wdt_shop_no: str = ""
    wdt_warehouse_no: str = ""
    recon_amount_tolerance: Decimal = Decimal("0.02")
    system_update_data_root: str = "/app/data/system-update"
    github_repo: str = ""
    github_branch: str = "main"
    github_token: str = ""
    github_api_url: str = "https://api.github.com"

    # 项目根 .env 优先于当前工作目录 .env。
    # 本地是 `cd backend && uvicorn ...` 启动的，只看 CWD 会读不到根目录 .env，
    # 导致「已填 WDT_APPSECRET 却提示未配置」这种静默失效。
    model_config = SettingsConfigDict(
        env_file=(".env", _ROOT / ".env"),
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
