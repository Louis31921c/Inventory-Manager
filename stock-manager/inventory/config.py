import getpass
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP = "stock-manager"


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP


def default_data_dir():
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP


def settings_file():
    return config_dir() / "settings.env"


@dataclass(frozen=True)
class SmsConfig:
    provider: str
    to: str
    sender: str
    account_sid: str
    auth_token: str
    webhook_url: str
    webhook_token: str
    max_per_hour: int

    @property
    def enabled(self):
        if not self.to or self.provider == "none":
            return False
        if self.provider == "twilio":
            return bool(self.account_sid and self.auth_token and self.sender)
        if self.provider == "webhook":
            return bool(self.webhook_url)
        return False


@dataclass(frozen=True)
class Config:
    backend: str
    gemini_api_key: str
    photo_retention_days: int
    data_dir: Path
    model: str
    user: str
    sms: SmsConfig

    @property
    def db_path(self):
        return self.data_dir / "inventory.duckdb"

    @property
    def images_dir(self):
        return self.data_dir / "images"


def _sms():
    return SmsConfig(
        provider=os.environ.get("SMS_PROVIDER", "none").strip().lower(),
        to=os.environ.get("SMS_TO", "").strip(),
        sender=os.environ.get("SMS_FROM", "").strip(),
        account_sid=os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
        auth_token=os.environ.get("TWILIO_AUTH_TOKEN", "").strip(),
        webhook_url=os.environ.get("SMS_WEBHOOK_URL", "").strip(),
        webhook_token=os.environ.get("SMS_WEBHOOK_TOKEN", "").strip(),
        max_per_hour=int(os.environ.get("SMS_MAX_PER_HOUR", "6")),
    )


def _who():
    name = os.environ.get("STOCK_USER", "").strip()
    if name:
        return name
    try:
        return getpass.getuser()
    except Exception:
        return "?"


def set_setting(key, value, path=None):
    """Write one KEY=value into the settings file, replacing any line that set it."""
    path = Path(path or settings_file())
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    os.environ[key] = str(value)
    return path


def load_config(data_dir=None, user=None):
    load_dotenv(settings_file())
    here = Path.cwd() / ".env"
    if here.is_file():
        load_dotenv(here)
    chosen = data_dir or os.environ.get("DATA_DIR") or default_data_dir()
    return Config(
        backend=os.environ.get("LLM_BACKEND", "claude").strip().lower(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        photo_retention_days=int(os.environ.get("PHOTO_RETENTION_DAYS", "42")),
        data_dir=Path(chosen).expanduser(),
        model=os.environ.get("LLM_MODEL", ""),
        user=user or _who(),
        sms=_sms(),
    )
