"""Runs inside Unreal Editor:
    set GTA4_UE_JOB=job.json
    UnrealEditor-Cmd.exe <project> -run=pythonscript -script="unreal_runner.py"

Imports what `gta4_map_importer.unreal_export` wrote: textures, master
materials + instances, static meshes, and a level with every placement.
Reports to the GUI with log lines starting with '@@'.
"""

import json
import math
import os
import sys
import time
import traceback

import unreal

ROOT = "/Game/GTAIV"


def say(msg):
    unreal.log("@@" + msg)


def progress(f):
    say("PROGRESS %.4f" % f)


def load_job():
    # the GUI passes the job through an env var: Unreal's -script= argument
    # doesn't cope well with extra arguments or paths containing spaces
    env = os.environ.get("GTA4_UE_JOB")
    if env and os.path.isfile(env):
        return json.load(open(env, encoding="utf-8"))
    for a in sys.argv[1:]:
        if a.lower().endswith(".json") and os.path.isfile(a):
            return json.load(open(a, encoding="utf-8"))
    raise RuntimeError("no job file given")


ASSET_TOOLS = unreal.AssetToolsHelpers.get_asset_tools()
EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary


# ------------------------------------------------------------------ textures
def import_textures(export_dir, names, normal_maps, p0, p1):
    dest = ROOT + "/Textures"
    todo = [n for n in names if not EAL.does_asset_exist("%s/%s" % (dest, n))]
    for i in range(0, len(todo), 200):
        tasks = []
        for n in todo[i:i + 200]:
            t = unreal.AssetImportTask()
            t.filename = os.path.join(export_dir, "textures", n + ".png")
            t.destination_path = dest
            t.automated = True
            t.replace_existing = True
            t.save = False
            tasks.append(t)
        ASSET_TOOLS.import_asset_tasks(tasks)
        progress(p0 + (p1 - p0) * min(1.0, (i + 200) / max(1, len(todo))))
    for n in normal_maps:
        tex = unreal.load_asset("%s/%s" % (dest, n))
        if tex:
            tex.set_editor_property("srgb", False)
            tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
    EAL.save_directory(dest, only_if_is_dirty=True)
    say("LOG Imported %d textures" % len(todo))


# ------------------------------------------------------------------ materials
def _master(name, masked):
    path = "%s/Materials/%s" % (ROOT, name)
    if EAL.does_asset_exist(path):
        return unreal.load_asset(path)
    mat = ASSET_TOOLS.create_asset(name, ROOT + "/Materials", unreal.Material, unreal.MaterialFactoryNew())
    # set usage up front, otherwise every instance recompiles the first time the map loads
    for flag in ("used_with_instanced_static_meshes", "used_with_nanite"):
        try:
            mat.set_editor_property(flag, True)
        except Exception:
            pass
    diff = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -500, -100)
    diff.set_editor_property("parameter_name", "Diffuse")
    diff.set_editor_property("texture", unreal.load_asset("/Engine/EngineResources/WhiteSquareTexture"))
    MEL.connect_material_property(diff, "RGB", unreal.MaterialProperty.MP_BASE_COLOR)
    nrm = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -500, 250)
    nrm.set_editor_property("parameter_name", "Normal")
    nrm.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    nrm.set_editor_property("texture", unreal.load_asset("/Engine/EngineMaterials/DefaultNormal"))
    MEL.connect_material_property(nrm, "RGB", unreal.MaterialProperty.MP_NORMAL)
    rough = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -250, 100)
    rough.set_editor_property("r", 0.85)
    MEL.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)
    spec = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, -250, 180)
    spec.set_editor_property("r", 0.3)
    MEL.connect_material_property(spec, "", unreal.MaterialProperty.MP_SPECULAR)
    if masked:
        mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
        mat.set_editor_property("two_sided", True)
        mat.set_editor_property("opacity_mask_clip_value", 0.4)
        MEL.connect_material_property(diff, "A", unreal.MaterialProperty.MP_OPACITY_MASK)
    MEL.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    return mat


def create_materials(materials, p0, p1):
    masters = {False: _master("M_GTA_Opaque", False), True: _master("M_GTA_Masked", True)}
    dest = ROOT + "/Materials"
    factory = unreal.MaterialInstanceConstantFactoryNew()
    items = sorted(materials.items())
    for i, (name, m) in enumerate(items):
        path = "%s/%s" % (dest, name)
        if EAL.does_asset_exist(path):
            continue
        mi = ASSET_TOOLS.create_asset(name, dest, unreal.MaterialInstanceConstant, factory)
        MEL.set_material_instance_parent(mi, masters[bool(m["masked"])])
        for param, key in (("Diffuse", "diffuse"), ("Normal", "bump")):
            if m.get(key):
                tex = unreal.load_asset("%s/Textures/%s" % (ROOT, m[key]))
                if tex:
                    MEL.set_material_instance_texture_parameter_value(mi, param, tex)
        if i % 200 == 0:
            progress(p0 + (p1 - p0) * i / max(1, len(items)))
    EAL.save_directory(dest, only_if_is_dirty=True)
    say("LOG Created %d materials" % len(items))


# ------------------------------------------------------------------ meshes
def _pipeline(nanite):
    pipe = unreal.InterchangeGenericAssetsPipeline()
    pipe.material_pipeline.import_materials = False
    pipe.material_pipeline.texture_pipeline.import_textures = False
    pipe.mesh_pipeline.combine_static_meshes_behavior = unreal.InterchangeCombineStaticMeshesBehavior.DO_NOT_COMBINE
    pipe.mesh_pipeline.import_skeletal_meshes = False
    pipe.mesh_pipeline.build_nanite = bool(nanite)
    pipe.mesh_pipeline.collision = False  # we use the render mesh as collision instead
    return pipe


def _batch_done(names):
    return bool(names) and all(EAL.does_asset_exist("%s/Meshes/%s" % (ROOT, n)) for n in names)


def import_meshes(export_dir, batches, batch_meshes, nanite, collision, max_batches, p0, p1):
    """Import up to `max_batches` batches that aren't in the project yet.

    Returns the number of batches still left. Unreal's memory grows with every
    import and is never fully released, so the GUI runs this in several
    short-lived editor processes instead of one long one.
    """
    dest = ROOT + "/Meshes"
    mat_cache = {}
    todo = [b for b in batches if not _batch_done(batch_meshes.get(b))]
    if len(todo) < len(batches):
        say("LOG  %d of %d mesh batches already imported" % (len(batches) - len(todo), len(batches)))
    for i, b in enumerate(todo[:max_batches]):
        # InterchangeManager.import_asset only completes its first call in commandlet mode;
        # AssetImportTask with a pipeline stack override works for every batch.
        task = unreal.AssetImportTask()
        task.filename = os.path.join(export_dir, "meshes", b)
        task.destination_path = dest
        task.automated = True
        task.replace_existing = True
        task.save = False
        stack = unreal.InterchangePipelineStackOverride()
        stack.add_pipeline(_pipeline(nanite))
        task.options = stack
        ASSET_TOOLS.import_asset_tasks([task])
        result = list(task.imported_object_paths)
        if not result:
            say("LOG  warning: nothing imported from %s" % b)
        for path in result:
            sm = unreal.load_asset(path)
            if not isinstance(sm, unreal.StaticMesh):
                continue
            for si, slot in enumerate(sm.static_materials):
                name = str(slot.material_slot_name)
                if name not in mat_cache:
                    mat_cache[name] = unreal.load_asset("%s/Materials/%s" % (ROOT, name))
                if mat_cache[name]:
                    sm.set_material(si, mat_cache[name])
            if collision:
                bs = sm.get_editor_property("body_setup")
                if bs:
                    bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
        EAL.save_directory(dest, only_if_is_dirty=True)
        del task, stack, result
        sm = None
        unreal.SystemLibrary.collect_garbage()
        done = len(batches) - len(todo) + i + 1
        progress(p0 + (p1 - p0) * done / len(batches))
        say("LOG  mesh batch %d / %d" % (done, len(batches)))
    return max(0, len(todo) - max_batches)


# ------------------------------------------------------------------ level
def _transform(row):
    """GTA (metres, right-handed) -> Unreal (cm, left-handed: Y is mirrored)."""
    _, x, y, z, qw, qx, qy, qz = row
    q = unreal.Quat(-qx, qy, -qz, qw)
    return unreal.Transform(unreal.Vector(x * 100.0, -y * 100.0, z * 100.0), q.rotator(), unreal.Vector(1, 1, 1))


def _add_lighting(eas):
    """New levels are empty; add a basic daylight setup so the city is visible."""
    sun = eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 50000),
                                     unreal.Rotator(roll=0, pitch=-40, yaw=-60))
    sun.set_actor_label("Sun")
    lc = sun.get_component_by_class(unreal.DirectionalLightComponent)
    lc.set_editor_property("intensity", 8.0)
    lc.set_editor_property("atmosphere_sun_light", True)
    lc.set_editor_property("dynamic_shadow_distance_movable_light", 30000.0)
    sky = eas.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0))
    sky.set_actor_label("SkyAtmosphere")
    sl = eas.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0, 0, 50000))
    sl.set_actor_label("SkyLight")
    slc = sl.get_component_by_class(unreal.SkyLightComponent)
    slc.set_editor_property("real_time_capture", True)
    slc.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    fog = eas.spawn_actor_from_class(unreal.ExponentialHeightFog, unreal.Vector(0, 0, 0))
    fog.set_actor_label("HeightFog")
    fog.get_component_by_class(unreal.ExponentialHeightFogComponent).set_editor_property("fog_density", 0.005)
    for a in (sun, sky, sl, fog):
        a.set_folder_path("Lighting")


def build_level(placements, map_path, mode, p0, p1):
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    if EAL.does_asset_exist(map_path):
        # re-export: reuse the map, replacing everything in it
        if not les.load_level(map_path):
            raise RuntimeError("Couldn't open existing level %s" % map_path)
        eas.destroy_actors([a for a in eas.get_all_level_actors()
                            if not isinstance(a, (unreal.WorldSettings, unreal.Brush))])
    elif not les.new_level(map_path):
        raise RuntimeError("Couldn't create level %s" % map_path)
    _add_lighting(eas)
    sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    lib = unreal.SubobjectDataBlueprintFunctionLibrary
    meshes = {}
    total = sum(len(v) for v in placements.values())
    done = 0
    for area, rows in sorted(placements.items()):
        if mode == "ACTORS":
            folder = "GTAIV/" + area
            for row in rows:
                sm = meshes.get(row[0]) or meshes.setdefault(row[0], unreal.load_asset("%s/Meshes/%s" % (ROOT, row[0])))
                if not sm:
                    continue
                t = _transform(row)
                # spawn_actor_from_object needs actor factories, which aren't available headless
                a = eas.spawn_actor_from_class(unreal.StaticMeshActor, t.translation, t.rotation.rotator())
                if a:
                    a.static_mesh_component.set_static_mesh(sm)
                    a.set_actor_label(row[0][3:])
                    a.set_folder_path(folder)
                done += 1
                if done % 500 == 0:
                    progress(p0 + (p1 - p0) * done / total)
        else:
            actor = eas.spawn_actor_from_class(unreal.Actor, unreal.Vector(0, 0, 0))
            actor.set_actor_label("GTAIV_" + area)
            handles = sds.k2_gather_subobject_data_for_instance(actor)
            root_handle = handles[0]
            by_mesh = {}
            for row in rows:
                by_mesh.setdefault(row[0], []).append(row)
            for mesh_name, mrows in sorted(by_mesh.items()):
                sm = meshes.get(mesh_name) or meshes.setdefault(mesh_name, unreal.load_asset("%s/Meshes/%s" % (ROOT, mesh_name)))
                if not sm:
                    continue
                params = unreal.AddNewSubobjectParams(parent_handle=root_handle,
                                                      new_class=unreal.HierarchicalInstancedStaticMeshComponent)
                handle, fail = sds.add_new_subobject(params)
                comp = lib.get_object(lib.get_data(handle))
                sds.rename_subobject(handle, unreal.Text(mesh_name[3:]))
                comp.set_static_mesh(sm)
                comp.add_instances([_transform(r) for r in mrows], False, True)
                done += len(mrows)
            progress(p0 + (p1 - p0) * done / total)
        say("LOG  placed %s (%d)" % (area, len(rows)))
    les.save_current_level()


def main():
    t0 = time.time()
    job = load_job()
    export_dir = job["export_dir"]
    data = json.load(open(os.path.join(export_dir, "gta4_unreal.json"), encoding="utf-8"))
    stage = job.get("stage", "all")
    # progress: textures 0-5%, materials 5-10%, meshes 10-85%, level 85-100%
    if stage in ("all", "assets"):
        if job.get("first_pass", True):
            normal_maps = sorted({m["bump"] for m in data["materials"].values() if m.get("bump")})
            say("LOG Importing into %s" % ROOT)
            import_textures(export_dir, data["textures"], normal_maps, 0.0, 0.05)
            create_materials(data["materials"], 0.05, 0.10)
        left = import_meshes(export_dir, data["batches"], data.get("batch_meshes", {}),
                             job.get("nanite", False), job.get("collision", True),
                             job.get("max_batches", 10 ** 9), 0.10, 0.85)
        if left:
            say("MORE %d" % left)
            return
        if stage == "assets":
            say("ASSETS_DONE")
            return
    map_path = "%s/Maps/%s" % (ROOT, job.get("map_name", "LibertyCity"))
    say("LOG Building level %s" % map_path)
    build_level(data["placements"], map_path, job.get("mode", "INSTANCES"), 0.85, 1.0)
    say("DONE %d" % sum(len(v) for v in data["placements"].values()))
    say("LOG Finished in %.0fs. Open %s in the editor." % (time.time() - t0, map_path))


try:
    main()
except Exception as e:
    unreal.log_error(traceback.format_exc())
    say("FAILED %s" % e)
