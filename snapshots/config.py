"""Settings: defaults, then config.toml, then command-line overrides."""

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_data_dir() -> Path:
    """Where the database and thumbnails live (never inside your photo folders)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Snapshots"
    return Path.home() / ".snapshots"


@dataclass
class Config:
    library_path: Path | None = None
    data_dir: Path = field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8080
    index_on_start: bool = True
    scan_interval_minutes: int = 15        # 0 turns automatic rescans off
    faces: bool = True                     # find and group faces
    places: bool = True                    # turn GPS positions into place names
    face_threads: int = 2                  # CPU threads face recognition may use
    face_match_threshold: float = 0.50     # higher = stricter grouping (more, smaller groups)
    min_faces_per_person: int = 3          # unnamed groups smaller than this are not listed
    face_merge_threshold: float = 0.42     # merge two groups this alike on average (0 turns it off)
    face_suggest_threshold: float = 0.30   # suggest (not make) merges of groups this alike on average
    photo_checks: bool = True              # find copies, bursts, blurry and dark photos
    duplicate_distance: int = 8            # copies differ in at most this many of 64 fingerprint bits
    burst_seconds: int = 30                # photos this close in time ...
    burst_distance: int = 14               # ... and this alike are a burst
    blur_threshold: float = 50             # sharpness below this is blurry
    dark_threshold: float = 30             # average brightness (0-255) below this is too dark
    event_gap_hours: float = 4             # a pause this long starts a new event
    event_min_items: int = 8               # smaller events are not listed
    smart_search: bool = True              # search by what is in a photo (downloads a 154 MB model)
    text_search: bool = True               # read text in photos so it can be searched (11 MB model)
    search_threads: int = 2                # CPU threads content and text search may use
    map_tiles: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"  # map pictures ("" = outlines only)
    map_attribution: str = "© OpenStreetMap contributors"
    extra_libraries: list = field(default_factory=list)  # more photo folders, besides library_path
    backup_hours: float = 24               # copy the database this often (0 turns backups off)
    backup_keep: int = 14                  # how many backups to keep
    backup_dir: Path | None = None         # where backups go (default: "backups" in the data folder)
    video_encoder: str = "auto"            # how videos are converted: auto, libx264, h264_amf, h264_nvenc, h264_qsv
    video_cache_gb: float = 20             # converted videos kept for playing, at most this much
    offsite_method: str = ""               # copies of the photos themselves: "" (off), folder, rclone or restic
    offsite_target: str = ""               # a folder (folder), an rclone remote:path, or a restic repository
    offsite_hours: float = 24              # how often the copy is brought up to date
    offsite_password_file: Path | None = None  # restic: a file holding the repository password
    health_check_days: float = 0           # check every file for damage this often (0: only when asked)
    review_seconds: float = 4              # year in review videos: seconds each photo is shown
    xmp_sidecars: bool = False             # write descriptions, tags, people and dates to .xmp files beside photos
    upload_template: str = "Uploads/{year}/{month}"  # where uploads and imported files are filed
    config_file: Path | None = None  # the file these settings were read from, if any

    @property
    def db_path(self) -> Path:
        return self.data_dir / "snapshots.db"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "previews"

    @property
    def face_dir(self) -> Path:
        return self.data_dir / "faces"

    @property
    def model_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def places_dir(self) -> Path:
        return self.data_dir / "places"

    @property
    def motion_dir(self) -> Path:
        return self.data_dir / "motion"

    @property
    def edited_dir(self) -> Path:
        return self.data_dir / "edited"

    @property
    def video_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def backups_path(self) -> Path:
        return Path(self.backup_dir) if self.backup_dir else self.data_dir / "backups"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    def thumb_path(self, asset_id: int) -> Path:
        return self.thumb_dir / f"{asset_id}.jpg"

    def preview_path(self, asset_id: int) -> Path:
        return self.preview_dir / f"{asset_id}.jpg"

    def face_path(self, face_id: int) -> Path:
        return self.face_dir / f"{face_id}.jpg"

    def motion_path(self, asset_id: int) -> Path:
        return self.motion_dir / f"{asset_id}.mp4"

    def edited_path(self, asset_id: int) -> Path:
        return self.edited_dir / f"{asset_id}.jpg"

    def video_path(self, asset_id: int) -> Path:
        return self.video_dir / f"{asset_id}.mp4"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.thumb_dir, self.preview_dir, self.face_dir,
                  self.model_dir, self.places_dir, self.motion_dir, self.edited_dir, self.video_dir):
            d.mkdir(parents=True, exist_ok=True)


# Settings that may appear in config.toml, with the type each must have.
_FILE_KEYS = {
    "host": str, "port": int, "index_on_start": bool, "scan_interval_minutes": int,
    "faces": bool, "places": bool, "face_threads": int, "face_match_threshold": float,
    "min_faces_per_person": int, "face_merge_threshold": float, "face_suggest_threshold": float,
    "photo_checks": bool, "duplicate_distance": int, "burst_seconds": int, "burst_distance": int,
    "blur_threshold": float, "dark_threshold": float, "event_gap_hours": float, "event_min_items": int,
    "smart_search": bool, "text_search": bool, "search_threads": int,
    "map_tiles": str, "map_attribution": str,
    "backup_hours": float, "backup_keep": int, "video_encoder": str, "video_cache_gb": float,
    "offsite_method": str, "offsite_target": str, "offsite_hours": float,
    "xmp_sidecars": bool, "upload_template": str, "health_check_days": float, "review_seconds": float,
}
VIDEO_ENCODERS = ("auto", "libx264", "h264_amf", "h264_nvenc", "h264_qsv")
OFFSITE_METHODS = ("", "folder", "rclone", "restic")


def user_config_path() -> Path:
    """Where the folder chosen on first run is remembered (an exe has no config.toml beside it)."""
    return default_data_dir() / "config.toml"


def save_library_path(library: Path, config_file: Path | None = None) -> Path:
    """Remember the photo folder, keeping any other settings already in the file."""
    path = Path(config_file) if config_file else user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A JSON string is a valid TOML string, and handles backslashes, quotes and accents.
    line = "library_path = " + json.dumps(str(library), ensure_ascii=False)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if re.search(r"(?m)^library_path\s*=.*$", text):
        text = re.sub(r"(?m)^library_path\s*=.*$", lambda _m: line, text, count=1)
    else:
        text = line + "\n" + text
    path.write_text(text, encoding="utf-8")
    return path


def _toml_list(paths) -> str:
    return "[" + ", ".join(json.dumps(str(p), ensure_ascii=False) for p in paths) + "]"


def save_extra_libraries(paths, config_file: Path | None = None) -> Path:
    """Remember the extra photo folders, keeping any other settings already in the file."""
    path = Path(config_file) if config_file else user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = "extra_libraries = " + _toml_list(paths)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    pattern = r"(?ms)^extra_libraries\s*=\s*\[.*?\]"
    if re.search(pattern, text):
        text = re.sub(pattern, lambda _m: line, text, count=1)
    else:
        text = text + ("" if not text or text.endswith("\n") else "\n") + line + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def save_settings(values: dict, config_file: Path | None = None) -> Path:
    """Write simple settings (text, numbers, true/false) to config.toml, keeping everything else in the file."""
    path = Path(config_file) if config_file else user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for key, value in values.items():
        if value is None:
            value = ""
        if isinstance(value, bool):
            shown = "true" if value else "false"
        elif isinstance(value, (int, float)):
            shown = repr(value)
        else:
            shown = json.dumps(str(value), ensure_ascii=False)
        line = f"{key} = {shown}"
        pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
        if re.search(pattern, text):
            text = re.sub(pattern, lambda _m: line, text, count=1)
        else:
            text = text + ("" if not text or text.endswith("\n") else "\n") + line + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def load_config(config_file: str | os.PathLike | None = None, **overrides) -> Config:
    """Build a Config. `overrides` (from the command line) win over the file.

    Without an explicit file, ./config.toml is used if present, otherwise the one in the
    user's Snapshots data folder.
    """
    cfg = Config()
    if config_file:
        path = Path(config_file)
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {config_file}")
    else:
        path = next((p for p in (Path("config.toml"), user_config_path()) if p.is_file()), None)
    if path is not None:
        cfg.config_file = path
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        if "library_path" in data:
            cfg.library_path = Path(data["library_path"])
        if "data_dir" in data:
            cfg.data_dir = Path(data["data_dir"])
        if "backup_dir" in data:
            cfg.backup_dir = Path(data["backup_dir"]) if data["backup_dir"] else None
        if "offsite_password_file" in data:
            cfg.offsite_password_file = Path(data["offsite_password_file"]) if data["offsite_password_file"] else None
        if "extra_libraries" in data:
            extra = data["extra_libraries"]
            if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
                raise ValueError(f"{path}: 'extra_libraries' should be a list of folders")
            cfg.extra_libraries = [Path(x) for x in extra if x.strip()]
        for key, kind in _FILE_KEYS.items():
            if key in data:
                try:
                    setattr(cfg, key, kind(data[key]))
                except (TypeError, ValueError):
                    raise ValueError(f"{path}: '{key}' should be a {kind.__name__}") from None
        if cfg.video_encoder not in VIDEO_ENCODERS:
            raise ValueError(f"{path}: 'video_encoder' should be one of {', '.join(VIDEO_ENCODERS)}")
        if cfg.offsite_method not in OFFSITE_METHODS:
            raise ValueError(f"{path}: 'offsite_method' should be \"\", folder, rclone or restic")
        from .storage import template_problem  # here, not at the top: storage imports this module
        problem = template_problem(cfg.upload_template)
        if problem:
            raise ValueError(f"{path}: 'upload_template': {problem}")

    for key, value in overrides.items():
        if value is None:
            continue
        if key in ("library_path", "data_dir"):
            value = Path(value)
        setattr(cfg, key, value)
    return cfg
