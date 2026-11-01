"""A sentence describing each photo ("A red motorcycle parked in a garage next to a shelf"), on this computer.

Uses Microsoft's Florence-2 (base, fine-tuned; MIT licence), converted to ONNX and 8-bit quantized by the
ONNX community (https://huggingface.co/onnx-community/Florence-2-base-ft). About 275 MB, downloaded once
and checked against known SHA-256 hashes. Nothing leaves the computer.

How it works: the picture (768 x 768) becomes 577 vectors (the vision encoder); those and the question
"What does the image describe?" go through the language encoder; the decoder then writes the answer one
word piece at a time (greedy search, no three-piece phrase twice), until it says it is done.

It is slow on a processor (one to several seconds a photo), so it is off unless switched on and runs
after everything else, newest photos first. Captions are searchable like descriptions and shown in the
photo's details.
"""

import json
import logging
import threading
from pathlib import Path

from . import downloads

log = logging.getLogger("snapshots.captions")

_REVISION = "e88a44eaf3791a35eae0c5a47b3dbcd36e67eb6f"
_HF = f"https://huggingface.co/onnx-community/Florence-2-base-ft/resolve/{_REVISION}/"
FILES = {
    "vision_encoder.onnx": {
        "sha256": "3b79d54f23f666f731549db23cb070c35a979ce19cbd9720e90e67a78dc9768c",
        "urls": [_HF + "onnx/vision_encoder_quantized.onnx"],
    },
    "embed_tokens.onnx": {
        "sha256": "6b2258db1c8ee9b160576ccde3cd3814d83a2edaed0dd1c6ca9ff3c38fa62214",
        "urls": [_HF + "onnx/embed_tokens_quantized.onnx"],
    },
    "encoder_model.onnx": {
        "sha256": "f4ad7a68f1fb875d3bcf735ea14a7021b7ba7e83baf7cf10289881b4ed6d9b85",
        "urls": [_HF + "onnx/encoder_model_quantized.onnx"],
    },
    "decoder_model_merged.onnx": {
        "sha256": "f22f52f980c33df0efa15932c2f3db6d9d3595ce6387eca938b8cfe23dc4c641",
        "urls": [_HF + "onnx/decoder_model_merged_quantized.onnx"],
    },
    "vocab.json": {
        "sha256": "394fdc63c71aabe0a9b97117f5d62fb5fcc4d59b2b3ea929a3929e6a53217b3c",
        "urls": [_HF + "vocab.json"],
    },
}
DOWNLOAD_MB = 275

SIZE = 768
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)
# "<s>What does the image describe?</s>" in the model's word pieces (Florence-2's <CAPTION> task).
PROMPT = [0, 2264, 473, 5, 2274, 6190, 116, 2]
BOS, PAD, EOS, UNK = 0, 1, 2, 3
DECODER_START = 2
LAYERS, HEADS, HEAD_DIM = 6, 12, 64
MAX_PIECES = 40
PICTURE_SIDE = 1024    # the photo is read at this size (the model looks at it at 768 x 768)
MAX_CHARS = 300


def _byte_decoder() -> dict[str, int]:
    """The reverse of GPT-2's (and BART's) byte-to-character table."""
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {chr(c): b for b, c in zip(bs, cs)}


def tidy(text: str) -> str | None:
    """The caption as stored: one line, a capital first letter, a full stop; None if empty."""
    text = " ".join(text.split()).strip()
    if not text:
        return None
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text[:MAX_CHARS]


def no_repeat(ids: list[int], size: int = 3) -> set[int]:
    """Word pieces that would repeat a phrase of `size` pieces already written."""
    if len(ids) < size:
        return set()
    tail = tuple(ids[-(size - 1):])
    return {ids[i + size - 1] for i in range(len(ids) - size + 1) if tuple(ids[i:i + size - 1]) == tail}


def preprocess(img):
    import numpy as np
    from PIL import Image

    img = img.convert("RGB").resize((SIZE, SIZE), Image.BICUBIC)
    x = np.asarray(img, dtype=np.float32) / 255.0
    x = (x - np.array(MEAN, np.float32)) / np.array(STD, np.float32)
    return x.transpose(2, 0, 1)[None].astype(np.float32)


class CaptionEngine:
    def __init__(self, paths: dict[str, Path], threads: int = 2):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        providers = ["CPUExecutionProvider"]

        def load(name):
            return ort.InferenceSession(str(paths[name]), opts, providers=providers)

        self.vision = load("vision_encoder.onnx")
        self.embed = load("embed_tokens.onnx")
        self.encoder = load("encoder_model.onnx")
        self.decoder = load("decoder_model_merged.onnx")
        self.out_names = [o.name for o in self.decoder.get_outputs()]
        vocab = json.loads(Path(paths["vocab.json"]).read_text(encoding="utf-8"))
        self.pieces = {i: t for t, i in vocab.items()}
        self.bytes = _byte_decoder()
        self._lock = threading.Lock()

    def _embed(self, ids):
        import numpy as np

        return self.embed.run(None, {"input_ids": np.array([ids], dtype=np.int64)})[0]

    def decode(self, ids: list[int]) -> str:
        text = "".join(self.pieces.get(i, "") for i in ids if i > UNK)
        return bytes(self.bytes[c] for c in text if c in self.bytes).decode("utf-8", "replace")

    def caption(self, img) -> str | None:
        import numpy as np

        with self._lock:
            feats = self.vision.run(None, {"pixel_values": preprocess(img)})[0]
            inputs = np.concatenate([feats, self._embed(PROMPT)], axis=1)
            mask = np.ones(inputs.shape[:2], dtype=np.int64)
            hidden = self.encoder.run(None, {"inputs_embeds": inputs, "attention_mask": mask})[0]
            ids = [DECODER_START]
            past = None
            empty = np.zeros((1, HEADS, 0, HEAD_DIM), np.float32)
            for step in range(MAX_PIECES + 1):
                feed = {"inputs_embeds": self._embed([ids[-1]]), "encoder_hidden_states": hidden,
                        "encoder_attention_mask": mask}
                if past is None:
                    for layer in range(LAYERS):
                        for part in ("decoder", "encoder"):
                            for kv in ("key", "value"):
                                feed[f"past_key_values.{layer}.{part}.{kv}"] = empty
                    feed["use_cache_branch"] = np.array([False])
                else:
                    feed.update(past)
                    feed["use_cache_branch"] = np.array([True])
                outs = self.decoder.run(None, feed)
                fresh = {}
                for name, value in zip(self.out_names[1:], outs[1:]):
                    key = name.replace("present", "past_key_values")
                    # The encoder's part is worked out on the first step only.
                    fresh[key] = past[key] if past is not None and ".encoder." in name else value
                past = fresh
                if step == 0:
                    nxt = BOS                     # the model always starts its answer with <s>
                else:
                    logits = outs[0][0, -1].astype(np.float32)
                    for banned in no_repeat(ids):
                        logits[banned] = -np.inf
                    nxt = int(logits.argmax())
                ids.append(nxt)
                if nxt == EOS:
                    break
        return tidy(self.decode(ids))


class CaptionJob:
    """Describes photos (and videos, from their thumbnail), a few at a time, after everything else."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.engine: CaptionEngine | None = None
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "done": 0, "message": None}
        self._load_lock = threading.Lock()

    @property
    def folder(self) -> Path:
        return self.cfg.model_dir / "florence2"

    def pending(self, conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM assets WHERE caption_done = 0 AND has_thumb = 1"
                            " AND trashed_at IS NULL").fetchone()[0]

    def load_engine(self) -> CaptionEngine:
        with self._load_lock:
            if self.engine is None:
                if downloads.missing(self.folder, FILES):
                    self.state["message"] = f"Downloading the photo-description model (about {DOWNLOAD_MB} MB)…"
                paths = downloads.ensure_files(self.folder, FILES)
                self.engine = CaptionEngine(paths, self.cfg.search_threads)
                self.state["message"] = None
            return self.engine

    def unload(self) -> None:
        """Free the model's memory (about 400 MB) when describing is switched off."""
        with self._load_lock:
            self.engine = None

    def _picture(self, asset_id: int, kind: str, path: str, edit_v):
        """The photo itself at up to PICTURE_SIDE pixels (a thumbnail loses the detail the model needs),
        or the thumbnail for videos, edited photos and files that can't be read."""
        from PIL import Image

        from . import libraries, media

        if kind == "photo" and edit_v is None:
            try:
                full = libraries.full_path(self.cfg, path)
                if full is not None and full.is_file():
                    return media.load_oriented(full, PICTURE_SIDE)
            except Exception as exc:
                log.info("using the thumbnail of asset %s: %s", asset_id, exc)
        with Image.open(self.cfg.thumb_path(asset_id)) as img:
            return img.convert("RGB")

    def run_batch(self, conn, now: float, should_stop, batch: int = 4) -> bool:
        if now < self.retry_at or not self.cfg.captions:
            return False
        rows = conn.execute("SELECT id, sha1, edit_v, kind, path FROM assets WHERE caption_done = 0 AND has_thumb = 1"
                            " AND trashed_at IS NULL ORDER BY taken_at DESC LIMIT ?", (batch,)).fetchall()
        if not rows:
            self.state.update(running=False, pending=0)
            return False
        try:
            engine = self.load_engine()
        except Exception as exc:
            log.warning("photo-description model unavailable: %s", exc)
            self.state.update(running=False, message="Could not download the photo-description model. "
                                                      "Will try again in an hour.")
            self.retry_at = now + 3600
            return False
        self.state["running"] = True
        for asset_id, sha1, edit_v, kind, path in rows:
            if should_stop():
                break
            text = None
            try:
                img = self._picture(asset_id, kind, path, edit_v)
                try:
                    text = engine.caption(img)
                finally:
                    img.close()
            except Exception as exc:
                log.info("no description for asset %s: %s", asset_id, exc)
            # Unless the picture changed (or was edited) meanwhile: then it is described again.
            n = conn.execute("UPDATE assets SET caption = ?, caption_done = 1 WHERE id = ? AND sha1 = ?"
                             " AND edit_v IS ? AND caption_done = 0", (text, asset_id, sha1, edit_v)).rowcount
            conn.commit()
            self.state["done"] += n
        self.state["pending"] = self.pending(conn)
        return True
