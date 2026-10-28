#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402

BASELINE = "0001_baseline"
# 用于识别「V0.4 及更早、还没有 alembic_version 的老库」的特征表。
# 其中 import_batches / legacy_shipment_rows 已在 0009 删除，但老库里必然存在过它们，
# 所以这里保留原名作为历史特征，不要跟着 models.py 一起改。
CORE_TABLES = {"import_batches", "legacy_shipment_rows", "stockout_orders"}


def wait_for_database(url: str, attempts: int = 45, sleep_seconds: int = 2) -> None:
    engine = create_engine(url, future=True, pool_pre_ping=True)
    last = None
    for _ in range(attempts):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return
        except Exception as exc:  # pragma: no cover - network timing
            last = exc
            time.sleep(sleep_seconds)
    raise RuntimeError(f"数据库在 {attempts * sleep_seconds}s 内未就绪: {last}")


def expected_revision() -> str:
    manifest = ROOT / "update-manifest.json"
    if not manifest.exists():
        return ""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"无法读取 update-manifest.json: {exc}") from exc
    return str(data.get("database_revision") or "").strip()


def current_revision(url: str) -> str:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            return str(ctx.get_current_revision() or "")
    finally:
        engine.dispose()


def main() -> int:
    url = os.getenv("DATABASE_URL") or settings.database_url
    wait_for_database(url)

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    engine.dispose()

    script = ScriptDirectory.from_config(cfg)
    package_head = str(script.get_current_head() or "")
    expected = expected_revision()
    if expected and package_head != expected:
        raise RuntimeError(f"安装包迁移链不完整：manifest 要求 {expected}，实际 Alembic head 为 {package_head}")

    if not tables:
        # 0001_baseline 动态引用最新模型。对空库先创建最新 schema，
        # 再逐条重放历史 ADD COLUMN / CREATE TABLE 会重复建列或建表。
        # 仅限真正的空库直接初始化并登记当前版本；已有数据仍逐版迁移。
        from app.db import Base
        import app.models  # noqa: F401

        print("[migrate] 检测到全新空库，按当前模型建表并登记最新迁移版本")
        engine = create_engine(url, future=True)
        try:
            with engine.begin() as conn:
                Base.metadata.create_all(bind=conn)
        finally:
            engine.dispose()
        command.stamp(cfg, package_head)
    elif "alembic_version" not in tables:
        # V0.4 及更早版本没有 alembic_version，但保留了特征业务表。
        if tables.intersection(CORE_TABLES):
            print("[migrate] 检测到旧版数据库，登记 V0.5 baseline，不重建历史表")
            command.stamp(cfg, BASELINE)
        else:
            # 不能把未知的非空库误当新库 stamp head：那样会跳过必要的数据迁移。
            raise RuntimeError(
                "检测到有表但没有 Alembic 版本的未知数据库；已停止自动迁移，"
                "请先备份并核对原有数据库版本"
            )

    print("[migrate] alembic upgrade head")
    command.upgrade(cfg, "head")

    actual = current_revision(url)
    if actual != package_head:
        raise RuntimeError(f"数据库迁移校验失败：当前 {actual or 'none'}，预期 {package_head}")
    if expected and actual != expected:
        raise RuntimeError(f"数据库版本不符合安装包要求：当前 {actual}，预期 {expected}")

    print(f"[migrate] schema verified: {actual}")
    print("[migrate] 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
