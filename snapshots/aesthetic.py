"""How good a photo looks: the LAION-Aesthetics predictor V1 for CLIP ViT-B/32.

A linear model (512 weights and a bias) on top of the photo's content vector, trained by LAION on
photos people rated from 1 to 10 (the AVA and Simulacra sets, Schuhmann et al., 2022; MIT license,
https://github.com/LAION-AI/aesthetic-predictor, file sa_0_4_vit_b_32_linear.pth at commit 6d122ad,
sha256 c7b14cead230694acc7b9447974d3cad78003c72da032e402a303b6c2429e85f). The content vectors come from
the same model (OpenAI's CLIP ViT-B/32, see clipsearch.py), so the weights apply as they are. They are
small enough to keep here, so nothing is downloaded.

The score is roughly the 1-10 rating people would give; only the order between photos is used (covers,
memories, the year in review, Highlights). It replaces comparing each photo with "a beautiful photo" and
"a blurry photo" descriptions, which agreed with people only roughly.
"""

# The weights below are from LAION's aesthetic-predictor, under this license:
#
# MIT License
#
# Copyright (c) 2022 LAION AI
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import base64
from functools import lru_cache

_WEIGHTS = (
    "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"
    "Rna/QPUUPbXGsD8irfa/TD4ZP0tXHkBh+VpA"
)


@lru_cache(maxsize=1)
def _model():
    import numpy as np

    raw = base64.b64decode("".join(_WEIGHTS))
    w = np.frombuffer(raw[:2048], dtype="<f4").astype(np.float32)
    b = float(np.frombuffer(raw[2048:2052], dtype="<f4")[0])
    return w, b


def score(vec) -> float:
    """The predicted rating (about 1-10) of a photo from its content vector (any length: it is normalised)."""
    import numpy as np

    w, b = _model()
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return float(v @ w / n + b) if n else b
