"""Application settings loaded from .env."""
import re
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    tv_ip: str = ""  # empty = not configured; first-run UI will prompt for one
    data_dir: Path = Path("data")

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "tv_token.txt"

    @property
    def mac_file(self) -> Path:
        """Cached TV MAC address for Wake-on-LAN. Written when the TV is
        reachable (REST device-info exposes wifiMac); read by wake() so
        we can wake the TV from full-off when the WS API can't reach it."""
        return self.data_dir / "tv_mac.txt"

    @property
    def remote_token_file(self) -> Path:
        """Separate token for the remote-control channel (KEY_POWER etc.).
        The TV authorizes art and remote independently even though
        samsungtvws shares one token by default — keeping them separate
        avoids auth state from one channel poisoning the other."""
        return self.data_dir / "tv_remote_token.txt"

    def update_tv_ip(self, new_ip: str) -> None:
        """Update tv_ip in memory and rewrite the .env file so the change
        survives a restart. Caller is responsible for resetting any open
        TV connection."""
        self.tv_ip = new_ip
        if not ENV_FILE.exists():
            ENV_FILE.write_text(f"TV_IP={new_ip}\n")
            return
        text = ENV_FILE.read_text()
        if re.search(r"^TV_IP=.*$", text, flags=re.MULTILINE):
            text = re.sub(r"^TV_IP=.*$", f"TV_IP={new_ip}", text, flags=re.MULTILINE)
        else:
            if not text.endswith("\n"):
                text += "\n"
            text += f"TV_IP={new_ip}\n"
        ENV_FILE.write_text(text)


settings = Settings()
settings.thumb_dir.mkdir(parents=True, exist_ok=True)
