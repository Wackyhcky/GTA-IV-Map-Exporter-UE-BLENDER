"""Runs inside Blender (blender -b --python blender_runner.py -- job.json).

Imports the map with the add-on code and saves a .blend. Talks to the GUI
through stdout lines starting with '@@'.
"""

import json
import os
import sys
import traceback

sys.stdout.reconfigure(line_buffering=True)

job = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
sys.path.insert(0, job["addon_dir"])

import bpy  # noqa: E402

from gta4_map_importer.blender_import import Importer  # noqa: E402

_last = [-1.0]


def progress(f):
    if f - _last[0] >= 0.002 or f >= 1.0:
        _last[0] = f
        print("@@PROGRESS %.4f" % f)


def main():
    # factory startup scene has a cube, camera and light
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    n = Importer(job["game_dir"], job["cache_dir"], job["areas"], job["lods"], job["mode"],
                 job["textures"], job["normals"],
                 key_cache=job.get("key_cache"), detail=job.get("detail"),
                 interiors=job.get("interiors", True)).run(progress=progress)
    # the map is ~6 km across; default 1 km clip distance would hide most of it
    for screen in bpy.data.screens:
        for area in screen.areas:
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    space.clip_end = 20000.0
                    space.clip_start = 0.1
    print("@@SAVING")
    bpy.ops.wm.save_as_mainfile(filepath=job["output"], relative_remap=True)
    print("@@DONE %d" % n)


try:
    main()
except Exception as e:
    traceback.print_exc()
    print("@@FAILED %s" % e)
    sys.exit(1)
