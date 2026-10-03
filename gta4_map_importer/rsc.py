"""RAGE RSC05 resource container (GTA IV PC)."""

import struct
import zlib

RSC5_MAGIC = 0x05435352  # "RSC\x05"


class Resource:
    """Decompressed resource split into its system (CPU) and graphics (GPU)
    segments. Pointers inside are 0x5XXXXXXX (system) / 0x6XXXXXXX (graphics)."""

    def __init__(self, data):
        magic, self.type, flags = struct.unpack_from("<III", data)
        if magic != RSC5_MAGIC:
            raise ValueError("not an RSC05 resource")
        sys_size = (flags & 0x7FF) << (((flags >> 11) & 0xF) + 8)
        gfx_size = ((flags >> 15) & 0x7FF) << (((flags >> 26) & 0xF) + 8)
        raw = zlib.decompress(data[12:])
        self.sys = raw[:sys_size]
        self.gfx = raw[sys_size:sys_size + gfx_size]

    # ---- helpers working on system-segment offsets
    def u8(self, off):
        return self.sys[off]

    def u16(self, off):
        return struct.unpack_from("<H", self.sys, off)[0]

    def u32(self, off):
        return struct.unpack_from("<I", self.sys, off)[0]

    def f32(self, off, n=1):
        v = struct.unpack_from("<%df" % n, self.sys, off)
        return v[0] if n == 1 else v

    def ptr(self, off):
        """Read a pointer field; returns a system offset, or None."""
        p = self.u32(off)
        if p >> 28 == 5:
            o = p & 0x0FFFFFFF
            return o if o < len(self.sys) else None
        return None

    def gptr(self, off):
        """Read a pointer to the graphics segment."""
        p = self.u32(off)
        if p >> 28 == 6:
            return p & 0x0FFFFFFF
        return None

    def cstr(self, off):
        end = self.sys.index(b"\0", off)
        return self.sys[off:end].decode("latin-1")

    def collection(self, off):
        """pgObjectArray / pgArray: (data ptr, u16 count, u16 capacity)."""
        p = self.ptr(off)
        count = self.u16(off + 4)
        return p, count

    def ptr_array(self, off):
        p, count = self.collection(off)
        if p is None:
            return []
        return [self.ptr(p + 4 * i) for i in range(count)]
