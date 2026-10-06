"""Exports the GTA IV map for Unreal Engine (no Blender needed).

Writes into <out_dir>:
    meshes/batch_NNN.gltf + .bin   many models per file, one mesh node each
    textures/<name>.png    (Unreal can't import compressed DDS)
    gta4_unreal.json               materials, models and placements
`unreal_runner.py` then imports all of it inside the editor.
"""

import base64
import fnmatch
import json
import os
import re
import struct
import time

import numpy as np

from .mapdata import GameData, model_size, select_detail, split_interiors

ALPHA_SHADERS = ("alpha", "cutout", "decal", "glass", "window", "billboard", "trees", "grass", "wire")
BATCH_MODELS = 150


def ue_name(s):
    """Unreal asset names: letters, digits and underscores only."""
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def _quat(r):
    """WPL quaternion -> rotation quaternion (w, x, y, z) in GTA space."""
    x, y, z, w = r
    return (w, -x, -y, -z)


class _GltfBatch:
    def __init__(self):
        self.bin = bytearray()
        self.accessors = []
        self.views = []
        self.meshes = []
        self.nodes = []
        self.materials = []
        self._mat_index = {}

    def _add(self, arr, target, comp, typ, minmax=False):
        data = np.ascontiguousarray(arr).tobytes()
        while len(self.bin) % 4:
            self.bin.append(0)
        self.views.append({"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data), "target": target})
        self.bin += data
        acc = {"bufferView": len(self.views) - 1, "componentType": comp, "count": len(arr), "type": typ}
        if minmax:
            acc["min"] = arr.min(axis=0).tolist()
            acc["max"] = arr.max(axis=0).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def material(self, name):
        if name not in self._mat_index:
            self._mat_index[name] = len(self.materials)
            self.materials.append({"name": name, "pbrMetallicRoughness": {"metallicFactor": 0.0}})
        return self._mat_index[name]

    def add_model(self, name, mesh, slot_names):
        # GTA (right-handed, Z up) -> glTF (right-handed, Y up): (x, y, z) -> (x, z, -y)
        p = mesh.positions
        pos = np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1).astype(np.float32)
        n = mesh.normals
        nrm = np.stack([n[:, 0], n[:, 2], -n[:, 1]], axis=1).astype(np.float32)
        ln = np.linalg.norm(nrm, axis=1)
        bad = ln < 1e-6
        nrm[bad] = (0.0, 1.0, 0.0)
        ln[bad] = 1.0
        nrm /= ln[:, None]
        uv = mesh.uvs.astype(np.float32)  # GTA UVs are already top-left origin like glTF
        a_pos = self._add(pos, 34962, 5126, "VEC3", minmax=True)
        a_nrm = self._add(nrm, 34962, 5126, "VEC3")
        a_uv = self._add(uv, 34962, 5126, "VEC2")
        prims = []
        for mi in np.unique(mesh.face_mats):
            idx = mesh.faces[mesh.face_mats == mi].astype(np.uint32).ravel()
            a_idx = self._add(idx, 34963, 5125, "SCALAR")
            prims.append({"attributes": {"POSITION": a_pos, "NORMAL": a_nrm, "TEXCOORD_0": a_uv},
                          "indices": a_idx, "material": self.material(slot_names[mi]), "mode": 4})
        self.meshes.append({"name": name, "primitives": prims})
        self.nodes.append({"name": name, "mesh": len(self.meshes) - 1})

    def write(self, path):
        bin_name = os.path.splitext(os.path.basename(path))[0] + ".bin"
        with open(os.path.join(os.path.dirname(path), bin_name), "wb") as f:
            f.write(self.bin)
        doc = {
            "asset": {"version": "2.0", "generator": "GTA IV Map Exporter"},
            "scene": 0,
            "scenes": [{"nodes": list(range(len(self.nodes)))}],
            "nodes": self.nodes,
            "meshes": self.meshes,
            "materials": self.materials,
            "accessors": self.accessors,
            "bufferViews": self.views,
            "buffers": [{"uri": bin_name, "byteLength": len(self.bin)}],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))


class UnrealExporter:
    def __init__(self, game_dir, out_dir, areas="*", lods=False, textures=True, key_cache=None, log=print,
                 detail=None, interiors=True):
        self.game_dir = game_dir
        self.out_dir = out_dir
        self.area_patterns = [a.strip().lower() for a in areas.replace(";", ",").split(",") if a.strip()] or ["*"]
        self.detail = detail or ("LOW" if lods else "FULL")
        self.textures = textures
        self.interiors = interiors
        self.key_cache = key_cache or os.path.join(out_dir, "gta4_key.bin")
        self.log = log
        self._txd_cache = {}
        self._wdd_cache = {}
        self.materials = {}   # material asset name -> {diffuse, bump, masked}
        self.tex_names = {}   # GTA texture name -> written file stem (or None)

    def _find_texture(self, name, mesh, txd):
        tex = mesh.textures.get(name)
        if tex is not None:
            return tex
        for t in self.gd.txd_chain(txd):
            d = self._txd_cache.get(t)
            if d is None:
                d = self._txd_cache[t] = self.gd.load_txd(t)
            tex = d.get(name)
            if tex is not None:
                return tex
        return None

    def _texture(self, name, mesh, txd):
        if not name or not self.textures:
            return None
        if name in self.tex_names:
            return self.tex_names[name]
        stem = None
        tex = self._find_texture(name, mesh, txd)
        if tex is not None and tex.data:
            stem = "T_" + ue_name(name)
            path = os.path.join(self.out_dir, "textures", stem + ".png")
            if not os.path.isfile(path):
                try:
                    png = tex.to_png()
                except Exception as e:
                    self.log("Couldn't decode texture %s: %s" % (name, e))
                    self.tex_names[name] = None
                    return None
                with open(path, "wb") as f:
                    f.write(png)
        self.tex_names[name] = stem
        return stem

    def _material_name(self, m, mesh, txd):
        masked = any(s in m.shader.lower() for s in ALPHA_SHADERS)
        diffuse = self._texture(m.diffuse, mesh, txd)
        bump = self._texture(m.bump, mesh, txd)
        name = "MI_" + (ue_name(m.diffuse) if m.diffuse else "untextured")
        if masked:
            name += "_M"
        if bump:
            name += "_N"
        if name not in self.materials:
            self.materials[name] = {"diffuse": diffuse, "bump": bump, "masked": masked}
        return name

    def _wanted_area(self, area):
        return any(fnmatch.fnmatch(area, p) for p in self.area_patterns)

    def run(self, progress=None):
        t0 = time.time()
        for sub in ("meshes", "textures"):
            os.makedirs(os.path.join(self.out_dir, sub), exist_ok=True)
        self.gd = gd = GameData(self.game_dir, log=self.log, key_cache=self.key_cache)
        gd.load_definitions()
        gd.open_archives()
        areas = gd.load_instances(interiors=self.interiors)

        selected = {}
        for area, insts in areas.items():
            if self._wanted_area(area):
                keep = select_detail(gd, insts, area, self.detail)
                if keep:
                    selected[area] = keep
        if not selected:
            raise RuntimeError("No placements matched areas '%s'" % ",".join(self.area_patterns))
        selected = split_interiors(selected)
        need = {}  # model -> smallest size any of its placements accepts
        for v in selected.values():
            for i, min_size in v:
                need[i.hash] = min(need.get(i.hash, min_size), min_size)
        hashes = sorted(need)
        sizes = {}
        self.log("Detail level %s: %d candidate placements of %d models from %d areas"
                 % (self.detail, sum(len(v) for v in selected.values()), len(hashes), len(selected)))

        models = {}  # hash -> {"name", "batch"}
        batch, batch_no, batches = _GltfBatch(), 0, []
        batch_meshes = {}  # gltf file -> mesh names in it (lets the importer resume)

        def flush():
            nonlocal batch, batch_no
            if batch.nodes:
                fn = "batch_%03d.gltf" % batch_no
                batch.write(os.path.join(self.out_dir, "meshes", fn))
                batches.append(fn)
                batch_meshes[fn] = [n["name"] for n in batch.nodes]
                batch_no += 1
                batch = _GltfBatch()

        for n, h in enumerate(hashes):
            mdef = gd.models[h]
            try:
                mesh = gd.load_model(mdef, self._wdd_cache)
            except Exception as e:
                self.log("Failed to read %s: %s" % (mdef.name, e))
                mesh = None
            if mesh is not None and model_size(mesh) < need[h]:
                mesh = None  # too small for this detail level
            if mesh is not None:
                sizes[h] = model_size(mesh)
                slots = [self._material_name(m, mesh, mdef.txd) for m in mesh.materials]
                sm = "SM_" + ue_name(mdef.name)
                batch.add_model(sm, mesh, slots)
                models[h] = {"name": sm, "batch": "batch_%03d" % batch_no}
                if len(batch.nodes) >= BATCH_MODELS:
                    flush()
            if progress:
                progress(n / len(hashes))
            if n % 1000 == 0:
                self.log("  models %d / %d  (%.0fs)" % (n, len(hashes), time.time() - t0))
        flush()

        placements = {}
        for area, insts in sorted(selected.items()):
            rows = []
            for i, min_size in insts:
                m = models.get(i.hash)
                if m is None or sizes[i.hash] < min_size:
                    continue
                rows.append([m["name"]] + [round(v, 4) for v in i.pos] + [round(v, 6) for v in _quat(i.rot)])
            placements[area] = rows
        data = {
            "version": 1,
            "batches": batches,
            "batch_meshes": batch_meshes,
            "textures": sorted({v for v in self.tex_names.values() if v}),
            "materials": self.materials,
            "placements": placements,  # [mesh, x, y, z, qw, qx, qy, qz] in GTA space (metres, right-handed)
        }
        with open(os.path.join(self.out_dir, "gta4_unreal.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)
        for _, a in gd.archives:
            a.close()
        self.log("Wrote %d mesh batches, %d textures, %d materials in %.0fs"
                 % (len(batches), len(data["textures"]), len(self.materials), time.time() - t0))
        return sum(len(v) for v in placements.values())
