"""配置层回归测试：确保项目根 .env 在任何工作目录下都能被读到。

背景（真实踩坑）：本地用 `cd backend && uvicorn ...` 启动，
pydantic-settings 的 env_file=".env" 是相对当前工作目录解析的，
所以放在项目根的 .env 读不到 —— 表现为「明明填了 WDT_APPSECRET，
接口却一直提示未配置」，属于静默失效。这里锁死该行为。
"""
from pathlib import Path

from app.config import Settings, settings

# backend/tests/test_config_env.py -> parents[0]=tests, [1]=backend, [2]=项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_file_entries() -> list[Path]:
    env_file = Settings.model_config.get("env_file")
    entries = env_file if isinstance(env_file, (list, tuple)) else [env_file]
    return [Path(p) for p in entries if p is not None]


def test_env_file_includes_project_root_absolute_path():
    absolute = {p.resolve() for p in _env_file_entries() if p.is_absolute()}
    assert (PROJECT_ROOT / ".env").resolve() in absolute, (
        f"env_file 未包含项目根 .env 的绝对路径，从 backend/ 启动时会读不到配置：{_env_file_entries()}"
    )


def test_settings_instance_created():
    assert isinstance(settings, Settings)
