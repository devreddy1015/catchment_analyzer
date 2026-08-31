"""Runtime settings. Everything has a safe default; nothing requires an API key."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    PROJECT_NAME = "Catchment Analyzer"
    VERSION = "2.1.0"
    API_PREFIX = "/api"

    # Where generated overlay PNGs are written and served from.
    STORAGE_DIR = Path(os.getenv("STORAGE_DIR", BASE_DIR / "storage"))

    # Upload guard rails.
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "64"))
    ALLOWED_EXTENSIONS = (".kml", ".kmz")

    # Terrain grid resolution (cells per side) and the range a caller may ask for.
    DEFAULT_RESOLUTION = int(os.getenv("GRID_RESOLUTION", "160"))
    MIN_RESOLUTION = 48
    MAX_RESOLUTION = 260

    # Keep only the most recent overlay files on disk.
    MAX_STORED_OVERLAYS = 60

    # ---------------------------------------------------------------- #
    # OpenZenith — the global elevation service used when no contour
    # file is uploaded. Free and open: no key, no account, no quota.
    # https://openzenith.org
    # ---------------------------------------------------------------- #
    ELEVATION_API_URL = os.getenv("ELEVATION_API_URL", "https://openzenith.cyopsys.com").rstrip("/")
    ELEVATION_API_ENABLED = os.getenv("ELEVATION_API_ENABLED", "1") not in ("0", "false", "no")
    ELEVATION_TIMEOUT_S = float(os.getenv("ELEVATION_TIMEOUT_S", "45"))
    ELEVATION_RETRIES = int(os.getenv("ELEVATION_RETRIES", "3"))
    ELEVATION_WORKERS = int(os.getenv("ELEVATION_WORKERS", "6"))

    # Terrain tiles are 256x256 int16 rasters on the web-mercator pyramid. Zoom
    # 14 is finer than the 30 m source data, so asking for more buys nothing.
    ELEVATION_MAX_ZOOM = int(os.getenv("ELEVATION_MAX_ZOOM", "14"))
    ELEVATION_MAX_TILES = int(os.getenv("ELEVATION_MAX_TILES", "64"))

    # Downloaded tiles are immutable, so they are worth keeping between runs.
    # Kept out of STORAGE_DIR, which is mounted for the browser to read.
    ELEVATION_CACHE_DIR = Path(os.getenv("ELEVATION_CACHE_DIR", BASE_DIR / ".dem_cache"))
    ELEVATION_CACHE_TILES = int(os.getenv("ELEVATION_CACHE_TILES", "600"))

    # How large an area may be requested by coordinates, per side.
    MIN_AREA_KM = 0.25
    MAX_AREA_KM = float(os.getenv("MAX_AREA_KM", "12"))
    DEFAULT_AREA_KM = 2.5

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @property
    def user_agent(self) -> str:
        return os.getenv(
            "ELEVATION_API_USER_AGENT",
            f"{self.PROJECT_NAME.replace(' ', '')}/{self.VERSION} (+https://openzenith.org)",
        )


settings = Settings()
settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
