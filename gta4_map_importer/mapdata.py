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
    __slots__ = ("hash", "pos", "rot", "lod", "wpl", "index", "is_lod", "interior")

    def __init__(self, hash_, pos, rot, lod, wpl, index):
        self.hash = hash_
        self.pos = pos
        self.rot = rot      # (x, y, z, w) as stored in the WPL
        self.lod = lod
        self.wpl = wpl
        self.index = index
        self.is_lod = False
        self.interior = None  # interior (MLO) name for objects that belong to one


def _q_true(r):
    """Stored (x, y, z, w) -> actual rotation (w, x, y, z). WPL and MLO entries store the inverse."""
    x, y, z, w = r
    return (w, -x, -y, -z)


def _q_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw)


def _q_rotate(q, v):
    w, x, y, z = q
    p = _q_mul(_q_mul(q, (0.0, v[0], v[1], v[2])), (w, -x, -y, -z))
    return p[1], p[2], p[3]


class MloDef:
    """An interior: objects placed relative to the interior's origin."""
    __slots__ = ("name", "entities")

    def __init__(self, name):
        self.name = name
        self.entities = []  # (hash, (x, y, z), stored rot (x, y, z, w))


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
        self.mlos = {}         # hash -> MloDef (interiors)
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
        mlo, mlo_left, mlo_skip = None, 0, False
        for line in open(path, encoding="latin-1"):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if "," not in line:
                tok = line.lower()
                if section == "mlo" and tok.startswith(("mloroom", "roomend", "mloportal")):
                    mlo_skip = True  # rooms and portals: only needed for culling
                    continue
                if section == "mlo" and tok == "mloend":
                    mlo, mlo_left, mlo_skip = None, 0, False
                    continue
                section = None if tok == "end" else tok
                mlo, mlo_left, mlo_skip = None, 0, False
                continue
            f = [x.strip() for x in line.split(",")]
            if section == "mlo":
                if mlo_skip:
                    continue
                if mlo is None:
                    # header: name, flags, rooms, portals, entities, ...
                    if len(f) >= 5:
                        mlo = MloDef(f[0].lower())
                        try:
                            mlo_left = int(f[4])
                        except ValueError:
                            mlo_left = 0
                        self.mlos[joaat(f[0])] = mlo
                    continue
                if mlo_left > 0 and len(f) >= 8:
                    try:
                        vals = [float(v) for v in f[1:8]]
                    except ValueError:
                        continue
                    mlo.entities.append((joaat(f[0]), tuple(vals[0:3]), tuple(vals[3:7])))
                    mlo_left -= 1
                continue
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

    def load_instances(self, interiors=True):
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
        if interiors:
            n_int = n_obj = 0
            for area, insts in areas.items():
                extra = []
                for inst in insts:
                    mlo = self.mlos.get(inst.hash)
                    if mlo is None or inst.hash in self.models:
                        continue
                    n_int += 1
                    q = _q_true(inst.rot)
                    for h, epos, erot in mlo.entities:
                        off = _q_rotate(q, epos)
                        world = (inst.pos[0] + off[0], inst.pos[1] + off[1], inst.pos[2] + off[2])
                        w, x, y, z = _q_mul(q, _q_true(erot))
                        child = Instance(h, world, (-x, -y, -z, w), -1, inst.wpl, -1)
                        child.interior = mlo.name
                        extra.append(child)
                n_obj += len(extra)
                insts.extend(extra)
            self.log("Placed %d interiors (%d objects)" % (n_int, n_obj))
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


# ---------------------------------------------------------------- detail levels
# (key, label, min size in metres for full-detail objects)
DETAIL_LEVELS = [
    ("FULL", "Full detail (everything)", 0.0),
    ("HIGH", "High (skip tiny props under 2 m)", 2.0),
    ("MEDIUM", "Medium (skip props under 8 m)", 8.0),
    ("LOW", "Low (GTA's LOD models + large objects)", 8.0),
    ("VERYLOW", "Very low (GTA's far SLOD models + large objects)", 15.0),
]
DETAIL_KEYS = [d[0] for d in DETAIL_LEVELS]


def model_size(mesh):
    """Largest bounding-box side of a drawable.Mesh, in metres."""
    if mesh is None or not len(mesh.positions):
        return 0.0
    return float((mesh.positions.max(0) - mesh.positions.min(0)).max())


def select_detail(gd, insts, area, detail):
    """Choose placements for one area at a detail level.

    Returns [(Instance, min_size)]: the placement is kept only if its model is at
    least min_size metres across (checked once the model is loaded).

    GTA IV map tiers: full-detail objects (mostly in streamed WPLs) point to a
    low-poly LOD stand-in in the area's main WPL, and LODs point to SLOD models
    that cover whole blocks. Many objects have no stand-in; in the low levels
    the large ones are kept so nothing is missing, small props are dropped.
    """
    detail = detail if detail in DETAIL_KEYS else "FULL"
    min_hd = dict((d[0], d[2]) for d in DETAIL_LEVELS)[detail]
    insts = [i for i in insts if i.hash in gd.models]
    main_name = area + ".wpl"
    main = [i for i in insts if i.wpl == main_name]
    main.sort(key=lambda i: i.index)
    by_index = {i.index: i for i in main}

    def is_slod(i):
        return gd.models[i.hash].name.startswith("slod")

    hd = [i for i in insts if not i.is_lod]
    if detail in ("FULL", "HIGH", "MEDIUM"):
        return [(i, min_hd) for i in hd]

    out = []
    # full-detail objects with no LOD stand-in: keep the large ones
    for i in hd:
        parent = by_index.get(i.lod) if i.wpl != main_name else None
        if parent is None or not parent.is_lod:
            out.append((i, min_hd))
    lods = [i for i in insts if i.is_lod and not is_slod(i)]
    if detail == "LOW":
        return out + [(i, 0.0) for i in lods]
    # VERYLOW: an LOD's SLOD parent replaces it; LODs without one stay
    slods = {}
    for i in lods:
        parent = by_index.get(i.lod)
        if parent is not None and parent is not i and parent.is_lod and is_slod(parent):
            slods[id(parent)] = parent
        else:
            out.append((i, 0.0))
    # SLODs nothing points to (e.g. covering only full-detail objects) are skipped:
    # their area is already drawn by the objects above
    return out + [(i, 0.0) for i in slods.values()]


def split_interiors(selected):
    """{area: [(inst, min_size)]} -> interiors moved to their own '<area>_interiors' group."""
    out = {}
    for area, rows in selected.items():
        for row in rows:
            key = area + "_interiors" if row[0].interior else area
            out.setdefault(key, []).append(row)
    return out
