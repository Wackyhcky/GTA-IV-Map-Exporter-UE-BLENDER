# GTA IV Map Importer for Blender

Imports the Liberty City map straight from your GTA IV install. You don't need OpenIV
or any pre-extracted files.

## Desktop app (easiest)
Double-click **`GTA IV Map Exporter.pyw`** (needs Python 3.10+ with numpy and Blender 4.2+ installed).
It finds your GTA IV and Blender installs, lists every district with its placement count,
checks free disk space, and builds a ready-to-open `.blend` with a progress bar. Textures go in a
`<name>_textures` folder next to the .blend (relative paths, so the two can be moved together).

## Blender add-on
1. Blender: *Edit → Preferences → Add-ons → ▾ → Install from Disk…* and pick `gta4_map_importer.zip`.
2. Enable **GTA IV Map Importer**.

## Use
*File → Import → GTA IV Map (game folder)*

| Option | Meaning |
|---|---|
| GTA IV folder | The folder that contains `GTAIV.exe` (e.g. `…\steamapps\common\Grand Theft Auto IV\GTAIV`) |
| Areas | `*` for everything, or names/wildcards: `manhat*` (Algonquin), `nj_*` (Alderney), `brook_*`, `queens_*`, `bronx_*`, e.g. `manhat01,manhat02` |
| Placement | **Geometry Nodes instances** (default): one point cloud per area that instances shared models, light enough for the whole map. **Separate objects**: one real object per placement, easier to edit, best for a few areas |
| Import LOD models instead | Imports the low-detail city instead |
| Textures | Extracts textures as `.dds` into the texture folder |
| Custom normals | Uses the game's vertex normals |
| Texture folder | Where `.dds` files go. Blank means `gta4_cache` next to the saved .blend (save first!) |

To edit part of a Geometry Nodes area, select its point object and use *Object → Apply → Visual Geometry
to Mesh*, or import that area again in **Separate objects** mode.

## How it works
* `img.py` reads IMG v3 archives. The AES key comes from your own `GTAIV.exe`, checked against a SHA1,
  so no key ships with the add-on.
* `rsc.py` handles RSC05 resources (zlib, system/graphics segments).
* `drawable.py` reads `.wdr` / `.wdd` / `.wft` models (fragment pieces are placed by their bones).
* `texture.py` reads `.wtd` and embedded texture dictionaries and writes them out as DDS.
* `mapdata.py` reads `gta.dat`, `images.txt`, IDE definitions and binary WPL placements.
  Files in `GTAIV/update/` take priority, so Fusion Fix overrides are respected.
* `blender_import.py` builds meshes, materials and placements.

## Not included (yet)
Interiors (MLO), TLAD/TBoGT episode maps, collision, 2dfx lights, water, vehicles and peds.
