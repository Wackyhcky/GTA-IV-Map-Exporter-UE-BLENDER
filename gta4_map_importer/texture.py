"""GTA IV texture dictionaries (.wtd / embedded) -> DDS or PNG files."""

import struct
import zlib

from .rsc import Resource

D3DFMT_A8R8G8B8 = 21
D3DFMT_X8R8G8B8 = 22
D3DFMT_A8 = 28
D3DFMT_L8 = 50
FOURCC = {0x31545844: b"DXT1", 0x33545844: b"DXT3", 0x35545844: b"DXT5"}


class Texture:
    __slots__ = ("name", "width", "height", "fmt", "levels", "data")

    def __init__(self, name, width, height, fmt, levels, data):
        self.name = name
        self.width = width
        self.height = height
        self.fmt = fmt
        self.levels = levels
        self.data = data

    def _level_size(self, w, h):
        if self.fmt == 0x31545844:
            return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 8
        if self.fmt in FOURCC:
            return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 16
        if self.fmt in (D3DFMT_L8, D3DFMT_A8):
            return w * h
        return w * h * 4

    def total_size(self):
        size, w, h = 0, self.width, self.height
        for _ in range(max(1, self.levels)):
            size += self._level_size(w, h)
            w, h = max(1, w // 2), max(1, h // 2)
        return size

    def to_dds(self):
        flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000  # caps|height|width|pixelformat|mipcount
        caps = 0x1000 | (0x400008 if self.levels > 1 else 0)
        if self.fmt in FOURCC:
            pf = struct.pack("<II4sIIIII", 32, 0x4, FOURCC[self.fmt], 0, 0, 0, 0, 0)
            flags |= 0x80000
            pitch = self._level_size(self.width, self.height)
        elif self.fmt in (D3DFMT_L8,):
            pf = struct.pack("<II4sIIIII", 32, 0x20000, b"\0\0\0\0", 8, 0xFF, 0, 0, 0)
            flags |= 0x8
            pitch = self.width
        elif self.fmt == D3DFMT_A8:
            pf = struct.pack("<II4sIIIII", 32, 0x2, b"\0\0\0\0", 8, 0, 0, 0, 0xFF)
            flags |= 0x8
            pitch = self.width
        else:
            alpha = 0xFF000000 if self.fmt != D3DFMT_X8R8G8B8 else 0
            pf = struct.pack("<II4sIIIII", 32, 0x40 | (0x1 if alpha else 0), b"\0\0\0\0", 32,
                             0x00FF0000, 0x0000FF00, 0x000000FF, alpha)
            flags |= 0x8
            pitch = self.width * 4
        hdr = struct.pack("<4sIIIIIII44x", b"DDS ", 124, flags, self.height, self.width,
                          pitch, 0, max(1, self.levels))
        hdr += pf + struct.pack("<IIIII", caps, 0, 0, 0, 0)
        return hdr + self.data

    def to_rgba(self):
        """Decode the top mip level to an (h, w, 4) uint8 array (needs numpy)."""
        import numpy as np
        w, h = self.width, self.height
        if self.fmt in FOURCC:
            return _decode_bc(np, self.data, w, h, FOURCC[self.fmt])
        if self.fmt in (D3DFMT_L8, D3DFMT_A8):
            a = np.frombuffer(self.data, np.uint8, w * h).reshape(h, w)
            out = np.empty((h, w, 4), np.uint8)
            if self.fmt == D3DFMT_L8:
                out[..., :3] = a[..., None]
                out[..., 3] = 255
            else:
                out[..., :3] = 255
                out[..., 3] = a
            return out
        bgra = np.frombuffer(self.data, np.uint8, w * h * 4).reshape(h, w, 4)
        out = bgra[..., [2, 1, 0, 3]].copy()
        if self.fmt == D3DFMT_X8R8G8B8:
            out[..., 3] = 255
        return out

    def to_png(self, level=3):
        """PNG bytes of the top mip level (RGB, or RGBA when there is alpha)."""
        rgba = self.to_rgba()
        h, w = rgba.shape[:2]
        has_alpha = bool((rgba[..., 3] < 255).any())
        px = rgba if has_alpha else rgba[..., :3]
        rows = bytearray()
        for y in range(h):
            rows.append(0)  # filter: none
            rows += px[y].tobytes()

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 6 if has_alpha else 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(rows), level)) + chunk(b"IEND", b""))


def _decode_bc(np, data, w, h, fourcc):
    """Vectorised DXT1/DXT3/DXT5 decoder -> (h, w, 4) uint8."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    bsize = 8 if fourcc == b"DXT1" else 16
    nblk = bw * bh
    avail = min(len(data), nblk * bsize) // bsize
    blocks = np.zeros((nblk, bsize), np.uint8)
    blocks[:avail] = np.frombuffer(data, np.uint8, avail * bsize).reshape(-1, bsize)
    col = blocks[:, bsize - 8:]

    c0 = col[:, 0].astype(np.uint32) | (col[:, 1].astype(np.uint32) << 8)
    c1 = col[:, 2].astype(np.uint32) | (col[:, 3].astype(np.uint32) << 8)

    def rgb565(c):
        r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
        return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], axis=1).astype(np.int32)

    p0, p1 = rgb565(c0), rgb565(c1)
    four = (c0 > c1) | (fourcc != b"DXT1")
    p2 = np.where(four[:, None], (2 * p0 + p1) // 3, (p0 + p1) // 2)
    p3 = np.where(four[:, None], (p0 + 2 * p1) // 3, 0)
    palette = np.stack([p0, p1, p2, p3], axis=1)                       # (n, 4, 3)
    pal_a = np.full((nblk, 4), 255, np.uint8)
    if fourcc == b"DXT1":
        pal_a[~four, 3] = 0
    bits = (col[:, 4].astype(np.uint32) | (col[:, 5].astype(np.uint32) << 8)
            | (col[:, 6].astype(np.uint32) << 16) | (col[:, 7].astype(np.uint32) << 24))
    idx = ((bits[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3).astype(np.int64)  # (n, 16)
    rgb = np.take_along_axis(palette, idx[:, :, None], axis=1)
    alpha = np.take_along_axis(pal_a, idx, axis=1)

    if fourcc == b"DXT3":
        ab = blocks[:, :8]
        alpha = (np.stack([ab & 15, ab >> 4], axis=2).reshape(nblk, 16) * 17).astype(np.uint8)
    elif fourcc == b"DXT5":
        a0 = blocks[:, 0].astype(np.int32)[:, None]
        a1 = blocks[:, 1].astype(np.int32)[:, None]
        i = np.arange(1, 7)
        pal8 = np.concatenate([a0, a1, ((7 - i) * a0 + i * a1) // 7], axis=1)
        j = np.arange(1, 5)
        pal6 = np.concatenate([a0, a1, ((5 - j) * a0 + j * a1) // 5,
                               np.zeros_like(a0), np.full_like(a0, 255)], axis=1)
        apal = np.where(a0 > a1, pal8, pal6)
        abits = np.zeros(nblk, np.uint64)
        for k in range(6):
            abits |= blocks[:, 2 + k].astype(np.uint64) << np.uint64(8 * k)
        aidx = (abits[:, None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))) & np.uint64(7)
        alpha = np.take_along_axis(apal, aidx.astype(np.int64), axis=1).astype(np.uint8)

    px = np.concatenate([rgb.astype(np.uint8), alpha[:, :, None]], axis=2)  # (n, 16, 4)
    img = px.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    return np.ascontiguousarray(img[:h, :w])


def parse_txd(res, off=0):
    """Parse a pgDictionary<grcTexturePC> at system offset `off`."""
    out = {}
    for t in res.ptr_array(off + 0x18):
        if t is None:
            continue
        np_ = res.ptr(t + 0x14)
        if np_ is None:
            continue
        name = res.cstr(np_).lower().replace("\\", "/").rsplit("/", 1)[-1]
        if name.startswith("pack:"):
            name = name[5:]
        if name.endswith(".dds"):
            name = name[:-4]
        w, h, fmt = struct.unpack_from("<HHI", res.sys, t + 0x1C)
        levels = res.u8(t + 0x27)
        dp = res.gptr(t + 0x48)
        if dp is None or w == 0 or h == 0:
            continue
        tex = Texture(name, w, h, fmt, levels, b"")
        tex.data = res.gfx[dp:dp + tex.total_size()]
        out[name] = tex
    return out


def load_wtd(data):
    return parse_txd(Resource(data), 0)
