"""JPEG XL (.jxl) photos, read through Pillow like any other picture.

The pixels are decoded by imagecodecs (BSD licence, libjxl inside). Pillow has no JPEG XL reader of its own,
so this registers a small one: once imported, Image.open() works on .jxl files everywhere in the app.

The camera's EXIF (date, camera, GPS) sits in an "Exif" box of the JPEG XL container. libjxl already turns
the picture the right way up, so the EXIF orientation is reset to "normal" to keep it from being turned twice.
"""

import mimetypes
import struct

from PIL import Image, ImageFile

try:
    import imagecodecs

    AVAILABLE = bool(getattr(imagecodecs, "JPEGXL", None) and imagecodecs.JPEGXL.available)
except Exception:  # pragma: no cover - depends on the install
    imagecodecs = None
    AVAILABLE = False

CODESTREAM = b"\xff\x0a"                               # a bare JPEG XL picture
CONTAINER = b"\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a"    # a JPEG XL picture in boxes, with room for EXIF and XMP
_ORIENTATION = 0x0112


def is_jxl(head: bytes) -> bool:
    return head[:2] == CODESTREAM or head[:12] == CONTAINER


def _boxes(data: bytes):
    """(type, payload) for each top-level box of a JPEG XL container."""
    pos, end = 0, len(data)
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = pos + 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            body = pos + 16
        elif size == 0:
            size = end - pos
        if size < body - pos or pos + size > end:
            return
        yield kind, data[body:pos + size]
        pos += size


def exif_from_container(data: bytes) -> bytes | None:
    """The EXIF block (TIFF bytes) of a JPEG XL container, with the orientation set to normal; None if there is none."""
    if data[:12] != CONTAINER:
        return None
    for kind, payload in _boxes(data):
        if kind == b"Exif" and len(payload) > 4:
            offset = struct.unpack(">I", payload[:4])[0]       # where the TIFF header starts, after these 4 bytes
            tiff = payload[4 + offset:]
            if tiff[:4] in (b"II*\x00", b"MM\x00*"):
                return _upright(tiff)
    return None


def _upright(tiff: bytes) -> bytes:
    """The same EXIF with its orientation tag (if any) set to 1: libjxl has already turned the picture."""
    order = "<" if tiff[:2] == b"II" else ">"
    try:
        ifd = struct.unpack(order + "I", tiff[4:8])[0]
        count = struct.unpack(order + "H", tiff[ifd:ifd + 2])[0]
        out = bytearray(tiff)
        for n in range(count):
            at = ifd + 2 + 12 * n
            tag, kind = struct.unpack(order + "HH", tiff[at:at + 4])
            if tag == _ORIENTATION and kind == 3:              # a SHORT, kept in the entry itself
                out[at + 8:at + 10] = struct.pack(order + "H", 1)
        return bytes(out)
    except struct.error:
        return tiff


def _decode_pixels(data: bytes):
    """The picture as a numpy array (height, width[, channels]); the first frame of an animation."""
    try:
        arr = imagecodecs.jpegxl_decode(data, index=0)
    except TypeError:                                      # an older imagecodecs without index=
        arr = imagecodecs.jpegxl_decode(data)
    return arr


def _to_image(arr) -> Image.Image:
    import numpy as np

    if arr.ndim == 4:                                      # frames of an animation: the first one
        arr = arr[0]
    if arr.dtype != np.uint8:
        if np.issubdtype(arr.dtype, np.floating):
            arr = (np.clip(arr, 0.0, 1.0) * 255 + 0.5).astype(np.uint8)
        else:                                              # 16-bit (or more): keep the top 8 bits
            arr = (arr >> (8 * (arr.dtype.itemsize - 1))).astype(np.uint8)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]
    if not (arr.ndim == 2 or (arr.ndim == 3 and arr.shape[2] in (2, 3, 4))):
        raise ValueError(f"unexpected JPEG XL picture shape {arr.shape}")
    return Image.fromarray(np.ascontiguousarray(arr))       # L, LA, RGB or RGBA from the shape


class JxlImageFile(ImageFile.ImageFile):
    format = "JXL"
    format_description = "JPEG XL"

    def _open(self):
        data = self.fp.read()
        if not is_jxl(data[:12]):
            raise SyntaxError("not a JPEG XL file")
        if not AVAILABLE:
            raise OSError("JPEG XL needs the imagecodecs package")
        picture = _to_image(_decode_pixels(data))
        exif = exif_from_container(data)
        if exif:
            self.info["exif"] = exif
        self._picture = picture
        self._size = picture.size
        self._mode = picture.mode
        self.tile = []

    def load(self):
        picture = getattr(self, "_picture", None)
        if picture is not None:
            self.im = picture.im
            self._picture = None
        return Image.Image.load(self)


def _accept(prefix: bytes) -> bool:
    return is_jxl(prefix)


def register() -> None:
    Image.register_open(JxlImageFile.format, JxlImageFile, _accept)
    Image.register_extensions(JxlImageFile.format, [".jxl"])
    Image.register_mime(JxlImageFile.format, "image/jxl")
    mimetypes.add_type("image/jxl", ".jxl")           # so downloads of the original are labelled right
