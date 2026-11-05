"""Which videos play as they are depends on the window asking: HEVC and AV1 need a codec from Windows."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers import ids_by_name, new_client, setup_admin, wait_scan  # noqa: E402

from snapshots import media  # noqa: E402


def test_what_plays_depends_on_the_window():
    hevc = {"codec": "hevc", "playable": False}
    av1 = {"codec": "av1", "playable": True}
    h264 = {"codec": "h264", "playable": True}
    # Without being told, the usual list: AV1 yes, HEVC no.
    assert media.playable_here(av1, "a.mp4") and not media.playable_here(hevc, "a.mp4")
    # A window with the HEVC codec plays HEVC as it is; one without AV1 is sent a converted copy.
    assert media.playable_here(hevc, "a.mov", "h264,hevc")
    assert not media.playable_here(av1, "a.mp4", "h264,hevc")
    assert media.playable_here(av1, "a.mp4", "h264,av1")
    # H.264 always counts, even from a window that listed nothing (or something odd).
    assert media.playable_here(h264, "a.mp4", "") and media.playable_here(h264, "a.mp4", "nonsense")
    # A container browsers don't open is converted (repackaged) whatever the codec.
    assert not media.playable_here(h264, "a.mkv", "h264,hevc,av1")
    assert not media.playable_here(hevc, "a.mkv", "h264,hevc")
    # Unknown codec (no video stream found): left alone.
    assert media.playable_here({"codec": None}, "a.mp4", "h264")


def test_play_uses_the_windows_codecs(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    root.mkdir()
    (root / "clip.mov").write_bytes(b"not really a video" * 50)
    monkeypatch.setattr(media, "probe_video", lambda path: {"codec": "hevc", "duration": 1.0, "playable": False})
    monkeypatch.setattr(media, "has_ffmpeg", lambda: True)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        vid = ids["clip.mov"]
        # A window that can play HEVC is given the file itself, with nothing to prepare.
        info = c.get(f"/api/assets/{vid}/play", params={"can": "h264,hevc,av1"}).json()
        assert info["converted"] is False and info["url"].startswith(f"/api/assets/{vid}/view")
        assert "preparing" not in info
        # One that can't, only when asked not to convert, still gets the file, marked as convertible.
        info = c.get(f"/api/assets/{vid}/play", params={"can": "h264", "convert": "false"}).json()
        assert info["can_convert"] is True and info["codec"] == "hevc"


def _make_mp4(path, frames=60, fps=20):
    import cv2
    import numpy as np

    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    if not out.isOpened():
        return False
    rng = np.random.default_rng(1)
    for k in range(frames):
        frame = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
        out.write(frame)
    out.release()
    return path.exists() and path.stat().st_size > 0


def test_video_thumbnails_need_no_ffmpeg(tmp_path):
    import pytest
    from PIL import Image

    src = tmp_path / "clip.mp4"
    if not _make_mp4(src):
        pytest.skip("this OpenCV can't write mp4")
    dst = tmp_path / "t.jpg"
    assert media._opencv_thumb(src, dst)
    with Image.open(dst) as img:
        assert img.size == (320, 240)
    short = tmp_path / "short.mp4"
    _make_mp4(short, frames=10)
    assert media._opencv_thumb(short, tmp_path / "s.jpg")
    assert not media._opencv_thumb(tmp_path / "missing.mp4", tmp_path / "m.jpg")


def test_the_scan_gives_old_videos_their_picture(tmp_path, monkeypatch):
    import pytest

    root = tmp_path / "Photos"
    root.mkdir()
    if not _make_mp4(root / "v.mp4"):
        pytest.skip("this OpenCV can't write mp4")
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        vid = ids_by_name(c)["v.mp4"]
        assert c.get(f"/api/assets/{vid}/thumb").status_code == 200
