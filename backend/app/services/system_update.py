from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import secrets
import shutil
import tempfile
import zipfile
import base64
import re
import httpx


@dataclass
class UpdatePaths:
    root: Path

    @property
    def packages(self) -> Path:
        return self.root / "packages"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def request(self) -> Path:
        return self.root / "request.env"

    @property
    def status(self) -> Path:
        return self.root / "status.env"

    @property
    def log(self) -> Path:
        return self.root / "update.log"

    @property
    def auto_config(self) -> Path:
        return self.root / "auto.env"


SAFE_ENV_KEYS = {
    "REQUEST_ID",
    "ACTION",
    "STATE",
    "MESSAGE",
    "UPDATED_AT",
    "REQUESTED_AT",
    "PACKAGE_DIR",
    "PACKAGE_VERSION",
    "CURRENT_VERSION",
    "CURRENT_COMMIT",
    "REMOTE_COMMIT",
    "REMOTE_VERSION",
    "GITHUB_REPO",
    "HAS_UPDATE",
    "MODE",
    "AUTO_UPDATE",
    "INTERVAL_MINUTES",
    "LAST_CHECK_AT",
    "LAST_RESULT",
    "LAST_CHECK_VERSION",
}


def update_paths(data_root: str | Path = "/app/data/system-update") -> UpdatePaths:
    paths = UpdatePaths(Path(data_root))
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.packages.mkdir(parents=True, exist_ok=True)
    paths.staging.mkdir(parents=True, exist_ok=True)
    return paths


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_value(value: object) -> str:
    text = str(value if value is not None else "")
    return text.replace("\r", " ").replace("\n", " ")[:2000]


def write_env_atomic(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for key, value in data.items():
                if key not in SAFE_ENV_KEYS:
                    continue
                f.write(f"{key}={_safe_value(value)}\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def read_log(path: Path, max_chars: int = 12000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def queue_action(action: str, *, data_root: str | Path = "/app/data/system-update", package_dir: str = "", package_version: str = "") -> dict[str, str]:
    if action not in {"check", "update", "rollback", "install_package"}:
        raise ValueError("不支持的更新动作")
    paths = update_paths(data_root)
    current = read_env_file(paths.request)
    status = read_env_file(paths.status)
    if current and status.get("STATE") in {"queued", "running"} and current.get("REQUEST_ID") == status.get("REQUEST_ID"):
        raise RuntimeError("已有系统更新任务正在执行")
    request_id = secrets.token_hex(8)
    payload = {
        "REQUEST_ID": request_id,
        "ACTION": action,
        "REQUESTED_AT": _now(),
        "PACKAGE_DIR": package_dir,
        "PACKAGE_VERSION": package_version,
    }
    write_env_atomic(paths.request, payload)
    write_env_atomic(
        paths.status,
        {
            **payload,
            "STATE": "queued",
            "MESSAGE": "任务已提交，等待群晖更新任务处理（通常不超过1分钟）",
            "UPDATED_AT": _now(),
        },
    )
    paths.log.write_text("", encoding="utf-8")
    return {k: str(v) for k, v in payload.items()}


def get_update_status(*, current_version: str, data_root: str | Path = "/app/data/system-update") -> dict[str, object]:
    paths = update_paths(data_root)
    status = read_env_file(paths.status)
    return {
        "current_version": current_version,
        "state": status.get("STATE", "idle"),
        "message": status.get("MESSAGE", "尚未执行更新任务"),
        "request_id": status.get("REQUEST_ID", ""),
        "action": status.get("ACTION", ""),
        "updated_at": status.get("UPDATED_AT", ""),
        "package_version": status.get("PACKAGE_VERSION", ""),
        "current_commit": status.get("CURRENT_COMMIT", ""),
        "remote_commit": status.get("REMOTE_COMMIT", ""),
        "remote_version": status.get("REMOTE_VERSION", ""),
        "github_repo": status.get("GITHUB_REPO", ""),
        "has_update": status.get("HAS_UPDATE") == "1",
        "mode": status.get("MODE", ""),
        "log": read_log(paths.log),
    }


DEFAULT_AUTO_INTERVAL_MINUTES = 30


def read_auto_config(*, data_root: str | Path = "/app/data/system-update") -> dict[str, object]:
    """读取自动升级配置（存在 data/system-update/auto.env，不依赖容器环境变量）。"""
    paths = update_paths(data_root)
    data = read_env_file(paths.auto_config)
    try:
        interval = int(data.get("INTERVAL_MINUTES") or DEFAULT_AUTO_INTERVAL_MINUTES)
    except ValueError:
        interval = DEFAULT_AUTO_INTERVAL_MINUTES
    interval = max(5, min(1440, interval))
    return {
        "enabled": data.get("AUTO_UPDATE") == "1",
        "interval_minutes": interval,
        "last_check_at": data.get("LAST_CHECK_AT", ""),
        "last_result": data.get("LAST_RESULT", ""),
        "last_check_version": data.get("LAST_CHECK_VERSION", ""),
    }


def write_auto_config(
    *,
    data_root: str | Path = "/app/data/system-update",
    enabled: bool | None = None,
    interval_minutes: int | None = None,
    **extra: object,
) -> dict[str, object]:
    """更新自动升级配置；未传的字段保持原值。"""
    paths = update_paths(data_root)
    current = read_env_file(paths.auto_config)
    merged: dict[str, str] = {
        "AUTO_UPDATE": current.get("AUTO_UPDATE", "0"),
        "INTERVAL_MINUTES": current.get("INTERVAL_MINUTES", str(DEFAULT_AUTO_INTERVAL_MINUTES)),
        "LAST_CHECK_AT": current.get("LAST_CHECK_AT", ""),
        "LAST_RESULT": current.get("LAST_RESULT", ""),
        "LAST_CHECK_VERSION": current.get("LAST_CHECK_VERSION", ""),
    }
    if enabled is not None:
        merged["AUTO_UPDATE"] = "1" if enabled else "0"
    if interval_minutes is not None:
        merged["INTERVAL_MINUTES"] = str(max(5, min(1440, int(interval_minutes))))
    for key, value in extra.items():
        if key in SAFE_ENV_KEYS:
            merged[key] = "" if value is None else str(value)
    write_env_atomic(paths.auto_config, merged)
    return read_auto_config(data_root=data_root)


def _safe_zip_member(name: str) -> bool:
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        return False
    return True


def stage_update_zip(content: bytes, filename: str, *, data_root: str | Path = "/app/data/system-update") -> dict[str, str]:
    if not filename.lower().endswith(".zip"):
        raise ValueError("更新包必须是 ZIP 文件")
    if len(content) > 100 * 1024 * 1024:
        raise ValueError("更新包不能超过 100MB")
    paths = update_paths(data_root)
    package_id = secrets.token_hex(8)
    zip_path = paths.packages / f"{package_id}.zip"
    zip_path.write_bytes(content)
    stage_tmp = paths.staging / f".{package_id}.tmp"
    stage_dir = paths.staging / package_id
    shutil.rmtree(stage_tmp, ignore_errors=True)
    shutil.rmtree(stage_dir, ignore_errors=True)
    stage_tmp.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            if not members:
                raise ValueError("更新包为空")
            for member in members:
                if not _safe_zip_member(member.filename):
                    raise ValueError(f"更新包包含非法路径：{member.filename}")
            zf.extractall(stage_tmp)

            # zipfile.extractall() 不会保留 zip 内的 Unix 权限位，
            # 会导致 scripts/*.sh 变成 644，宿主机更新脚本执行时报 Permission denied。
            # 这里按 zip 记录的 external_attr 恢复权限。
            for member in members:
                if member.is_dir():
                    continue
                target = stage_tmp / member.filename
                if not target.exists():
                    continue
                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    try:
                        target.chmod(mode)
                    except OSError:
                        pass

        # 允许 ZIP 最外层多一个目录。
        children = [p for p in stage_tmp.iterdir() if p.name != "__MACOSX"]
        package_root = stage_tmp
        if len(children) == 1 and children[0].is_dir():
            package_root = children[0]

        manifest_path = package_root / "update-manifest.json"
        if not manifest_path.exists():
            raise ValueError("不是有效的系统更新包：缺少 update-manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("app") != "ecommerce-reconciliation":
            raise ValueError("更新包应用标识不正确")
        version = str(manifest.get("version") or "").strip()
        if not version:
            raise ValueError("更新包未声明版本号")
        db_revision = str(manifest.get("database_revision") or "").strip()
        if not db_revision:
            raise ValueError("更新包未声明数据库版本 database_revision")
        required = ["backend", "scripts", "migrations", "Dockerfile", "docker-compose.yml", "VERSION"]
        missing = [name for name in required if not (package_root / name).exists()]
        if missing:
            raise ValueError("更新包缺少必要文件：" + ", ".join(missing))
        version_file = (package_root / "VERSION").read_text(encoding="utf-8", errors="replace").strip()
        if version_file != version:
            raise ValueError(f"更新包版本不一致：manifest={version}，VERSION={version_file}")
        forbidden = [".env", "data", "backups", ".git", "compose.yaml"]
        found_forbidden = [name for name in forbidden if (package_root / name).exists()]
        if found_forbidden:
            raise ValueError("更新包包含禁止覆盖的运行数据：" + ", ".join(found_forbidden))

        shutil.copytree(package_root, stage_dir)
        # 保险：无论 zip 是否记录权限，脚本必须可执行，否则宿主机更新流程会失败。
        for script in stage_dir.rglob("*.sh"):
            script.chmod(0o755)
        migrate_script = stage_dir / "scripts" / "migrate.py"
        if migrate_script.exists():
            migrate_script.chmod(0o755)
        return {
            "package_id": package_id,
            "package_dir": f"staging/{package_id}",
            "package_version": version,
            "filename": filename,
        }
    finally:
        shutil.rmtree(stage_tmp, ignore_errors=True)


def set_update_status(*, data_root: str | Path = "/app/data/system-update", **values: object) -> None:
    """Write a status snapshot that is safe for the web UI."""
    paths = update_paths(data_root)
    payload = {"UPDATED_AT": _now(), **values}
    write_env_atomic(paths.status, payload)


def _github_headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ecommerce-reconciliation-updater",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _validate_github_repo(repo: str) -> str:
    repo = (repo or "").strip().strip("/")
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("GITHUB_REPO 格式应为 owner/repository")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if any(any(ch not in allowed for ch in part) for part in parts):
        raise ValueError("GITHUB_REPO 包含不支持的字符")
    return repo


def _version_key(value: str) -> tuple[int, ...]:
    text = (value or "").strip().lstrip("vV")
    if not re.fullmatch(r"\d+(?:\.\d+)*", text):
        raise ValueError(f"版本号格式不支持：{value}")
    return tuple(int(x) for x in text.split("."))


def _is_newer_version(remote: str, current: str) -> bool:
    r = _version_key(remote)
    c = _version_key(current)
    width = max(len(r), len(c))
    return r + (0,) * (width - len(r)) > c + (0,) * (width - len(c))


def github_check(*, repo: str, branch: str, token: str, current_version: str, api_url: str = "https://api.github.com", timeout: float = 30.0) -> dict[str, object]:
    repo = _validate_github_repo(repo)
    branch = (branch or "main").strip() or "main"
    base = api_url.rstrip("/")
    headers = _github_headers(token)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            r = client.get(f"{base}/repos/{repo}/contents/VERSION", params={"ref": branch})
            if r.status_code == 401:
                raise RuntimeError("GitHub Token 无效或已过期")
            if r.status_code == 403:
                raise RuntimeError("GitHub Token 没有读取该仓库的权限")
            if r.status_code == 404:
                raise RuntimeError("找不到 GitHub 仓库或 VERSION 文件，请检查 GITHUB_REPO / GITHUB_BRANCH")
            r.raise_for_status()
            meta = r.json()
            encoded = meta.get("content") or ""
            try:
                remote_version = base64.b64decode(encoded).decode("utf-8").strip()
            except Exception as exc:
                raise RuntimeError("无法读取 GitHub VERSION 文件") from exc
            c = client.get(f"{base}/repos/{repo}/commits/{branch}")
            remote_commit = ""
            if c.is_success:
                remote_commit = str((c.json() or {}).get("sha") or "")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"GitHub 检查失败：{exc}") from exc
    return {
        "repo": repo,
        "branch": branch,
        "remote_version": remote_version,
        "remote_commit": remote_commit,
        "has_update": _is_newer_version(remote_version, current_version),
    }


def github_download_update(*, repo: str, branch: str, token: str, api_url: str = "https://api.github.com", data_root: str | Path = "/app/data/system-update", timeout: float = 120.0) -> dict[str, str]:
    repo = _validate_github_repo(repo)
    branch = (branch or "main").strip() or "main"
    url = f"{api_url.rstrip('/')}/repos/{repo}/zipball/{branch}"
    headers = _github_headers(token)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            r = client.get(url)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"GitHub 下载失败：{exc}") from exc
    if r.status_code == 401:
        raise RuntimeError("GitHub Token 无效或已过期")
    if r.status_code == 403:
        raise RuntimeError("GitHub Token 没有下载该仓库的权限")
    if r.status_code == 404:
        raise RuntimeError("找不到 GitHub 仓库或分支")
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"GitHub 下载失败：HTTP {r.status_code}") from exc
    return stage_update_zip(r.content, f"github-{repo.replace('/', '-')}-{branch}.zip", data_root=data_root)
