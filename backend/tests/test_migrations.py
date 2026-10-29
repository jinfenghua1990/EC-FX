"""数据库初始化与幂等迁移回归测试：不连接真实业务数据库。"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def _migrate(path: Path):
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{path}"
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate.py")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_new_sqlite_database_bootstraps_and_migrates_twice(tmp_path):
    database = tmp_path / "new.db"
    for attempt in range(2):
        result = _migrate(database)
        assert result.returncode == 0, (
            f"migration attempt {attempt + 1} failed:\n{result.stdout}\n{result.stderr}"
        )
    with sqlite3.connect(database) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(fx_products)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    manifest = json.loads((ROOT / "update-manifest.json").read_text(encoding="utf-8"))
    assert revision == manifest["database_revision"]
    assert "tier1_price" in columns
    assert "fx_orders" in tables
    assert "fx_payment_vouchers" in tables


def test_unknown_unversioned_database_is_not_stamped(tmp_path):
    database = tmp_path / "unknown.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE user_data (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO user_data (value) VALUES ('must survive')")
    result = _migrate(database)
    assert result.returncode != 0
    assert "未知数据库" in result.stderr
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM user_data").fetchone()[0] == "must survive"
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='alembic_version'"
        ).fetchone() is None
