"""GTA IV IMG (version 3) archive reader."""

import struct

from . import aes

IMG3_MAGIC = 0xA94E2A52


class ImgEntry:
    __slots__ = ("name", "offset", "size", "rsc_type", "flags")

    def __init__(self, name, offset, size, rsc_type, flags):
        self.name = name
        self.offset = offset
        self.size = size
        self.rsc_type = rsc_type
        self.flags = flags


class ImgArchive:
    def __init__(self, path, key=None):
        self.path = path
        self.entries = {}
        self._f = open(path, "rb")
        head = self._f.read(20)
        magic = struct.unpack_from("<I", head)[0]
        encrypted = magic != IMG3_MAGIC
        if encrypted:
            if key is None:
                raise RuntimeError("%s is encrypted and no key was given" % path)
            head = aes.decrypt(head[:16], key) + head[16:]
        magic, version, count, toc_size = struct.unpack_from("<4I", head)
        if magic != IMG3_MAGIC or version != 3:
            raise RuntimeError("%s is not a GTA IV IMG archive" % path)
        toc = self._f.read(toc_size)
        if encrypted:
            toc = aes.decrypt(toc, key)
        names = toc[count * 16:].split(b"\0")
        for i in range(count):
            size, rtype, block, used, flags = struct.unpack_from("<IIIHH", toc, i * 16)
            real = used * 2048 - (flags & 0x7FF)
            if rtype == 0 and size:
                real = size
            name = names[i].decode("latin-1").lower()
            self.entries[name] = ImgEntry(name, block * 2048, real, rtype, size)

    def names(self):
        return self.entries.keys()

    def read(self, name):
        e = self.entries.get(name.lower())
        if e is None:
            return None
        self._f.seek(e.offset)
        return self._f.read(e.size)

    def close(self):
        self._f.close()
