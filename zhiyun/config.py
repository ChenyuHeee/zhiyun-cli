"""本地配置与凭证存储。

默认位置：~/.config/zhiyun/（可用 ZHIYUN_CONFIG_DIR 覆盖）。
文件权限尽量收紧到 0600。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


APP_NAME = "zhiyun"


def config_dir() -> Path:
    override = os.environ.get("ZHIYUN_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_NAME


CONFIG_FILE = config_dir() / "config.json"
CREDENTIALS_FILE = config_dir() / "credentials.json"


@dataclass
class Config:
    jwt: str = ""
    username: str = ""
    student_id: str = ""
    expires_at: int = 0
    saved_at: int = 0
    source: str = ""

    def is_expired(self, leeway: int = 60) -> bool:
        return bool(self.expires_at) and time.time() >= (self.expires_at - leeway)

    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time())) if self.expires_at else 0

    def masked_token(self) -> str:
        if not self.jwt:
            return "(未设置)"
        return f"{self.jwt[:12]}…{self.jwt[-6:]}" if len(self.jwt) > 24 else "…"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> Config:
    raw = _read_json(CONFIG_FILE)
    cfg = Config()
    for field_name in cfg.__dataclass_fields__:
        if field_name in raw:
            value = raw[field_name]
            if field_name in {"expires_at", "saved_at"}:
                try:
                    value = int(value or 0)
                except (TypeError, ValueError):
                    value = 0
            setattr(cfg, field_name, str(value) if field_name not in {"expires_at", "saved_at"} else value)
    return cfg


def save_config(cfg: Config) -> None:
    data = asdict(cfg)
    _write_private(CONFIG_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_credentials() -> dict[str, str]:
    raw = _read_json(CREDENTIALS_FILE)
    username = str(raw.get("username", "") or "")
    password = str(raw.get("password", "") or "")
    return {"username": username, "password": password} if username and password else {}


def save_credentials(username: str, password: str) -> None:
    _write_private(
        CREDENTIALS_FILE,
        json.dumps({"username": username, "password": password}, ensure_ascii=False, indent=2) + "\n",
    )


def delete_config(*, keep_credentials: bool = False) -> None:
    for path in (CONFIG_FILE, CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    if not keep_credentials:
        for path in (CREDENTIALS_FILE, CREDENTIALS_FILE.with_name(CREDENTIALS_FILE.name + ".tmp")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
