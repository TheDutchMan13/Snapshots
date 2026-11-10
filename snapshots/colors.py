"""The main colours of each photo, so a search can ask for them ("red car", "blue sea").

Each thumbnail is shrunk to 64 pixels and every pixel is named: black, white or grey when it has little
colour, otherwise by its hue (red, orange, yellow, green, blue, purple, pink), and dark orange and red
count as brown. Vivid pixels count more than dull ones, because they are what the eye notices. Colours
that cover enough of the picture are stored, most first. Word search finds them like any other word,
and search by content puts photos that have the colour asked for a little higher.
"""

import logging

log = logging.getLogger("snapshots.colors")

NAMES = ("red", "orange", "yellow", "green", "blue", "purple", "pink", "brown", "black", "white", "gray")
WORDS = {**{n: n for n in NAMES}, "grey": "gray", "violet": "purple", "teal": "blue", "turquoise": "blue",
         "gold": "yellow", "golden": "yellow"}
SIDE = 64
MIN_SHARE = 0.12       # a colour must cover this much of the picture (after weighting) to count
MAX_COLORS = 3
CONTENT_BOOST = 0.03   # search by content: how much higher a photo with the colour asked for comes


def name_pixels(rgb):
    """Colour name index (into NAMES) and weight of every pixel of an (n, 3) uint8 array."""
    import numpy as np

    x = rgb.astype(np.float32) / 255.0
    mx, mn = x.max(axis=1), x.min(axis=1)
    v = mx
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    r, g, b = x[:, 0], x[:, 1], x[:, 2]
    d = np.maximum(mx - mn, 1e-6)
    h = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60.0
    idx = np.full(len(x), NAMES.index("gray"))
    hue = np.select(
        [(h < 15) | (h >= 345), h < 40, h < 70, h < 170, h < 255, h < 290, h < 345],
        [NAMES.index(n) for n in ("red", "orange", "yellow", "green", "blue", "purple", "pink")], 0)
    coloured = s >= 0.25
    idx = np.where(coloured, hue, idx)
    brown = coloured & ((hue == NAMES.index("orange")) | (hue == NAMES.index("red"))) & (v < 0.55) & (s < 0.85)
    idx = np.where(brown, NAMES.index("brown"), idx)
    idx = np.where(~coloured & (v < 0.2), NAMES.index("black"), idx)
    idx = np.where(~coloured & (v > 0.85) & (s < 0.12), NAMES.index("white"), idx)
    idx = np.where(coloured & (v < 0.15), NAMES.index("black"), idx)
    weight = np.where(coloured, 0.5 + s * v, 0.5)
    return idx, weight


def main_colors(img) -> list[str]:
    """The main colours of a picture, most first."""
    import numpy as np

    small = img.convert("RGB").resize((SIDE, SIDE))
    idx, weight = name_pixels(np.asarray(small).reshape(-1, 3))
    share = np.bincount(idx, weights=weight, minlength=len(NAMES))
    share = share / share.sum()
    order = [int(i) for i in np.argsort(-share) if share[i] >= MIN_SHARE]
    return [NAMES[i] for i in order[:MAX_COLORS]]


def color_words(text: str) -> set[str]:
    """The colour names asked for in a search."""
    return {WORDS[w] for w in text.lower().split() if w in WORDS}


class ColorJob:
    """Names the main colours of photos (from their thumbnails), a batch at a time."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0}

    def run_batch(self, conn, now: float, should_stop, batch: int = 100) -> bool:
        from PIL import Image

        rows = conn.execute("SELECT id FROM assets WHERE colors_done = 0 AND has_thumb = 1 AND trashed_at IS NULL AND missing_since IS NULL"
                            " LIMIT ?", (batch,)).fetchall()
        if not rows:
            self.state["running"] = False
            return False
        self.state["running"] = True
        for (asset_id,) in rows:
            if should_stop():
                break
            colors = None
            try:
                with Image.open(self.cfg.thumb_path(asset_id)) as img:
                    colors = " ".join(main_colors(img)) or None
            except Exception as exc:
                log.info("no colours for asset %s: %s", asset_id, exc)
            conn.execute("UPDATE assets SET colors = ?, colors_done = 1 WHERE id = ?", (colors, asset_id))
        conn.commit()
        return True
