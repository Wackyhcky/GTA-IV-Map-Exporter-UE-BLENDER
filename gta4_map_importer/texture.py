"""GTA IV texture dictionaries (.wtd / embedded) -> DDS files."""

import struct

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
