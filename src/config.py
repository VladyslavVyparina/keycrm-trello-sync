import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    keycrm_api_token: str
    keycrm_base_url: str
    keycrm_pipeline_id: int
    keycrm_require_phone: bool
    sync_auto_create_trello_cards: bool
    sync_bidirectional: bool
    sync_comments: bool
    trello_api_key: str
    trello_token: str
    trello_base_url: str
    trello_board_id: str
    poll_interval_seconds: int
    log_file: str
    keycrm_status_map: Dict[str, str]


def load_settings(root_dir: Path) -> Settings:
    _load_env_file(root_dir / ".env")
    map_path = root_dir / "config" / "keycrm_status_map.json"
    keycrm_status_map = json.loads(map_path.read_text(encoding="utf-8"))

    return Settings(
        keycrm_api_token=os.getenv("KEYCRM_API_TOKEN", ""),
        keycrm_base_url=os.getenv("KEYCRM_BASE_URL", "https://openapi.keycrm.app/v1"),
        keycrm_pipeline_id=int(os.getenv("KEYCRM_PIPELINE_ID", "0")),
        keycrm_require_phone=_env_bool("KEYCRM_REQUIRE_PHONE", default=False),
        sync_auto_create_trello_cards=_env_bool("SYNC_AUTO_CREATE_TRELLO_CARDS", default=True),
        sync_bidirectional=_env_bool("SYNC_BIDIRECTIONAL", default=True),
        sync_comments=_env_bool("SYNC_COMMENTS", default=True),
        trello_api_key=os.getenv("TRELLO_API_KEY", ""),
        trello_token=os.getenv("TRELLO_TOKEN", ""),
        trello_base_url=os.getenv("TRELLO_BASE_URL", "https://api.trello.com/1"),
        trello_board_id=os.getenv("TRELLO_BOARD_ID", ""),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "30")),
        log_file=os.getenv("LOG_FILE", "logs/sync.log"),
        keycrm_status_map=keycrm_status_map,
    )


def validate_settings(settings: Settings) -> None:
    problems = []
    if not settings.keycrm_api_token:
        problems.append("KEYCRM_API_TOKEN")
    if settings.keycrm_pipeline_id <= 0:
        problems.append("KEYCRM_PIPELINE_ID (must be a positive integer)")
    if not settings.trello_api_key:
        problems.append("TRELLO_API_KEY")
    if not settings.trello_token:
        problems.append("TRELLO_TOKEN")
    if not settings.trello_board_id:
        problems.append("TRELLO_BOARD_ID")
    if problems:
        raise ValueError(f"Missing or invalid configuration: {', '.join(problems)}")

    if not settings.keycrm_status_map:
        raise ValueError("config/keycrm_status_map.json must contain at least one status mapping")
