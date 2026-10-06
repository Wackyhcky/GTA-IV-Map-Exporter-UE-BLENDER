"""GTA IV Map Exporter - desktop app.

Pick your GTA IV folder and the districts you want, and it builds either a
.blend file for Blender or a level inside an Unreal Engine project.
No OpenIV needed.
"""

import glob
import json
import os
import queue
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# When frozen by PyInstaller, the runner scripts and the add-on package are
# bundled as plain files (Blender's / Unreal's own Python imports them from there).
HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APP_NAME = "GTA IV Map Exporter"
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GTA4MapExporter")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# area prefix -> island / borough name
GROUPS = [
    ("Algonquin", ("manhat",)),
    ("Alderney", ("nj_",)),
    ("Broker", ("brook_",)),
    ("Dukes", ("queens_",)),
    ("Bohan", ("bronx_",)),
    ("Other", ("",)),
]
FULL_MAP_PLACEMENTS = 165857
# measured triangle share of each detail level vs full detail (manhat01 / bronx_e)
DETAIL_SHARE = {"FULL": 1.0, "HIGH": 0.7, "MEDIUM": 0.35, "LOW": 0.17, "VERYLOW": 0.08}
# Unreal import: share of the progress bar for the conversion step, mesh batches
# (150 models each) per editor process, and crashed attempts in a row before giving up
UE_CONVERT_SHARE = 0.3
UE_BATCHES_PER_PASS = 12
UE_MAX_RETRIES = 4
# measured: full map in Blender ~2.7 GB textures + 0.85 GB .blend;
# Unreal ~0.72 GB (export + imported assets) for manhat01, scaled to the full map
FULL_MAP_BYTES = {"BLENDER": 3.6e9, "UNREAL": 9e9}


def group_of(area):
    for name, prefixes in GROUPS:
        if any(area.startswith(p) for p in prefixes):
            return name
    return "Other"


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


# ---------------------------------------------------------------- detection
def steam_libraries():
    roots = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            roots.append(winreg.QueryValueEx(k, "SteamPath")[0])
    except Exception:
        pass
    roots.append(r"C:\Program Files (x86)\Steam")
    libs = []
    for r in roots:
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            try:
                text = open(vdf, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                p = m.group(1).replace("\\\\", "\\")
                if p not in libs:
                    libs.append(p)
    return libs


def find_game_dir():
    cands = [os.path.join(l, "steamapps", "common", "Grand Theft Auto IV", "GTAIV") for l in steam_libraries()]
    cands += [r"C:\Program Files\Rockstar Games\Grand Theft Auto IV",
              r"C:\Program Files (x86)\Rockstar Games\Grand Theft Auto IV",
              r"C:\Program Files\Rockstar Games\Grand Theft Auto IV The Complete Edition"]
    for c in cands:
        if os.path.isfile(os.path.join(c, "GTAIV.exe")):
            return c
    return ""


def _ver(p):
    m = re.search(r"(?:Blender|UE_)\s*(\d+)\.(\d+)", p)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_blender():
    found = glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe")
    for l in steam_libraries():
        p = os.path.join(l, "steamapps", "common", "Blender", "blender.exe")
        if os.path.isfile(p):
            found.append(p)
    w = shutil.which("blender")
    if w:
        found.append(w)
    found.sort(key=_ver, reverse=True)
    return found[0] if found else ""


def unreal_installs():
    """{'5.8': 'C:\\Program Files\\Epic Games\\UE_5.8', ...} from the Epic launcher + common folders."""
    out = {}
    dat = r"C:\ProgramData\Epic\UnrealEngineLauncher\LauncherInstalled.dat"
    try:
        for e in json.load(open(dat, encoding="utf-8")).get("InstallationList", []):
            if e.get("AppName", "").startswith("UE_"):
                out[e["AppName"][3:]] = e["InstallLocation"]
    except Exception:
        pass
    for d in glob.glob(r"C:\Program Files\Epic Games\UE_*") + glob.glob(r"?:\Epic Games\UE_*"):
        out.setdefault(os.path.basename(d)[3:], d)
    return {k: v for k, v in out.items()
            if os.path.isfile(os.path.join(v, "Engine", "Binaries", "Win64", "UnrealEditor.exe"))}


def find_unreal(uproject=None):
    installs = unreal_installs()
    if uproject and os.path.isfile(uproject):
        try:
            assoc = json.load(open(uproject, encoding="utf-8")).get("EngineAssociation", "")
            if assoc in installs:
                return os.path.join(installs[assoc], "Engine", "Binaries", "Win64", "UnrealEditor.exe")
        except Exception:
            pass
    if not installs:
        return ""
    best = max(installs, key=lambda v: _ver("UE_" + v))
    return os.path.join(installs[best], "Engine", "Binaries", "Win64", "UnrealEditor.exe")


def unreal_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
                             capture_output=True, text=True, creationflags=NO_WINDOW).stdout
        return "UnrealEditor.exe" in out
    except Exception:
        return False


def default_output():
    best, best_free = None, -1
    for d in string.ascii_uppercase:
        root = d + ":\\"
        if not os.path.exists(root):
            continue
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        if free > best_free:
            best, best_free = root, free
    base = best or os.path.expanduser("~")
    return os.path.join(base, "GTA IV Blender", "liberty_city.blend")


def load_settings():
    try:
        return json.load(open(SETTINGS_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass  # e.g. system drive full; settings are a convenience


# ---------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(860, 700)
        self.q = queue.Queue()
        self.proc = None
        self.cancelled = False
        self.area_counts = {}
        self.area_vars = {}
        self.group_vars = {}
        self.start_time = None
        self._result = None

        s = load_settings()
        self.target = tk.StringVar(value=s.get("target", "BLENDER"))
        self.game_dir = tk.StringVar(value=s.get("game_dir") or find_game_dir())
        self.blender = tk.StringVar(value=s.get("blender") or find_blender())
        self.output = tk.StringVar(value=s.get("output") or default_output())
        self.uproject = tk.StringVar(value=s.get("uproject", ""))
        self.unreal = tk.StringVar(value=s.get("unreal") or find_unreal(s.get("uproject")))
        self.map_name = tk.StringVar(value=s.get("map_name", "LibertyCity"))
        self.mode = tk.StringVar(value=s.get("mode", "INSTANCES"))
        self.ue_mode = tk.StringVar(value=s.get("ue_mode", "INSTANCES"))
        self.detail = tk.StringVar(value=s.get("detail") or ("LOW" if s.get("lods") else "FULL"))
        self.textures = tk.BooleanVar(value=s.get("textures", True))
        self.normals = tk.BooleanVar(value=s.get("normals", True))
        self.interiors = tk.BooleanVar(value=s.get("interiors", True))
        self.nanite = tk.BooleanVar(value=s.get("nanite", False))
        self.collision = tk.BooleanVar(value=s.get("collision", True))
        self.keep_files = tk.BooleanVar(value=s.get("keep_files", False))
        self._saved_areas = set(s.get("areas", []))

        self._build()
        for v in (self.game_dir, self.blender, self.unreal, self.uproject, self.output, self.map_name):
            v.trace_add("write", lambda *a: self._check_paths())
        self.target.trace_add("write", lambda *a: self._target_changed())
        self._target_changed()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if os.path.isfile(os.path.join(self.game_dir.get(), "GTAIV.exe")):
            self.after(200, self.scan)

    # ------------------------------------------------------------ layout
    def _build(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Head.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Ok.TLabel", foreground="#1a7f37")
        style.configure("Bad.TLabel", foreground="#c62828")
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text=APP_NAME, style="Head.TLabel").pack(anchor="w")
        ttk.Label(root, text="Exports Liberty City straight from your GTA IV install to Blender or Unreal Engine. "
                             "No OpenIV needed.", foreground="#666").pack(anchor="w", pady=(0, 6))

        # paths
        pf = ttk.LabelFrame(root, text="Locations", padding=6)
        pf.pack(fill="x")
        pf.columnconfigure(1, weight=1)
        self.game_status = self._path_row(pf, 0, "GTA IV folder", self.game_dir, self._browse_game)[0]

        tr = ttk.Frame(pf)
        tr.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 2))
        ttk.Label(tr, text="Export to").pack(side="left", padx=(0, 18))
        ttk.Radiobutton(tr, text="Blender (.blend file)", variable=self.target, value="BLENDER").pack(side="left")
        ttk.Radiobutton(tr, text="Unreal Engine (level in a project)", variable=self.target,
                        value="UNREAL").pack(side="left", padx=(16, 0))

        self.blender_rows = []
        st, w = self._path_row(pf, 2, "Blender", self.blender, self._browse_blender)
        self.blender_status = st
        self.blender_rows += w
        st, w = self._path_row(pf, 3, "Save .blend as", self.output, self._browse_output)
        self.output_status = st
        self.blender_rows += w

        self.unreal_rows = []
        st, w = self._path_row(pf, 4, "Unreal project", self.uproject, self._browse_uproject)
        self.uproject_status = st
        self.unreal_rows += w
        st, w = self._path_row(pf, 5, "Unreal Editor", self.unreal, self._browse_unreal)
        self.unreal_status = st
        self.unreal_rows += w
        lbl = ttk.Label(pf, text="Level name")
        lbl.grid(row=6, column=0, sticky="w", padx=(0, 8), pady=2)
        ent = ttk.Entry(pf, textvariable=self.map_name, width=30)
        ent.grid(row=6, column=1, sticky="w", pady=2)
        self.map_hint = ttk.Label(pf, text="", foreground="#666")
        self.map_hint.grid(row=6, column=1, sticky="e", pady=2)
        self.ue_space_status = ttk.Label(pf, text="", width=26)
        self.ue_space_status.grid(row=6, column=3, sticky="w", padx=(8, 0))
        self.unreal_rows += [lbl, ent, self.map_hint, self.ue_space_status]

        # areas
        af = ttk.LabelFrame(root, text="Districts", padding=6)
        af.pack(fill="both", expand=True, pady=(8, 0))
        bar = ttk.Frame(af)
        bar.pack(fill="x")
        ttk.Button(bar, text="Select all", command=lambda: self._select_all(True)).pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self._select_all(False)).pack(side="left", padx=4)
        self.rescan_btn = ttk.Button(bar, text="Rescan game files", command=self.scan)
        self.rescan_btn.pack(side="left")
        from gta4_map_importer.mapdata import DETAIL_LEVELS
        self._detail_labels = {k: label for k, label, _ in DETAIL_LEVELS}
        ttk.Label(bar, text="Detail level").pack(side="left", padx=(18, 6))
        self.detail_box = ttk.Combobox(bar, state="readonly", width=44,
                                       values=[label for _, label, _ in DETAIL_LEVELS])
        self.detail_box.set(self._detail_labels.get(self.detail.get(), DETAIL_LEVELS[0][1]))
        self.detail_box.bind("<<ComboboxSelected>>", self._detail_changed)
        self.detail_box.pack(side="left")
        ttk.Checkbutton(bar, text="Interiors", variable=self.interiors,
                        command=self._update_summary).pack(side="left", padx=(12, 0))
        self.summary = ttk.Label(bar, text="")
        self.summary.pack(side="right")

        canvas = tk.Canvas(af, highlightthickness=0, height=230)
        sb = ttk.Scrollbar(af, orient="vertical", command=canvas.yview)
        self.areas_frame = ttk.Frame(canvas)
        self.areas_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.areas_frame, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True, pady=(6, 0))
        sb.pack(side="right", fill="y", pady=(6, 0))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        ttk.Label(self.areas_frame, text="Choose your GTA IV folder to list districts.",
                  foreground="#666").pack(anchor="w", padx=4, pady=8)

        # options
        self.opt_holder = ttk.Frame(root)
        self.opt_holder.pack(fill="x", pady=(8, 0))
        self.blender_opts = ttk.LabelFrame(self.opt_holder, text="Blender options", padding=6)
        ttk.Radiobutton(self.blender_opts, text="Geometry Nodes instances (fast; recommended for big exports)",
                        variable=self.mode, value="INSTANCES").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(self.blender_opts, text="Separate objects (editable; best for a few districts)",
                        variable=self.mode, value="OBJECTS").grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(self.blender_opts, text="Textures", variable=self.textures,
                        command=self._update_summary).grid(row=0, column=1, sticky="w", padx=(24, 0))
        ttk.Checkbutton(self.blender_opts, text="Game normals", variable=self.normals).grid(
            row=1, column=1, sticky="w", padx=(24, 0))

        self.unreal_opts = ttk.LabelFrame(self.opt_holder, text="Unreal options", padding=6)
        ttk.Radiobutton(self.unreal_opts, text="Instanced meshes, one actor per district (fast; recommended)",
                        variable=self.ue_mode, value="INSTANCES").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(self.unreal_opts, text="Separate actors (editable; best for a few districts)",
                        variable=self.ue_mode, value="ACTORS").grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(self.unreal_opts, text="Textures", variable=self.textures,
                        command=self._update_summary).grid(row=0, column=1, sticky="w", padx=(24, 0))
        ttk.Checkbutton(self.unreal_opts, text="Collision (walkable)", variable=self.collision).grid(
            row=1, column=1, sticky="w", padx=(24, 0))
        ttk.Checkbutton(self.unreal_opts, text="Nanite", variable=self.nanite).grid(
            row=0, column=2, sticky="w", padx=(24, 0))
        ttk.Checkbutton(self.unreal_opts, text="Keep intermediate files", variable=self.keep_files).grid(
            row=0, column=3, sticky="w", padx=(24, 0))

        # run
        rf = ttk.Frame(root)
        rf.pack(fill="x", pady=(10, 0))
        self.export_btn = ttk.Button(rf, text="Export", style="Accent.TButton", command=self.export)
        self.export_btn.pack(side="left")
        self.cancel_btn = ttk.Button(rf, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.open_btn = ttk.Button(rf, text="Open result", command=self.open_result, state="disabled")
        self.open_btn.pack(side="right")
        self.folder_btn = ttk.Button(rf, text="Open folder", command=self.open_folder)
        self.folder_btn.pack(side="right", padx=4)

        self.progress = ttk.Progressbar(root, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=(8, 2))
        self.status = ttk.Label(root, text="Ready.")
        self.status.pack(anchor="w")

        lf = ttk.Frame(root)
        lf.pack(fill="both", expand=False, pady=(4, 0))
        self.log = tk.Text(lf, height=7, wrap="word", font=("Consolas", 9), state="disabled",
                           background="#f6f6f6", relief="flat")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    def _path_row(self, parent, row, label, var, browse):
        lbl = ttk.Label(parent, text=label)
        lbl.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        ent = ttk.Entry(parent, textvariable=var)
        ent.grid(row=row, column=1, sticky="ew", pady=2)
        btn = ttk.Button(parent, text="Browse…", command=browse)
        btn.grid(row=row, column=2, padx=(6, 0), pady=2)
        st = ttk.Label(parent, text="", width=26)
        st.grid(row=row, column=3, sticky="w", padx=(8, 0))
        return st, [lbl, ent, btn, st]

    def _target_changed(self):
        ue = self.target.get() == "UNREAL"
        for w in self.blender_rows:
            w.grid_remove() if ue else w.grid()
        for w in self.unreal_rows:
            w.grid() if ue else w.grid_remove()
        self.blender_opts.pack_forget()
        self.unreal_opts.pack_forget()
        (self.unreal_opts if ue else self.blender_opts).pack(fill="x")
        self.export_btn.configure(text="Export to Unreal" if ue else "Export to Blender")
        self.open_btn.configure(text="Open in Unreal" if ue else "Open in Blender",
                                state="normal" if (ue and os.path.isfile(self._map_file())) else "disabled")
        self._check_paths()

    # ------------------------------------------------------------ helpers
    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, label, ok, text):
        label.configure(text=("✔ " if ok else "✖ ") + text, style="Ok.TLabel" if ok else "Bad.TLabel")

    def _map_name_ok(self):
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.map_name.get().strip()))

    def _check_paths(self):
        if not hasattr(self, "summary"):
            return False, False
        gd = self.game_dir.get()
        g_ok = os.path.isfile(os.path.join(gd, "GTAIV.exe"))
        self._set_status(self.game_status, g_ok, "GTAIV.exe found" if g_ok else "GTAIV.exe not found")
        if self.target.get() == "UNREAL":
            up = self.uproject.get()
            p_ok = up.lower().endswith(".uproject") and os.path.isfile(up)
            self._set_status(self.uproject_status, p_ok, "project found" if p_ok else "pick a .uproject")
            ue = self.unreal.get()
            u_ok = os.path.isfile(ue) and os.path.isfile(self._unreal_cmd())
            self._set_status(self.unreal_status, u_ok, "Unreal found" if u_ok else "UnrealEditor.exe not found")
            self.map_hint.configure(text="→ /Game/GTAIV/Maps/%s" % self.map_name.get().strip()
                                    if self._map_name_ok() else "letters, digits and _ only")
            ok2 = p_ok and u_ok and self._map_name_ok()
        else:
            b = self.blender.get()
            ok2 = os.path.isfile(b) and b.lower().endswith(".exe")
            self._set_status(self.blender_status, ok2, "Blender found" if ok2 else "blender.exe not found")
        self._update_summary()
        return g_ok, ok2

    def _unreal_cmd(self):
        ue = self.unreal.get()
        return os.path.join(os.path.dirname(ue), "UnrealEditor-Cmd.exe")

    def _out_dir(self):
        if self.target.get() == "UNREAL":
            return os.path.dirname(self.uproject.get())
        return os.path.dirname(self.output.get())

    def _browse_game(self):
        d = filedialog.askdirectory(title="Folder that contains GTAIV.exe", initialdir=self.game_dir.get() or None)
        if d:
            self.game_dir.set(os.path.normpath(d))
            self.scan()

    def _browse_blender(self):
        f = filedialog.askopenfilename(title="Find blender.exe", filetypes=[("Blender", "blender.exe"), ("Programs", "*.exe")])
        if f:
            self.blender.set(os.path.normpath(f))

    def _browse_output(self):
        cur = self.output.get()
        f = filedialog.asksaveasfilename(title="Save Blender file as", defaultextension=".blend",
                                         filetypes=[("Blender file", "*.blend")],
                                         initialdir=os.path.dirname(cur) if cur else None,
                                         initialfile=os.path.basename(cur) if cur else "liberty_city.blend")
        if f:
            self.output.set(os.path.normpath(f))

    def _browse_uproject(self):
        cur = self.uproject.get()
        f = filedialog.askopenfilename(title="Unreal project to import into",
                                       filetypes=[("Unreal project", "*.uproject")],
                                       initialdir=os.path.dirname(cur) if cur else None)
        if f:
            self.uproject.set(os.path.normpath(f))
            ue = find_unreal(f)
            if ue:
                self.unreal.set(ue)

    def _browse_unreal(self):
        f = filedialog.askopenfilename(title="Find UnrealEditor.exe (Engine\\Binaries\\Win64)",
                                       filetypes=[("Unreal Editor", "UnrealEditor.exe"), ("Programs", "*.exe")])
        if f:
            self.unreal.set(os.path.normpath(f))

    # ------------------------------------------------------------ scanning
    def scan(self):
        gd = self.game_dir.get()
        if not os.path.isfile(os.path.join(gd, "GTAIV.exe")) or self._running():
            return
        self.rescan_btn.configure(state="disabled")
        self.status.configure(text="Reading game files…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def work():
            try:
                from gta4_map_importer import mapdata
                data = mapdata.GameData(gd, log=lambda m: self.q.put(("log", m)),
                                        key_cache=os.path.join(SETTINGS_DIR, "gta4_key.bin"))
                data.load_definitions()
                data.open_archives()
                areas = data.load_instances()
                counts = {}
                for a, insts in areas.items():
                    hd = sum(1 for i in insts if not i.is_lod and i.hash in data.models)
                    lod = sum(1 for i in insts if i.is_lod and i.hash in data.models)
                    if hd or lod:
                        counts[a] = (hd, lod)
                for _, arc in data.archives:
                    arc.close()
                self.q.put(("scanned", counts))
            except Exception as e:
                self.q.put(("scan_failed", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def scan_counts_changed(self):
        if self.area_counts:
            self._populate_areas(keep_selection=True)

    def _populate_areas(self, keep_selection=False):
        selected = {a for a, v in self.area_vars.items() if v.get()} if keep_selection else None
        for w in self.areas_frame.winfo_children():
            w.destroy()
        self.area_vars.clear()
        self.group_vars.clear()
        idx = 0  # full-detail counts; the detail level is applied at export time
        grouped = {}
        for a in sorted(self.area_counts, key=natural_key):
            grouped.setdefault(group_of(a), []).append(a)
        if selected is None:
            selected = self._saved_areas & set(self.area_counts) or set(self.area_counts)
        for gname, _ in GROUPS:
            areas = [a for a in grouped.get(gname, []) if self.area_counts[a][idx] > 0]
            if not areas:
                continue
            total = sum(self.area_counts[a][idx] for a in areas)
            gv = tk.BooleanVar()
            self.group_vars[gname] = (gv, areas)
            box = ttk.Frame(self.areas_frame)
            box.pack(fill="x", anchor="w", pady=(2, 6))
            ttk.Checkbutton(box, text="%s  ·  %s placements" % (gname, "{:,}".format(total)), variable=gv,
                            command=lambda g=gname: self._toggle_group(g)).grid(row=0, column=0, columnspan=6, sticky="w")
            for i, a in enumerate(areas):
                v = tk.BooleanVar(value=a in selected)
                self.area_vars[a] = v
                ttk.Checkbutton(box, text="%s (%s)" % (a, "{:,}".format(self.area_counts[a][idx])), variable=v,
                                command=self._area_changed).grid(row=1 + i // 5, column=i % 5, sticky="w",
                                                                  padx=(22 if i % 5 == 0 else 6, 6))
        self._area_changed()

    def _detail_changed(self, _event=None):
        label = self.detail_box.get()
        for k, v in self._detail_labels.items():
            if v == label:
                self.detail.set(k)
        self._update_summary()

    def _toggle_group(self, g):
        gv, areas = self.group_vars[g]
        for a in areas:
            self.area_vars[a].set(gv.get())
        self._area_changed()

    def _select_all(self, value):
        for v in self.area_vars.values():
            v.set(value)
        self._area_changed()

    def _area_changed(self):
        for gv, areas in self.group_vars.values():
            gv.set(all(self.area_vars[a].get() for a in areas))
        self._update_summary()

    def _selected(self):
        return [a for a, v in self.area_vars.items() if v.get()]

    def _estimate(self):
        n = sum(self.area_counts[a][0] for a in self._selected())
        frac = min(1.0, n / FULL_MAP_PLACEMENTS)
        size = FULL_MAP_BYTES[self.target.get()] * (frac ** 0.8)  # textures are shared: grows sub-linearly
        size *= 0.3 + 0.7 * DETAIL_SHARE.get(self.detail.get(), 1.0)
        if not self.textures.get():
            size *= 0.3
        return n, max(size, 50e6) if n else 0

    def _free_space(self, path):
        probe = path or "."
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return None

    def _update_summary(self):
        if not hasattr(self, "summary"):
            return
        n, size = self._estimate()
        free = self._free_space(self._out_dir())
        if self.area_counts:
            share = DETAIL_SHARE.get(self.detail.get(), 1.0)
            detail = "" if share == 1.0 else " · about %d%% of the triangles" % round(share * 100)
            self.summary.configure(text="%s placements selected%s · needs about %s"
                                   % ("{:,}".format(n), detail, fmt_bytes(size)))
        label = self.ue_space_status if self.target.get() == "UNREAL" else self.output_status
        if free is None:
            self._set_status(label, False, "folder not reachable")
        elif size and free < size * 1.2:
            self._set_status(label, False, "only %s free" % fmt_bytes(free))
        else:
            self._set_status(label, True, "%s free" % fmt_bytes(free))

    # ------------------------------------------------------------ export
    def _running(self):
        return self.proc is not None or getattr(self, "_worker_busy", False)

    def export(self):
        g_ok, t_ok = self._check_paths()
        ue = self.target.get() == "UNREAL"
        if not g_ok:
            messagebox.showerror(APP_NAME, "Pick the GTA IV folder (the one that contains GTAIV.exe).")
            return
        if not t_ok:
            messagebox.showerror(APP_NAME, "Pick an Unreal project (.uproject), the Unreal Editor and a valid level name."
                                 if ue else "Blender wasn't found. Install Blender 4.2 or newer, or point to blender.exe.")
            return
        areas = self._selected()
        if not areas:
            messagebox.showerror(APP_NAME, "Select at least one district.")
            return
        n, size = self._estimate()
        out_dir = self._out_dir()
        if not ue:
            out = self.output.get().strip()
            if not out.lower().endswith(".blend"):
                out += ".blend"
                self.output.set(out)
            out_dir = os.path.dirname(out)
        try:
            os.makedirs(out_dir, exist_ok=True)
            free = shutil.disk_usage(out_dir).free
        except OSError as e:
            messagebox.showerror(APP_NAME, "Can't use that folder:\n%s" % e)
            return
        if free < size * 1.2 and not messagebox.askyesno(
                APP_NAME, "This export needs about %s but only %s is free on that drive.\n\nContinue anyway?"
                % (fmt_bytes(size), fmt_bytes(free))):
            return
        if ue:
            if unreal_running() and not messagebox.askyesno(
                    APP_NAME, "Unreal Editor is running.\n\nClose it first if it has this project open, "
                              "otherwise the import can't save its files.\n\nContinue anyway?"):
                return
            mp = os.path.join(out_dir, "Content", "GTAIV", "Maps", self.map_name.get().strip() + ".umap")
            if os.path.exists(mp) and not messagebox.askyesno(
                    APP_NAME, "The level %s already exists in this project. Replace its contents?"
                    % self.map_name.get().strip()):
                return
        elif os.path.exists(out) and not messagebox.askyesno(APP_NAME, "%s already exists. Replace it?" % out):
            return

        self._persist()
        self._result = None
        self.cancelled = False
        self.start_time = time.time()
        self._busy(True)
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._log("Exporting %d districts (%s placements) to %s"
                  % (len(areas), "{:,}".format(n), "Unreal" if ue else out))
        if ue:
            self._export_unreal(areas)
        else:
            self._export_blender(areas, out)

    # Blender: one Blender process does everything
    def _export_blender(self, areas, out):
        out_dir = os.path.dirname(out)
        stem = os.path.splitext(os.path.basename(out))[0]
        tmp = os.path.join(out_dir, ".gta4_tmp")
        os.makedirs(tmp, exist_ok=True)
        job = {
            "addon_dir": HERE,
            "game_dir": self.game_dir.get(),
            "cache_dir": os.path.join(out_dir, stem + "_textures"),
            "output": out,
            "areas": ",".join(areas),
            "lods": False,
            "detail": self.detail.get(),
            "interiors": self.interiors.get(),
            "mode": self.mode.get(),
            "textures": self.textures.get(),
            "normals": self.normals.get(),
            "key_cache": os.path.join(SETTINGS_DIR, "gta4_key.bin"),
        }
        job_path = os.path.join(tmp, "job.json")
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump(job, f)
        env = dict(os.environ, TEMP=tmp, TMP=tmp)
        cmd = [self.blender.get(), "-b", "--factory-startup", "--python",
               os.path.join(HERE, "blender_runner.py"), "--", job_path]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        except OSError as e:
            self._busy(False)
            messagebox.showerror(APP_NAME, "Couldn't start Blender:\n%s" % e)
            return
        self.status.configure(text="Starting Blender…")

        def read():
            for line in self.proc.stdout:
                self._handle_line(line.rstrip(), 0.0, 1.0)
            code = self.proc.wait()
            shutil.rmtree(tmp, ignore_errors=True)
            self.q.put(("exit", code))
        threading.Thread(target=read, daemon=True).start()

    # Unreal: export files here (no Blender needed), then a headless editor imports them
    def _export_unreal(self, areas):
        project_dir = os.path.dirname(self.uproject.get())
        export_dir = os.path.join(project_dir, "Saved", "GTAIV_Export")
        self._worker_busy = True
        self.status.configure(text="Reading game archives…")

        def work():
            try:
                self._convert_for_unreal(areas, export_dir)
                if self.cancelled:
                    raise RuntimeError("cancelled")
                self._run_unreal_passes(project_dir, export_dir)
                if not self.keep_files.get() and isinstance(self._result, int):
                    shutil.rmtree(export_dir, ignore_errors=True)
                self.q.put(("exit", 0))
            except Exception as e:
                if not self.cancelled:
                    self.q.put(("failed", str(e)))
                self.q.put(("exit", -1))
            finally:
                self._worker_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _convert_for_unreal(self, areas, export_dir):
        """Step 1 (no Unreal needed): game files -> glTF + PNG + JSON. Reused when a previous
        run with the same selection didn't finish, so a retry goes straight to importing."""
        from gta4_map_importer.unreal_export import UnrealExporter
        sig = {"areas": sorted(areas), "detail": self.detail.get(), "interiors": self.interiors.get(),
               "textures": self.textures.get(), "v": 4}
        sig_path = os.path.join(export_dir, "selection.json")
        try:
            if (json.load(open(sig_path, encoding="utf-8")) == sig
                    and os.path.isfile(os.path.join(export_dir, "gta4_unreal.json"))):
                self.q.put(("log", "Reusing converted files from the previous unfinished run"))
                return
        except Exception:
            pass
        shutil.rmtree(export_dir, ignore_errors=True)
        os.makedirs(export_dir, exist_ok=True)
        n = UnrealExporter(self.game_dir.get(), export_dir, ",".join(areas), False,
                           self.textures.get(), key_cache=os.path.join(SETTINGS_DIR, "gta4_key.bin"),
                           log=lambda m: self.q.put(("log", m)), detail=self.detail.get(),
                           interiors=self.interiors.get()).run(
            progress=lambda f: self.q.put(("progress", (UE_CONVERT_SHARE * f, "Converting models"))))
        with open(sig_path, "w", encoding="utf-8") as f:
            json.dump(sig, f)
        self.q.put(("log", "Converted %s placements" % "{:,}".format(n)))

    def _run_unreal_passes(self, project_dir, export_dir):
        """Step 2: import with headless Unreal, a few mesh batches per editor process.

        A single long-running editor runs out of memory on big exports (its memory
        only grows while importing). Each pass resumes where the previous one
        stopped, and a crashed pass is retried with smaller chunks."""
        job_path = os.path.join(export_dir, "job.json")
        chunk = UE_BATCHES_PER_PASS
        failures = 0
        pass_no = 0
        first = True
        while not self.cancelled:
            pass_no += 1
            job = {"export_dir": export_dir, "mode": self.ue_mode.get(), "nanite": self.nanite.get(),
                   "collision": self.collision.get(), "map_name": self.map_name.get().strip(),
                   "stage": "all", "first_pass": first, "max_batches": chunk}
            with open(job_path, "w", encoding="utf-8") as f:
                json.dump(job, f)
            log_path = os.path.join(export_dir, "unreal_pass_%02d.log" % pass_no)
            self._pass = {"more": None, "failed": None}
            self.q.put(("log", "Unreal pass %d (up to %d mesh batches)…" % (pass_no, chunk)))
            if pass_no == 1:
                self.q.put(("progress", (UE_CONVERT_SHARE, "Starting Unreal Editor")))
            started = time.time()
            code = self._run_unreal_once(project_dir, job_path, log_path)
            if self.cancelled:
                return
            if isinstance(self._result, int):
                return  # level built
            if self._pass["failed"]:
                raise RuntimeError(self._pass["failed"])  # a script error: retrying won't help
            first = False
            if self._pass["more"] is not None:
                failures = 0
                chunk = min(UE_BATCHES_PER_PASS, chunk * 2)  # recover after a crash shrank it
                continue
            # the editor died without reporting back: a crash, most often out of memory
            reason = self._crash_reason(project_dir, started) or "Unreal exited unexpectedly (code %s)" % code
            failures += 1
            self.q.put(("log", "Unreal stopped: %s" % reason))
            if failures >= UE_MAX_RETRIES:
                raise RuntimeError("%s.\n\nGave up after %d attempts in a row. Meshes imported so far are "
                                   "kept, so exporting again resumes where it stopped." % (reason, failures))
            chunk = max(1, chunk // 2)
            self.q.put(("log", "Retrying from where it stopped (attempt %d of %d)…"
                        % (failures + 1, UE_MAX_RETRIES)))

    def _run_unreal_once(self, project_dir, job_path, log_path):
        cmd = ('"%s" "%s" -run=pythonscript -script="%s" -unattended -nop4 -nosplash -NoSound '
               '-EnablePlugins=PythonScriptPlugin,EditorScriptingUtilities -abslog="%s"'
               % (self._unreal_cmd(), self.uproject.get(), os.path.join(HERE, "unreal_runner.py"), log_path))
        env = dict(os.environ, GTA4_UE_JOB=job_path)
        # keep Unreal's derived-data cache with the project (the system drive may be full)
        env["UE-LocalDataCachePath"] = os.path.join(project_dir, "DerivedDataCache")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env=env, creationflags=NO_WINDOW)
        self._tail_log(log_path, self.proc)
        code = self.proc.wait()
        time.sleep(0.5)
        self._tail_log(log_path, None)
        self.proc = None
        return code

    @staticmethod
    def _crash_reason(project_dir, since):
        crashes = os.path.join(project_dir, "Saved", "Crashes")
        try:
            dirs = [os.path.join(crashes, d) for d in os.listdir(crashes)]
            dirs = [d for d in dirs if os.path.getmtime(d) >= since - 5]
        except OSError:
            return None
        if not dirs:
            return None
        try:
            text = open(os.path.join(max(dirs, key=os.path.getmtime), "CrashContext.runtime-xml"),
                        encoding="utf-8", errors="replace").read()
        except OSError:
            return "Unreal crashed"
        ctype = re.search(r"<CrashType>(.*?)</CrashType>", text)
        if ctype and "OutOfMemory" in ctype.group(1):
            return "Unreal ran out of memory"
        msg = re.search(r"<ErrorMessage>(.*?)</ErrorMessage>", text, re.S)
        return "Unreal crashed: " + (msg.group(1).strip()[:200] if msg else (ctype.group(1) if ctype else "unknown"))

    def _tail_log(self, path, proc):
        pos = getattr(self, "_tail_pos", 0) if proc is None else 0
        buf = ""
        while True:
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                buf += chunk
                *lines, buf = buf.split("\n")
                for line in lines:
                    m = re.search(r"LogPython: (?:Display: )?(@@.*)$", line)
                    if m:
                        self._handle_line(m.group(1).rstrip(), UE_CONVERT_SHARE, 1.0)
                    elif "LogPython: Error" in line:
                        self.q.put(("log", line.split("LogPython: Error: ", 1)[-1].rstrip()))
            self._tail_pos = pos
            if proc is None or proc.poll() is not None:
                return
            time.sleep(0.5)

    def _handle_line(self, line, p0, p1):
        if line.startswith("@@PROGRESS"):
            f = float(line.split()[1])
            phase = ("Importing into Unreal" if p0 > 0 else
                     ("Building models" if f < 0.8 else "Placing objects"))
            self.q.put(("progress", (p0 + (p1 - p0) * f, phase)))
        elif line.startswith("@@SAVING"):
            self.q.put(("phase", "Saving .blend file…"))
        elif line.startswith("@@DONE"):
            self._result = int(line.split()[1])
        elif line.startswith("@@FAILED"):
            if p0 > 0:
                self._pass["failed"] = line[9:]  # the Unreal pass loop reports it
            else:
                self.q.put(("failed", line[9:]))
        elif line.startswith("@@MORE"):
            self._pass["more"] = int(line.split()[1])
        elif line.startswith("@@LOG"):
            self.q.put(("log", line[6:]))
        elif line.startswith("[GTA IV]") or "Error" in line or "Traceback" in line or line.startswith("  File"):
            self.q.put(("log", line.replace("[GTA IV] ", "")))

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self._log("Cancelling…")

    def _busy(self, busy):
        st = "disabled" if busy else "normal"
        self.export_btn.configure(state=st)
        self.rescan_btn.configure(state=st)
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if busy:
            self.open_btn.configure(state="disabled")

    def _persist(self):
        save_settings({
            "target": self.target.get(), "game_dir": self.game_dir.get(), "blender": self.blender.get(),
            "output": self.output.get(), "uproject": self.uproject.get(), "unreal": self.unreal.get(),
            "map_name": self.map_name.get(), "mode": self.mode.get(), "ue_mode": self.ue_mode.get(),
            "detail": self.detail.get(), "interiors": self.interiors.get(),
            "textures": self.textures.get(), "normals": self.normals.get(),
            "nanite": self.nanite.get(), "collision": self.collision.get(), "keep_files": self.keep_files.get(),
            "areas": self._selected(),
        })

    def open_result(self):
        if self.target.get() == "UNREAL":
            if unreal_running():
                messagebox.showinfo(APP_NAME, "Unreal Editor is already running. Open the level "
                                    "Content/GTAIV/Maps/%s from the Content Browser." % self.map_name.get().strip())
                return
            subprocess.Popen([self.unreal.get(), self.uproject.get(),
                              "/Game/GTAIV/Maps/%s" % self.map_name.get().strip()])
        else:
            out = self.output.get()
            if os.path.isfile(out):
                subprocess.Popen([self.blender.get(), out])

    def open_folder(self):
        d = self._out_dir()
        if self.target.get() == "UNREAL":
            c = os.path.join(d, "Content", "GTAIV")
            d = c if os.path.isdir(c) else d
        if os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo(APP_NAME, "The folder doesn't exist yet; it is created when you export.")

    def _on_close(self):
        if self._running():
            if not messagebox.askyesno(APP_NAME, "An export is running. Stop it and quit?"):
                return
            self.cancel()
        self._persist()
        self.destroy()

    # ------------------------------------------------------------ events
    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._log(val)
                    if self._running() and self.progress["value"] == 0:
                        self.status.configure(text="Reading game archives… (about half a minute)")
                elif kind == "scanned":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.rescan_btn.configure(state="normal")
                    self.area_counts = val
                    self._populate_areas(keep_selection=bool(self.area_vars))
                    self.status.configure(text="Found %d districts. Pick what to export." % len(val))
                elif kind == "scan_failed":
                    self.progress.stop()
                    self.rescan_btn.configure(state="normal")
                    self.status.configure(text="Couldn't read the game files.")
                    self._log("Error: " + val)
                    messagebox.showerror(APP_NAME, "Couldn't read the game files:\n%s" % val)
                elif kind == "progress":
                    f, phase = val
                    self.progress.configure(value=f * 1000)
                    el = time.time() - self.start_time
                    eta = " · about %s left" % self._dur(el / f - el) if f > 0.05 else ""
                    self.status.configure(text="%s… %d%%%s" % (phase, f * 100, eta))
                elif kind == "phase":
                    self.status.configure(text=val)
                elif kind == "failed":
                    self._log("Error: " + val)
                    self._result = ("failed", val)
                elif kind == "exit":
                    self.proc = None
                    self._worker_busy = False
                    self._busy(False)
                    self._finish(val)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _finish(self, code):
        ue = self.target.get() == "UNREAL"
        if isinstance(self._result, int):
            self.progress.configure(value=1000)
            where = ("level /Game/GTAIV/Maps/%s in %s" % (self.map_name.get().strip(),
                                                         os.path.basename(self.uproject.get()))
                     if ue else self.output.get())
            msg = "Done in %s: %s placements saved to %s" % (
                self._dur(time.time() - self.start_time), "{:,}".format(self._result), where)
            self.status.configure(text=msg)
            self._log(msg)
            self.open_btn.configure(state="normal")
        elif self.cancelled:
            self.status.configure(text="Cancelled.")
            self.progress.configure(value=0)
        elif isinstance(self._result, tuple):
            self.status.configure(text="Export failed: " + self._result[1])
            messagebox.showerror(APP_NAME, "Export failed:\n%s\n\nSee the log for details." % self._result[1])
        else:
            self.status.configure(text="Export stopped (exit code %s)." % code)
            self.progress.configure(value=0)
        if ue and not isinstance(self._result, int) and os.path.isfile(self._map_file()):
            self.open_btn.configure(state="normal")  # a level from an earlier run can still be opened

    def _map_file(self):
        return os.path.join(os.path.dirname(self.uproject.get()), "Content", "GTAIV", "Maps",
                            self.map_name.get().strip() + ".umap")

    @staticmethod
    def _dur(s):
        s = int(max(0, s))
        return "%dm %02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
