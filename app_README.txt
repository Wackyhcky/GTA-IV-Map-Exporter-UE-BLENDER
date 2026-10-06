GTA IV Map Exporter
===================

1. Double-click "GTA IV Map Exporter.exe".
2. Check the GTA IV folder at the top (found automatically on most machines).
3. Pick where to export:
   - Blender: needs Blender 4.2+ installed; choose where to save the .blend.
   - Unreal Engine: needs Unreal Engine 5 installed; pick your .uproject and a level name.
     Close Unreal Editor first. Everything goes into Content/GTAIV in that project.
     Tip: use a separate project, the full city adds about 9 GB of assets.
4. Tick the districts you want (or "Select all" for the whole city).
5. Click "Export", wait for the progress bar, then "Open in Blender" / "Open in Unreal".

Keep the .exe next to the "_internal" folder.
Blender: textures are saved in a "<name>_textures" folder beside the .blend; move them together.
