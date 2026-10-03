# Double-click to start the GTA IV Map Exporter (no console window).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gta4_map_gui  # noqa: E402

gta4_map_gui.main()
