"""GTA IV drawables: .wdr (rmcDrawable), .wdd (drawable dictionary), .wft (fragment).

Produces plain-python/numpy mesh data so it can be used inside or outside Blender.
"""

import struct

import numpy as np

from .rsc import Resource
from .texture import parse_txd

# grcFvf element data types -> byte size
_TYPE_SIZE = {0: 2, 1: 4, 2: 6, 3: 8, 4: 4, 5: 8, 6: 12, 7: 16, 8: 4, 9: 4, 10: 4}
# usage bit indices
U_POS, U_WEIGHT, U_BINDEX, U_NORMAL, U_COLOR, U_SPEC, U_UV0 = 0, 1, 2, 3, 4, 5, 6

H_DIFFUSE = 0x2B5170FD   # texturesampler
H_BUMP = 0x46B7C64F      # bumpsampler
H_SPEC = 0x608799C6      # specsampler
H_DIFFUSE2 = 0x0DF8D962  # diffuse2sampler


def _clean_tex_name(name):
    name = name.lower().replace("\\", "/").rsplit("/", 1)[-1]
    if name.startswith("pack:"):
        name = name[5:]
    if name.endswith(".dds"):
        name = name[:-4]
    return name


class Material:
    __slots__ = ("shader", "diffuse", "bump", "spec")

    def __init__(self, shader, diffuse=None, bump=None, spec=None):
        self.shader = shader
        self.diffuse = diffuse
        self.bump = bump
        self.spec = spec


class Mesh:
    """Merged triangle mesh for one drawable (all geometries of the top LOD)."""

    def __init__(self):
        self.positions = []
        self.normals = []
        self.uvs = []
        self.colors = []
        self.faces = []        # list of (N,3) int arrays, already offset
        self.face_mats = []    # list of (N,) material index arrays
        self.materials = []
        self.textures = {}     # embedded textures: name -> Texture
        self._vbase = 0

    def finish(self):
        if not self.positions:
            return False
        self.positions = np.concatenate(self.positions)
        self.normals = np.concatenate(self.normals)
        self.uvs = np.concatenate(self.uvs)
        self.colors = np.concatenate(self.colors)
        self.faces = np.concatenate(self.faces)
        self.face_mats = np.concatenate(self.face_mats)
        return True


def _decode_decl(res, decl_off, stride):
    flags = res.u32(decl_off)
    types = struct.unpack_from("<Q", res.sys, decl_off + 8)[0]
    layout = {}
    off = 0
    for i in range(16):
        if flags & (1 << i):
            t = (types >> (4 * i)) & 0xF
            layout[i] = (off, t)
            off += _TYPE_SIZE.get(t, 4)
    if off != stride:
        return None
    return layout


def _read_elem(raw, count, stride, off, t, n_want):
    """Return float32 array (count, n_want) for one vertex element."""
    if t in (4, 5, 6, 7):
        n = t - 3
        a = np.ndarray((count, n), dtype="<f4", buffer=raw, offset=off, strides=(stride, 4))
    elif t in (0, 1, 2, 3):
        n = t + 1
        a = np.ndarray((count, n), dtype="<f2", buffer=raw, offset=off, strides=(stride, 2)).astype(np.float32)
    elif t in (8, 9):
        a = np.ndarray((count, 4), dtype="u1", buffer=raw, offset=off, strides=(stride, 1)).astype(np.float32) / 255.0
        if t == 9:  # D3DCOLOR is BGRA in memory
            a = a[:, [2, 1, 0, 3]]
        n = 4
    elif t == 10:  # dec3n
        v = np.ndarray((count,), dtype="<u4", buffer=raw, offset=off, strides=(stride,)).astype(np.int64)
        comps = []
        for sh in (0, 10, 20):
            c = (v >> sh) & 0x3FF
            c = np.where(c >= 512, c - 1024, c)
            comps.append(c / 511.0)
        a = np.stack(comps, axis=1).astype(np.float32)
        n = 3
    else:
        return None
    a = np.asarray(a, dtype=np.float32)
    if n >= n_want:
        return np.ascontiguousarray(a[:, :n_want])
    pad = np.zeros((count, n_want - n), dtype=np.float32)
    return np.concatenate([a, pad], axis=1)


def _read_shader(res, s):
    vals = res.ptr(s + 0x14)
    count = res.u32(s + 0x1C)
    types = res.ptr(s + 0x24)
    hashes = res.ptr(s + 0x34)
    name_p = res.ptr(s + 0x44)
    shader_name = res.cstr(name_p) if name_p is not None else "gta_default"
    mat = Material(shader_name)
    if vals is None or types is None or hashes is None or count > 256:
        return mat
    for i in range(count):
        if res.u8(types + i) != 0:
            continue
        tp = res.ptr(vals + 4 * i)
        if tp is None:
            continue
        np_ = res.ptr(tp + 0x14)
        if np_ is None:
            continue
        tex = _clean_tex_name(res.cstr(np_))
        h = res.u32(hashes + 4 * i)
        if h == H_DIFFUSE:
            mat.diffuse = tex
        elif h == H_BUMP:
            mat.bump = tex
        elif h == H_SPEC:
            mat.spec = tex
        elif h == H_DIFFUSE2 and mat.diffuse is None:
            mat.diffuse = tex
    return mat


def _read_geometry(res, g, mesh, mat_index):
    vb = res.ptr(g + 0x0C)
    ib = res.ptr(g + 0x1C)
    if vb is None or ib is None:
        return
    if res.u16(g + 0x36) != 3:  # triangle list only
        return
    vcount = res.u16(vb + 4)
    stride = res.u32(vb + 0x0C)
    vdata = res.gptr(vb + 8)
    if vdata is None:  # some resources only fill the second data pointer
        vdata = res.gptr(vb + 0x18)
    decl = res.ptr(vb + 0x10)
    if vdata is None or decl is None or vcount == 0:
        return
    layout = _decode_decl(res, decl, stride)
    if layout is None or U_POS not in layout:
        return
    raw = res.gfx[vdata:vdata + vcount * stride]
    if len(raw) < vcount * stride:
        return

    pos = _read_elem(raw, vcount, stride, *layout[U_POS], 3)
    nrm = _read_elem(raw, vcount, stride, *layout[U_NORMAL], 3) if U_NORMAL in layout else np.zeros((vcount, 3), np.float32)
    col = _read_elem(raw, vcount, stride, *layout[U_COLOR], 4) if U_COLOR in layout else np.ones((vcount, 4), np.float32)
    uv = _read_elem(raw, vcount, stride, *layout[U_UV0], 2) if U_UV0 in layout else np.zeros((vcount, 2), np.float32)
    if pos is None:
        return
    if nrm is None:
        nrm = np.zeros((vcount, 3), np.float32)
    if col is None:
        col = np.ones((vcount, 4), np.float32)
    if uv is None:
        uv = np.zeros((vcount, 2), np.float32)

    icount = res.u32(ib + 4)
    idata = res.gptr(ib + 8)
    if idata is None:
        return
    idx = np.frombuffer(res.gfx, dtype="<u2", count=icount, offset=idata).astype(np.int32)
    idx = idx[: (len(idx) // 3) * 3].reshape(-1, 3)
    if len(idx) == 0:
        return
    if idx.max() >= vcount:
        idx = idx[(idx < vcount).all(axis=1)]
    # drop degenerate triangles
    idx = idx[(idx[:, 0] != idx[:, 1]) & (idx[:, 1] != idx[:, 2]) & (idx[:, 0] != idx[:, 2])]

    mesh.positions.append(pos)
    mesh.normals.append(nrm)
    mesh.colors.append(col)
    mesh.uvs.append(uv)
    mesh.faces.append(idx + mesh._vbase)
    mesh.face_mats.append(np.full(len(idx), mat_index, np.int32))
    mesh._vbase += vcount


def _read_materials(res, d, mesh):
    sg = res.ptr(d + 0x08)
    shader_mats = []
    if sg is not None:
        for s in res.ptr_array(sg + 0x08):
            shader_mats.append(_read_shader(res, s) if s is not None else Material("gta_default"))
        txd = res.ptr(sg + 0x04)
        if txd is not None:
            try:
                mesh.textures = parse_txd(res, txd)
            except Exception:
                mesh.textures = {}
    mesh.materials = shader_mats or [Material("gta_default")]


def _read_models(res, d, mesh, transform=None):
    lod = None
    for i in range(4):
        lod = res.ptr(d + 0x40 + 4 * i)
        if lod is not None:
            break
    if lod is None:
        return
    start = len(mesh.positions)
    for m in res.ptr_array(lod):
        if m is None:
            continue
        geoms = res.ptr_array(m + 0x04)
        mapping = res.ptr(m + 0x10)
        for gi, g in enumerate(geoms):
            if g is None:
                continue
            si = res.u16(mapping + 2 * gi) if mapping is not None else 0
            if si >= len(mesh.materials):
                si = 0
            _read_geometry(res, g, mesh, si)
    if transform is not None:
        rot, trans = transform[:3, :3], transform[:3, 3]
        for k in range(start, len(mesh.positions)):
            mesh.positions[k] = (mesh.positions[k] @ rot.T + trans).astype(np.float32)
            mesh.normals[k] = (mesh.normals[k] @ rot.T).astype(np.float32)


def read_drawable(res, d):
    """Parse an rmcDrawable at system offset `d` into a Mesh (highest LOD)."""
    mesh = Mesh()
    _read_materials(res, d, mesh)
    _read_models(res, d, mesh)
    return mesh if mesh.finish() else None


def _quat_matrix(x, y, z, w):
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _bone_world_matrices(res, skel):
    """crSkeletonData -> list of 4x4 model-space bone matrices."""
    bones = res.ptr(skel)
    count = res.u16(skel + 0x14)
    if bones is None or count == 0 or count > 512:
        return []
    by_off, out = {}, []
    for i in range(count):
        b = bones + i * 0xE0
        local = np.eye(4)
        local[:3, :3] = _quat_matrix(*res.f32(b + 0x40, 4))
        local[:3, 3] = res.f32(b + 0x20, 3)
        parent = res.ptr(b + 0x10)
        world = by_off[parent] @ local if parent in by_off else local
        by_off[b] = world
        out.append(world)
    return out

def load_wdr(data):
    res = Resource(data)
    return read_drawable(res, 0)


def load_wdd(data):
    """Returns {hash: Mesh} for a drawable dictionary."""
    res = Resource(data)
    hp, count = res.collection(0x10)
    ptrs = res.ptr_array(0x18)
    out = {}
    for i, p in enumerate(ptrs):
        if p is None or hp is None:
            continue
        h = res.u32(hp + 4 * i)
        try:
            m = read_drawable(res, p)
        except Exception:
            m = None
        if m is not None:
            out[h] = m
    return out


def load_wft(data):
    """Fragment type: shaders/skeleton live on the main fragDrawable (fragType+0xB4);
    geometry lives on child drawables, each placed by its bone."""
    res = Resource(data)
    main = res.ptr(0xB4)
    if main is None or res.ptr(main + 0x08) is None:
        return None
    mesh = Mesh()
    _read_materials(res, main, mesh)
    _read_models(res, main, mesh)
    skel = res.ptr(main + 0x0C)
    bones = _bone_world_matrices(res, skel) if skel is not None else []
    # fragTypeChild: vtable 0x00, bone index u16 at 0x0E, drawable ptr at 0x90
    seen = set()
    for off in range(0x90, len(res.sys) - 4, 4):
        d = res.ptr(off)
        if d is None or d in seen or d == main or d + 0x50 > len(res.sys):
            continue
        child = off - 0x90
        if res.u32(child) == 0 or res.ptr(d + 0x40) is None or res.ptr(d + 0x0C) != skel:
            continue
        seen.add(d)
        bone = res.u16(child + 0x0E)
        try:
            _read_models(res, d, mesh, bones[bone] if bone < len(bones) else None)
        except Exception:
            continue
    return mesh if mesh.finish() else None
