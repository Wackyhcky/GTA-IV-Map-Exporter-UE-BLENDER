bl_info = {
    "name": "GTA IV Map Importer",
    "author": "dessierror",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > Import > GTA IV Map",
    "description": "Import the GTA IV map straight from the game files (no OpenIV needed)",
    "category": "Import-Export",
}

import os

try:
    import bpy
    from bpy.props import BoolProperty, EnumProperty, StringProperty
    from bpy.types import AddonPreferences, Operator
except ImportError:  # used outside Blender
    bpy = None

if bpy is not None:

    class GTA4_Prefs(AddonPreferences):
        bl_idname = __package__

        game_dir: StringProperty(
            name="GTA IV folder",
            description="Folder that contains GTAIV.exe",
            subtype="DIR_PATH",
        )

        def draw(self, context):
            self.layout.prop(self, "game_dir")

    class IMPORT_OT_gta4_map(Operator):
        """Import the GTA IV map from the game folder"""
        bl_idname = "import_scene.gta4_map"
        bl_label = "Import GTA IV Map"
        bl_options = {"REGISTER", "UNDO"}

        game_dir: StringProperty(name="GTA IV folder", subtype="DIR_PATH",
                                 description="Folder that contains GTAIV.exe")
        cache_dir: StringProperty(name="Texture folder", subtype="DIR_PATH",
                                  description="Where extracted .dds textures are stored "
                                              "(blank = next to the .blend, or the temp folder)")
        areas: StringProperty(
            name="Areas", default="*",
            description="Comma separated area names or wildcards, e.g. 'manhat*' or "
                        "'manhat01,brook_n'. Areas: nj_* (Alderney), manhat* (Algonquin), "
                        "brook_*, queens_*, bronx_* (Broker/Dukes/Bohan)")
        mode: EnumProperty(
            name="Placement",
            items=[("INSTANCES", "Geometry Nodes instances",
                    "One point cloud per area instancing shared models. Fast; use for the whole map"),
                   ("OBJECTS", "Separate objects",
                    "One object per placement (shared mesh data). Editable; best for a few areas")],
            default="INSTANCES")
        detail: EnumProperty(
            name="Detail level",
            items=[("FULL", "Full detail", "Everything, including small props"),
                   ("HIGH", "High", "Skip tiny props under 2 m"),
                   ("MEDIUM", "Medium", "Skip props under 8 m"),
                   ("LOW", "Low", "GTA's low-poly LOD models plus large objects"),
                   ("VERYLOW", "Very low", "GTA's far-distance SLOD models plus large objects")],
            default="FULL")
        interiors: BoolProperty(name="Interiors", default=True,
                                description="Building interiors and subway tunnels, in their own collections")
        textures: BoolProperty(name="Textures", default=True)
        normals: BoolProperty(name="Custom normals", default=True,
                              description="Use the game's vertex normals (slower import)")
        quat_conjugate: BoolProperty(name="Invert WPL rotation", default=True,
                                     options={"HIDDEN"})

        def invoke(self, context, event):
            prefs = context.preferences.addons[__package__].preferences
            if not self.game_dir:
                self.game_dir = prefs.game_dir
            return context.window_manager.invoke_props_dialog(self, width=480)

        def execute(self, context):
            from .blender_import import Importer

            game_dir = bpy.path.abspath(self.game_dir)
            if not os.path.isfile(os.path.join(game_dir, "GTAIV.exe")):
                self.report({"ERROR"}, "GTAIV.exe not found in '%s'" % game_dir)
                return {"CANCELLED"}
            context.preferences.addons[__package__].preferences.game_dir = self.game_dir
            cache = bpy.path.abspath(self.cache_dir) if self.cache_dir else ""
            if not cache:
                cache = (os.path.join(os.path.dirname(bpy.data.filepath), "gta4_cache")
                         if bpy.data.filepath else os.path.join(bpy.app.tempdir, "gta4_cache"))
            wm = context.window_manager
            wm.progress_begin(0, 100)
            try:
                n = Importer(game_dir, cache, self.areas, False, self.mode, self.textures,
                             self.normals, self.quat_conjugate, detail=self.detail,
                             interiors=self.interiors).run(
                    progress=lambda f: wm.progress_update(int(f * 100)))
            except Exception as e:
                self.report({"ERROR"}, str(e))
                return {"CANCELLED"}
            finally:
                wm.progress_end()
            self.report({"INFO"}, "Imported %d placements" % n)
            return {"FINISHED"}

        def draw(self, context):
            col = self.layout.column()
            for p in ("game_dir", "areas", "mode", "detail", "interiors", "textures", "normals", "cache_dir"):
                col.prop(self, p)

    def _menu(self, context):
        self.layout.operator(IMPORT_OT_gta4_map.bl_idname, text="GTA IV Map (game folder)")

    _classes = (GTA4_Prefs, IMPORT_OT_gta4_map)

    def register():
        for c in _classes:
            bpy.utils.register_class(c)
        bpy.types.TOPBAR_MT_file_import.append(_menu)

    def unregister():
        bpy.types.TOPBAR_MT_file_import.remove(_menu)
        for c in reversed(_classes):
            bpy.utils.unregister_class(c)
