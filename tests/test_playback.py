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
