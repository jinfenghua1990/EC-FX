from __future__ import annotations

from io import BytesIO
from pathlib import Path
import json
import zipfile

from app.services.system_update import get_update_status, queue_action, stage_update_zip, _is_newer_version


def _valid_zip() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pkg/update-manifest.json", json.dumps({"app": "ecommerce-reconciliation", "version": "9.9.9", "database_revision": "0004_standardization_pipeline"}))
        zf.writestr("pkg/backend/app/main.py", "print('ok')")
        zf.writestr("pkg/scripts/update.sh", "#!/bin/sh\n")
        zf.writestr("pkg/migrations/env.py", "# x\n")
        zf.writestr("pkg/Dockerfile", "FROM scratch\n")
        zf.writestr("pkg/docker-compose.yml", "services: {}\n")
        zf.writestr("pkg/VERSION", "9.9.9\n")
    return buf.getvalue()


def test_stage_update_zip_and_queue(tmp_path: Path):
    package = stage_update_zip(_valid_zip(), "update.zip", data_root=tmp_path)
    assert package["package_version"] == "9.9.9"
    assert package["package_dir"].startswith("staging/")
    req = queue_action(
        "install_package",
        data_root=tmp_path,
        package_dir=package["package_dir"],
        package_version=package["package_version"],
    )
    assert req["ACTION"] == "install_package"
    status = get_update_status(current_version="0.6.0", data_root=tmp_path)
    assert status["state"] == "queued"
    assert status["package_version"] == "9.9.9"


def test_update_zip_rejects_runtime_data(tmp_path: Path):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("update-manifest.json", json.dumps({"app": "ecommerce-reconciliation", "version": "1.0.0", "database_revision": "0004_standardization_pipeline"}))
        zf.writestr("backend/x", "x")
        zf.writestr("scripts/x", "x")
        zf.writestr("migrations/env.py", "x")
        zf.writestr("Dockerfile", "x")
        zf.writestr("docker-compose.yml", "x")
        zf.writestr("VERSION", "1.0.0")
        zf.writestr(".env", "SECRET=x")
    try:
        stage_update_zip(buf.getvalue(), "bad.zip", data_root=tmp_path)
        assert False, "should reject .env"
    except ValueError as exc:
        assert "禁止覆盖" in str(exc)


def test_version_comparison_only_marks_newer_remote():
    assert _is_newer_version("0.8.2", "0.8.1") is True
    assert _is_newer_version("0.8.1", "0.8.1") is False
    assert _is_newer_version("0.7.9", "0.8.1") is False
    assert _is_newer_version("1.0", "0.9.9") is True
