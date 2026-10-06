"""Builds the GTA IV map inside Blender."""

import fnmatch
import os
import time

import bpy
import numpy as np

from .mapdata import GameData, model_size, select_detail, split_interiors

ALPHA_SHADERS = ("alpha", "cutout", "decal", "glass", "window", "billboard", "trees", "grass", "wire")


def _log(msg):
    print("[GTA IV] " + msg)


class Importer:
    def __init__(self, game_dir, cache_dir, areas="*", lods=False, mode="INSTANCES",
                 textures=True, normals=True, quat_conjugate=True, key_cache=None, detail=None, interiors=True):
        self.game_dir = game_dir
        self.cache_dir = cache_dir
        self.area_patterns = [a.strip().lower() for a in areas.replace(";", ",").split(",") if a.strip()] or ["*"]
        self.detail = detail or ("LOW" if lods else "FULL")
        self.mode = mode
        self.interiors = interiors
        self.textures = textures
        self.normals = normals
        self.quat_conjugate = quat_conjugate
        self.key_cache = key_cache or os.path.join(cache_dir, "gta4_key.bin")
        self.tex_dir = os.path.join(cache_dir, "textures")
        self._txd_cache = {}
        self._wdd_cache = {}
        self._mat_cache = {}
        self._img_cache = {}
        self._written = set()

    # ------------------------------------------------------------ textures
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

    def _image(self, name, mesh, txd):
        if not name or not self.textures:
            return None
        key = name
        if key in self._img_cache:
            return self._img_cache[key]
        img = None
        tex = self._find_texture(name, mesh, txd)
        if tex is not None:
            path = os.path.join(self.tex_dir, name + ".dds")
            if not os.path.isfile(path):
                os.makedirs(self.tex_dir, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(tex.to_dds())
            try:
                img = bpy.data.images.load(path, check_existing=True)
                img.alpha_mode = "CHANNEL_PACKED"
            except Exception:
                img = None
        self._img_cache[key] = img
        return img

    # ------------------------------------------------------------ materials
    def _material(self, m, mesh, txd):
        shader = m.shader.lower()
        alpha = any(s in shader for s in ALPHA_SHADERS)
        key = (m.diffuse, m.bump if self.textures else None, alpha)
        mat = self._mat_cache.get(key)
        if mat is not None:
            return mat
        mat = bpy.data.materials.new(m.diffuse or "gta_untextured")
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
        bsdf.inputs["Roughness"].default_value = 0.8
        img = self._image(m.diffuse, mesh, txd)
        if img is not None:
            tn = nt.nodes.new("ShaderNodeTexImage")
            tn.image = img
            tn.location = (-400, 300)
            nt.links.new(tn.outputs["Color"], bsdf.inputs["Base Color"])
            if alpha:
                nt.links.new(tn.outputs["Alpha"], bsdf.inputs["Alpha"])
                for attr, val in (("surface_render_method", "DITHERED"), ("blend_method", "HASHED")):
                    try:
                        setattr(mat, attr, val)
                    except Exception:
                        pass
            mat.diffuse_color = (0.8, 0.8, 0.8, 1)
        bump = self._image(m.bump, mesh, txd)
        if bump is not None:
            bump.colorspace_settings.name = "Non-Color"
            bn = nt.nodes.new("ShaderNodeTexImage")
            bn.image = bump
            bn.location = (-700, -200)
            nm = nt.nodes.new("ShaderNodeNormalMap")
            nm.location = (-300, -200)
            nm.inputs["Strength"].default_value = 0.6
            nt.links.new(bn.outputs["Color"], nm.inputs["Color"])
            nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        self._mat_cache[key] = mat
        return mat

    # ------------------------------------------------------------ meshes
    def _build_mesh(self, mdef, mesh):
        faces = mesh.faces
        nv, nf = len(mesh.positions), len(faces)
        me = bpy.data.meshes.new(mdef.name)
        me.vertices.add(nv)
        me.vertices.foreach_set("co", mesh.positions.ravel())
        me.loops.add(nf * 3)
        me.loops.foreach_set("vertex_index", faces.ravel())
        me.polygons.add(nf)
        me.polygons.foreach_set("loop_start", np.arange(0, nf * 3, 3, dtype=np.int32))
        me.polygons.foreach_set("material_index", mesh.face_mats)

        uv = me.uv_layers.new(name="UVMap")
        loop_uv = mesh.uvs[faces.ravel()].copy()
        loop_uv[:, 1] = 1.0 - loop_uv[:, 1]
        uv.data.foreach_set("uv", loop_uv.ravel())

        col = me.color_attributes.new("Col", "BYTE_COLOR", "POINT")
        col.data.foreach_set("color", np.clip(mesh.colors, 0, 1).ravel())

        for m in mesh.materials:
            me.materials.append(self._material(m, mesh, mdef.txd))

        me.validate(clean_customdata=False)
        me.update()
        if self.normals:
            n = mesh.normals.astype(np.float64)
            ln = np.linalg.norm(n, axis=1)
            if (ln > 0.5).all():
                me.polygons.foreach_set("use_smooth", np.ones(len(me.polygons), dtype=bool))
                try:
                    me.normals_split_custom_set_from_vertices((n / ln[:, None]).tolist())
                except Exception:
                    pass
        return me

    # ------------------------------------------------------------ main
    def _wanted_area(self, area):
        return any(fnmatch.fnmatch(area, p) for p in self.area_patterns)

    def run(self, progress=None):
        t0 = time.time()
        os.makedirs(self.cache_dir, exist_ok=True)
        self.gd = gd = GameData(self.game_dir, log=_log,
                                key_cache=self.key_cache)
        gd.load_definitions()
        gd.open_archives()
        areas = gd.load_instances(interiors=self.interiors)

        selected = {}
        for area, insts in areas.items():
            if not self._wanted_area(area):
                continue
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
        _log("Detail level %s: %d candidate placements of %d models from %d areas"
             % (self.detail, sum(len(v) for v in selected.values()), len(hashes), len(selected)))

        root = bpy.data.collections.new("GTA IV Map")
        bpy.context.scene.collection.children.link(root)
        lib = bpy.data.collections.new("GTA IV Models")
        root.children.link(lib)

        meshes = {}
        sizes = {}
        lib_objects = {}
        for n, h in enumerate(hashes):
            mdef = gd.models[h]
            try:
                mesh = gd.load_model(mdef, self._wdd_cache)
            except Exception as e:
                _log("Failed to read %s: %s" % (mdef.name, e))
                mesh = None
            if mesh is not None:
                size = model_size(mesh)
                if size >= need[h]:
                    meshes[h] = self._build_mesh(mdef, mesh)
                    sizes[h] = size
            if progress:
                progress(0.8 * n / len(hashes))
            if n % 500 == 0:
                _log("  models %d / %d  (%.0fs)" % (n, len(hashes), time.time() - t0))
        self._txd_cache.clear()
        self._wdd_cache.clear()
        # drop placements whose model is smaller than this level allows
        selected = {a: [i for i, min_size in v if i.hash in meshes and sizes[i.hash] >= min_size]
                    for a, v in selected.items()}
        selected = {a: v for a, v in selected.items() if v}
        total = sum(len(v) for v in selected.values())
        _log("Placing %d objects" % total)

        if self.mode == "INSTANCES":
            lib_index = {}
            for idx, h in enumerate(h for h in hashes if h in meshes):
                me = meshes[h]
                ob = bpy.data.objects.new("%05d_%s" % (idx, gd.models[h].name), me)
                lib.objects.link(ob)
                lib_index[h] = idx
                lib_objects[h] = ob
            lib.hide_render = True
            lib.hide_viewport = True
            tree = self._instance_tree(lib)
        for ai, (area, insts) in enumerate(sorted(selected.items())):
            coll = bpy.data.collections.new(area)
            root.children.link(coll)
            insts = [i for i in insts if i.hash in meshes]
            if self.mode == "INSTANCES":
                self._area_points(coll, area, insts, lib_index, tree)
            else:
                for i in insts:
                    ob = bpy.data.objects.new(gd.models[i.hash].name, meshes[i.hash])
                    ob.location = i.pos
                    ob.rotation_mode = "QUATERNION"
                    ob.rotation_quaternion = self._quat(i.rot)
                    coll.objects.link(ob)
            if progress:
                progress(0.8 + 0.2 * ai / len(selected))
        for a in gd.archives:
            a[1].close()
        _log("Done in %.0fs" % (time.time() - t0))
        return total

    def _quat(self, r):
        x, y, z, w = r
        return (w, -x, -y, -z) if self.quat_conjugate else (w, x, y, z)

    def _area_points(self, coll, area, insts, lib_index, tree):
        me = bpy.data.meshes.new(area + "_placements")
        me.vertices.add(len(insts))
        me.vertices.foreach_set("co", np.array([i.pos for i in insts], np.float32).ravel())
        a = me.attributes.new("model_index", "INT", "POINT")
        a.data.foreach_set("value", np.array([lib_index[i.hash] for i in insts], np.int32))
        q = me.attributes.new("rot", "QUATERNION", "POINT")
        q.data.foreach_set("value", np.array([self._quat(i.rot) for i in insts], np.float32).ravel())
        ob = bpy.data.objects.new(area, me)
        coll.objects.link(ob)
        mod = ob.modifiers.new("GTA IV Instances", "NODES")
        mod.node_group = tree

    def _instance_tree(self, lib):
        tree = bpy.data.node_groups.new("GTA IV Instancer", "GeometryNodeTree")
        tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        N = tree.nodes
        gin = N.new("NodeGroupInput")
        gout = N.new("NodeGroupOutput")
        ci = N.new("GeometryNodeCollectionInfo")
        ci.inputs["Collection"].default_value = lib
        ci.inputs["Separate Children"].default_value = True
        ci.inputs["Reset Children"].default_value = True
        ci.transform_space = "ORIGINAL"
        idx = N.new("GeometryNodeInputNamedAttribute")
        idx.data_type = "INT"
        idx.inputs["Name"].default_value = "model_index"
        rot = N.new("GeometryNodeInputNamedAttribute")
        rot.data_type = "QUATERNION"
        rot.inputs["Name"].default_value = "rot"
        iop = N.new("GeometryNodeInstanceOnPoints")
        iop.inputs["Pick Instance"].default_value = True
        L = tree.links
        L.new(gin.outputs[0], iop.inputs["Points"])
        L.new(ci.outputs[0], iop.inputs["Instance"])
        L.new(idx.outputs["Attribute"], iop.inputs["Instance Index"])
        L.new(rot.outputs["Attribute"], iop.inputs["Rotation"])
        L.new(iop.outputs[0], gout.inputs[0])
        for i, n in enumerate((gin, ci, idx, rot, iop, gout)):
            n.location = (i * 220, 0)
        return tree
