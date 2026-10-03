"""Game-level data: gta.dat, images.txt, IDE definitions and WPL placements."""

import os
import re
import struct

from . import aes, drawable, texture
from .img import ImgArchive

WPL_HEADER = 68
WPL_INST = 48


def joaat(s):
    h = 0
    for c in s.lower().encode("latin-1"):
        h = (h + c) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    return (h + (h << 15)) & 0xFFFFFFFF


class ModelDef:
    __slots__ = ("name", "txd", "draw_dist", "flags", "wdd", "source")

    def __init__(self, name, txd, draw_dist, flags, wdd, source):
        self.name = name
        self.txd = txd
        self.draw_dist = draw_dist
        self.flags = flags
        self.wdd = wdd
        self.source = source


class Instance:
    __slots__ = ("hash", "pos", "rot", "lod", "wpl", "index", "is_lod")

    def __init__(self, hash_, pos, rot, lod, wpl, index):
        self.hash = hash_
        self.pos = pos
        self.rot = rot      # (x, y, z, w) as stored in the WPL
        self.lod = lod
        self.wpl = wpl
        self.index = index
        self.is_lod = False


def parse_wpl(data, wpl_name):
    count = struct.unpack_from("<I", data, 4)[0]
    out = []
    for i in range(count):
        off = WPL_HEADER + i * WPL_INST
        if off + WPL_INST > len(data):
            break
        px, py, pz, rx, ry, rz, rw, h, _flags, lod = struct.unpack_from("<7fIIi", data, off)
        out.append(Instance(h, (px, py, pz), (rx, ry, rz, rw), lod, wpl_name, i))
    return out


def area_of(wpl_name):
    """manhat01_stream12.wpl -> manhat01"""
    base = wpl_name.lower().rsplit("/", 1)[-1]
    if base.endswith(".wpl"):
        base = base[:-4]
    return re.sub(r"_(stream|strbig)\d+$", "", base)


class GameData:
    def __init__(self, game_dir, log=print, key_cache=None):
        self.root = game_dir
        self.log = log
        exe = None
        for n in ("GTAIV.exe", "EFLC.exe"):
            if os.path.isfile(os.path.join(game_dir, n)):
                exe = os.path.join(game_dir, n)
                break
        if exe is None:
            raise RuntimeError("GTAIV.exe not found in %s" % game_dir)
        self.key = aes.find_key(exe, key_cache)

        self.models = {}       # hash -> ModelDef
        self.txd_parent = {}   # txd -> parent txd
        self.archives = []     # (area, ImgArchive)
        self.index = {}        # entry name -> ImgArchive
        self.ide_paths = []
        self.ipl_paths = []
        self.img_paths = []
        self._read_gta_dat()
        self._read_images_txt()

    # ---------------------------------------------------------------- paths
    def resolve(self, path):
        path = path.strip().replace("\\", "/")
        if ":" in path:
            dev, rest = path.split(":", 1)
            rest = rest.lstrip("/")
            dev = dev.lower()
            if dev in ("platform", "platformimg"):
                rel = "pc/" + rest
            else:
                rel = "common/" + rest
        else:
            rel = path
        for base in (os.path.join(self.root, "update"), self.root):
            p = os.path.join(base, rel)
            if os.path.exists(p):
                return p
        return os.path.join(self.root, rel)

    def _read_gta_dat(self):
        for line in open(self.resolve("common:/data/gta.dat"), encoding="latin-1"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            kind = parts[0].upper()
            if kind == "IDE" and len(parts) > 1:
                self.ide_paths.append(self.resolve(parts[1]))
            elif kind == "IPL" and len(parts) > 1 and "/maps/" in parts[1].lower():
                p = self.resolve(parts[1][:-4] + ".wpl" if parts[1].lower().endswith(".ipl") else parts[1])
                if os.path.isfile(p):
                    self.ipl_paths.append(p)

    def _read_images_txt(self):
        for line in open(self.resolve("common:/data/images.txt"), encoding="latin-1"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split()[0]
            low = p.lower()
            if "/data/maps/" in low or low.endswith("/gtxd"):
                f = self.resolve(p + ".img")
                if os.path.isfile(f):
                    self.img_paths.append(f)

    # ---------------------------------------------------------------- loading
    def load_definitions(self):
        for p in self.ide_paths:
            if os.path.isfile(p):
                self._parse_ide(p)
        self.log("Loaded %d model definitions" % len(self.models))

    def _parse_ide(self, path):
        section = None
        src = os.path.splitext(os.path.basename(path))[0].lower()
        for line in open(path, encoding="latin-1"):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if "," not in line:
                tok = line.lower()
                section = None if tok == "end" else tok
                continue
            f = [x.strip() for x in line.split(",")]
            if section in ("objs", "tobj") and len(f) >= 4:
                self._add_model(f[0], f[1], f[2], f[3], f[15] if len(f) > 15 else "null", src)
            elif section in ("anim", "tanm") and len(f) >= 5:
                self._add_model(f[0], f[1], f[3], f[4], f[16] if len(f) > 16 else "null", src)
            elif section == "txdp" and len(f) >= 2:
                self.txd_parent[f[0].lower()] = f[1].lower()

    def _add_model(self, name, txd, dist, flags, wdd, src):
        try:
            d = float(dist)
        except ValueError:
            d = 0.0
        try:
            fl = int(float(flags))
        except ValueError:
            fl = 0
        wdd = wdd.lower()
        self.models[joaat(name)] = ModelDef(name.lower(), txd.lower(), d, fl,
                                            None if wdd == "null" else wdd, src)

    def open_archives(self):
        for p in self.img_paths:
            try:
                a = ImgArchive(p, self.key)
            except Exception as e:  # pragma: no cover
                self.log("Skipping %s: %s" % (p, e))
                continue
            area = os.path.splitext(os.path.basename(p))[0].lower()
            self.archives.append((area, a))
            for n in a.names():
                self.index.setdefault(n, a)
        self.log("Opened %d IMG archives (%d files)" % (len(self.archives), len(self.index)))

    def load_instances(self):
        """Returns {area: [Instance]} with is_lod set on LOD/SLOD placements."""
        by_wpl = {}
        for p in self.ipl_paths:
            name = os.path.basename(p).lower()
            by_wpl[name] = parse_wpl(open(p, "rb").read(), name)
        for _area, a in self.archives:
            for n in a.names():
                if n.endswith(".wpl"):
                    by_wpl[n] = parse_wpl(a.read(n), n)
        # streamed WPLs reference LOD parents inside the area's main WPL;
        # main WPL entries reference their own SLOD parents.
        for wname, insts in by_wpl.items():
            parent = by_wpl.get(area_of(wname) + ".wpl")
            if parent is None:
                continue
            for inst in insts:
                if 0 <= inst.lod < len(parent):
                    target = parent[inst.lod]
                    if target is not inst:
                        target.is_lod = True
        areas = {}
        for wname, insts in by_wpl.items():
            areas.setdefault(area_of(wname), []).extend(insts)
        for insts in areas.values():
            for inst in insts:
                m = self.models.get(inst.hash)
                if m is not None and (m.name.startswith("lod") or m.name.startswith("slod")):
                    inst.is_lod = True
        total = sum(len(v) for v in areas.values())
        self.log("Loaded %d placements in %d areas" % (total, len(areas)))
        return areas

    # ---------------------------------------------------------------- assets
    def read_file(self, name):
        a = self.index.get(name.lower())
        return a.read(name) if a is not None else None

    def load_model(self, mdef, wdd_cache):
        """Returns a drawable.Mesh for the given model definition, or None."""
        data = self.read_file(mdef.name + ".wdr")
        if data is not None:
            return drawable.load_wdr(data)
        data = self.read_file(mdef.name + ".wft")
        if data is not None:
            return drawable.load_wft(data)
        if mdef.wdd:
            if mdef.wdd not in wdd_cache:
                d = self.read_file(mdef.wdd + ".wdd")
                wdd_cache[mdef.wdd] = drawable.load_wdd(d) if d is not None else {}
            return wdd_cache[mdef.wdd].get(joaat(mdef.name))
        return None

    def load_txd(self, name):
        data = self.read_file(name + ".wtd")
        if data is None:
            return {}
        try:
            return texture.load_wtd(data)
        except Exception:
            return {}

    def txd_chain(self, txd):
        chain, seen = [], set()
        while txd and txd != "null" and txd not in seen:
            seen.add(txd)
            chain.append(txd)
            txd = self.txd_parent.get(txd)
        return chain
