"""Configuration loading for fbwatch."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_FILENAME = "config.json"
WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"
BOT_TOKEN_ENV_VAR = "DISCORD_BOT_TOKEN"
TELEGRAM_TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"


@dataclass
class Config:
    # --- Discord -------------------------------------------------------
    discord_webhook_url: str = ""
    discord_username: str = "Facebook Watcher"
    discord_avatar_url: str = ""
    embed_color: int = 0x1877F2  # Facebook blue
    include_images: bool = True
    notify_errors: bool = True

    # --- Telegram (notifications only) ---------------------------------
    # One bot serves everyone; each person is reached by their own chat id,
    # set per subscriber in subscribers.json.
    telegram_bot_token: str = ""

    # --- Configuring from Discord --------------------------------------
    # Optional.  Needs a bot token; a webhook can only send, never receive.
    control_enabled: bool = True
    discord_bot_token: str = ""
    discord_control_channel_id: str = ""
    command_prefix: str = "!fbw"
    control_poll_seconds: int = 5
    # Notify people in the channel they type commands in, unless they set a
    # channel of their own.  Means a new person needs only trigger words.
    notify_in_control_channel: bool = True
    # Let people subscribe themselves by using a command, instead of an admin
    # adding them first.  The control channel already limits who can do this.
    allow_self_signup: bool = True
    # Empty means anyone who can post in the control channel may reconfigure
    # the watcher.  List Discord user ids to restrict it.
    control_allowed_user_ids: list = field(default_factory=list)

    # --- Polling -------------------------------------------------------
    poll_interval_seconds: int = 300
    jitter_seconds: int = 90
    min_delay_between_groups: float = 6.0
    max_delay_between_groups: float = 20.0
    posts_per_group: int = 15
    notify_on_first_run: bool = False
    restart_browser_every_cycles: int = 24

    # --- Browser -------------------------------------------------------
    headless: bool = True
    browser_profile_dir: str = "browser_profile"
    locale: str = "sl-SI"
    timezone: str = "Europe/Ljubljana"
    block_media: bool = True
    page_timeout_seconds: int = 45

    # --- Files ---------------------------------------------------------
    groups_file: str = "groups.txt"
    keywords_file: str = "keywords.txt"
    subscribers_file: str = "subscribers.json"
    state_file: str = "state.json"
    log_file: str = "fbwatch.log"
    state_retention_days: int = 30

    def __post_init__(self) -> None:
        self.base_dir = Path.cwd()

    # -- helpers --------------------------------------------------------
    @property
    def webhook_configured(self) -> bool:
        """True only for a real webhook - not the placeholder in the example."""
        url = (self.discord_webhook_url or "").strip()
        if not url.startswith("http"):
            return False
        return "XXXX" not in url and "YYYY" not in url

    def path(self, value: str) -> Path:
        """Resolve a configured path relative to the config file's folder."""
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def groups_path(self) -> Path:
        return self.path(self.groups_file)

    @property
    def keywords_path(self) -> Path:
        return self.path(self.keywords_file)

    @property
    def subscribers_path(self) -> Path:
        return self.path(self.subscribers_file)

    @property
    def state_path(self) -> Path:
        return self.path(self.state_file)

    @property
    def log_path(self) -> Path:
        return self.path(self.log_file)

    @property
    def profile_path(self) -> Path:
        return self.path(self.browser_profile_dir)

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Config":
        """Read config.json; unknown keys are reported rather than ignored."""
        path = Path(config_path) if config_path else Path.cwd() / CONFIG_FILENAME
        cfg = cls()
        cfg.base_dir = path.parent.resolve() if path.parent.as_posix() else Path.cwd()

        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} is not valid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"{path} must contain a JSON object")

            known = {f.name for f in fields(cls)}
            # Keys starting with "_" are notes to the reader, not settings.
            unknown = sorted(k for k in set(data) - known if not k.startswith("_"))
            if unknown:
                raise ValueError(
                    f"{path}: unknown setting(s): {', '.join(unknown)}. "
                    f"Valid settings: {', '.join(sorted(known))}"
                )
            for key, value in data.items():
                if key in known:
                    setattr(cfg, key, value)

        # An env var wins over the file, so the webhook can stay out of the
        # config on a shared machine.
        env_hook = os.environ.get(WEBHOOK_ENV_VAR, "").strip()
        if env_hook:
            cfg.discord_webhook_url = env_hook
        env_token = os.environ.get(BOT_TOKEN_ENV_VAR, "").strip()
        if env_token:
            cfg.discord_bot_token = env_token
        env_tg = os.environ.get(TELEGRAM_TOKEN_ENV_VAR, "").strip()
        if env_tg:
            cfg.telegram_bot_token = env_tg

        if isinstance(cfg.embed_color, str):
            cfg.embed_color = int(cfg.embed_color.lstrip("#"), 16)

        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.poll_interval_seconds < 60:
            raise ValueError(
                "poll_interval_seconds must be at least 60 - polling Facebook "
                "harder than that invites a temporary block"
            )
        if self.posts_per_group < 1:
            raise ValueError("posts_per_group must be at least 1")
        if self.min_delay_between_groups > self.max_delay_between_groups:
            raise ValueError(
                "min_delay_between_groups cannot exceed max_delay_between_groups"
            )
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds cannot be negative")
        if self.restart_browser_every_cycles < 1:
            raise ValueError("restart_browser_every_cycles must be at least 1")
        if self.state_retention_days < 1:
            raise ValueError("state_retention_days must be at least 1")
        if self.control_poll_seconds < 2:
            raise ValueError("control_poll_seconds must be at least 2")
        if not str(self.command_prefix).strip():
            raise ValueError("command_prefix cannot be empty")
        if not isinstance(self.control_allowed_user_ids, list):
            raise ValueError("control_allowed_user_ids must be a list of Discord user ids")
