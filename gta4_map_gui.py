"""GTA IV Map Exporter - desktop app.

Pick your GTA IV folder and the districts you want, and it builds a .blend
file you can open in Blender. No OpenIV needed.
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

# When frozen by PyInstaller, blender_runner.py and the add-on package are
# bundled as plain files (Blender's own Python imports them from there).
HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APP_NAME = "GTA IV Map Exporter"
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GTA4MapExporter")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

# area prefix -> island / borough name
GROUPS = [
    ("Algonquin", ("manhat",)),
    ("Alderney", ("nj_",)),
    ("Broker", ("brook_",)),
    ("Dukes", ("queens_",)),
    ("Bohan", ("bronx_",)),
    ("Other", ("",)),
]
# measured on a full-map export: ~2.7 GB of textures + 0.85 GB .blend
FULL_MAP_BYTES = 3.6e9
FULL_MAP_PLACEMENTS = 165857


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


def find_blender():
    found = []
    for pat in (r"C:\Program Files\Blender Foundation\Blender*\blender.exe",):
        found += glob.glob(pat)
    for l in steam_libraries():
        p = os.path.join(l, "steamapps", "common", "Blender", "blender.exe")
        if os.path.isfile(p):
            found.append(p)
    w = shutil.which("blender")
    if w:
        found.append(w)

    def ver(p):
        m = re.search(r"Blender\s*(\d+)\.(\d+)", p)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    found.sort(key=ver, reverse=True)
    return found[0] if found else ""


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
        self.minsize(820, 640)
        self.q = queue.Queue()
        self.proc = None
        self.area_counts = {}
        self.area_vars = {}
        self.group_vars = {}
        self.scanned_dir = None
        self.start_time = None

        s = load_settings()
        self.game_dir = tk.StringVar(value=s.get("game_dir") or find_game_dir())
        self.blender = tk.StringVar(value=s.get("blender") or find_blender())
        self.output = tk.StringVar(value=s.get("output") or default_output())
        self.mode = tk.StringVar(value=s.get("mode", "INSTANCES"))
        self.lods = tk.BooleanVar(value=s.get("lods", False))
        self.textures = tk.BooleanVar(value=s.get("textures", True))
        self.normals = tk.BooleanVar(value=s.get("normals", True))
        self._saved_areas = set(s.get("areas", []))

        self._build()
        self.game_dir.trace_add("write", lambda *a: self._check_paths())
        self.blender.trace_add("write", lambda *a: self._check_paths())
        self.output.trace_add("write", lambda *a: self._update_summary())
        self._check_paths()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if os.path.isfile(os.path.join(self.game_dir.get(), "GTAIV.exe")):
            self.after(200, self.scan)

    # ------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 8, "pady": 4}
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
        ttk.Label(root, text="Builds a Blender file of Liberty City straight from your GTA IV install. No OpenIV needed.",
                  foreground="#666").pack(anchor="w", pady=(0, 6))

        # paths
        pf = ttk.LabelFrame(root, text="Locations", padding=6)
        pf.pack(fill="x")
        pf.columnconfigure(1, weight=1)
        self.game_status = self._path_row(pf, 0, "GTA IV folder", self.game_dir, self._browse_game)
        self.blender_status = self._path_row(pf, 1, "Blender", self.blender, self._browse_blender)
        self.output_status = self._path_row(pf, 2, "Save .blend as", self.output, self._browse_output)

        # areas
        af = ttk.LabelFrame(root, text="Districts", padding=6)
        af.pack(fill="both", expand=True, pady=(8, 0))
        bar = ttk.Frame(af)
        bar.pack(fill="x")
        ttk.Button(bar, text="Select all", command=lambda: self._select_all(True)).pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self._select_all(False)).pack(side="left", padx=4)
        self.rescan_btn = ttk.Button(bar, text="Rescan game files", command=self.scan)
        self.rescan_btn.pack(side="left")
        self.summary = ttk.Label(bar, text="")
        self.summary.pack(side="right")

        canvas = tk.Canvas(af, highlightthickness=0, height=260)
        sb = ttk.Scrollbar(af, orient="vertical", command=canvas.yview)
        self.areas_frame = ttk.Frame(canvas)
        self.areas_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.areas_frame, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True, pady=(6, 0))
        sb.pack(side="right", fill="y", pady=(6, 0))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.areas_placeholder = ttk.Label(self.areas_frame, text="Choose your GTA IV folder to list districts.",
                                           foreground="#666")
        self.areas_placeholder.pack(anchor="w", padx=4, pady=8)

        # options
        of = ttk.LabelFrame(root, text="Options", padding=6)
        of.pack(fill="x", pady=(8, 0))
        ttk.Radiobutton(of, text="Geometry Nodes instances (fast; recommended for big exports)",
                        variable=self.mode, value="INSTANCES").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(of, text="Separate objects (editable; best for a few districts)",
                        variable=self.mode, value="OBJECTS").grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(of, text="Textures", variable=self.textures,
                        command=self._update_summary).grid(row=0, column=1, sticky="w", padx=(24, 0))
        ttk.Checkbutton(of, text="Game normals", variable=self.normals).grid(row=1, column=1, sticky="w", padx=(24, 0))
        ttk.Checkbutton(of, text="Low-detail LOD city instead", variable=self.lods,
                        command=self.scan_counts_changed).grid(row=0, column=2, sticky="w", padx=(24, 0))

        # run
        rf = ttk.Frame(root)
        rf.pack(fill="x", pady=(10, 0))
        self.export_btn = ttk.Button(rf, text="Export to Blender", style="Accent.TButton", command=self.export)
        self.export_btn.pack(side="left")
        self.cancel_btn = ttk.Button(rf, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.open_btn = ttk.Button(rf, text="Open in Blender", command=self.open_blend, state="disabled")
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
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=2)
        ttk.Button(parent, text="Browse…", command=browse).grid(row=row, column=2, padx=(6, 0), pady=2)
        st = ttk.Label(parent, text="", width=26)
        st.grid(row=row, column=3, sticky="w", padx=(8, 0))
        return st

    # ------------------------------------------------------------ helpers
    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, label, ok, text):
        label.configure(text=("✔ " if ok else "✖ ") + text, style="Ok.TLabel" if ok else "Bad.TLabel")

    def _check_paths(self):
        gd = self.game_dir.get()
        g_ok = os.path.isfile(os.path.join(gd, "GTAIV.exe"))
        self._set_status(self.game_status, g_ok, "GTAIV.exe found" if g_ok else "GTAIV.exe not found")
        b = self.blender.get()
        b_ok = os.path.isfile(b) and b.lower().endswith(".exe")
        self._set_status(self.blender_status, b_ok, "Blender found" if b_ok else "blender.exe not found")
        self._update_summary()
        return g_ok, b_ok

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

    # ------------------------------------------------------------ scanning
    def scan(self):
        gd = self.game_dir.get()
        if not os.path.isfile(os.path.join(gd, "GTAIV.exe")):
            return
        if self.proc is not None:
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
                self.q.put(("scanned", (gd, counts)))
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
        idx = 1 if self.lods.get() else 0
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
        idx = 1 if self.lods.get() else 0
        n = sum(self.area_counts[a][idx] for a in self._selected())
        frac = min(1.0, n / FULL_MAP_PLACEMENTS)
        size = FULL_MAP_BYTES * (frac ** 0.8)  # textures are shared, so it grows sub-linearly
        if not self.textures.get():
            size *= 0.3
        return n, max(size, 50e6) if n else 0

    def _update_summary(self):
        if not hasattr(self, "summary"):
            return
        n, size = self._estimate()
        out_dir = os.path.dirname(self.output.get()) or "."
        free = None
        probe = out_dir
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            pass
        if self.area_counts:
            self.summary.configure(text="%s placements selected · needs about %s" % ("{:,}".format(n), fmt_bytes(size)))
        if free is None:
            self._set_status(self.output_status, False, "folder not reachable")
        elif size and free < size * 1.2:
            self._set_status(self.output_status, False, "only %s free" % fmt_bytes(free))
        else:
            self._set_status(self.output_status, True, "%s free" % fmt_bytes(free))

    # ------------------------------------------------------------ export
    def export(self):
        g_ok, b_ok = self._check_paths()
        if not g_ok:
            messagebox.showerror(APP_NAME, "Pick the GTA IV folder (the one that contains GTAIV.exe).")
            return
        if not b_ok:
            messagebox.showerror(APP_NAME, "Blender wasn't found. Install Blender 4.2 or newer, or point to blender.exe.")
            return
        areas = self._selected()
        if not areas:
            messagebox.showerror(APP_NAME, "Select at least one district.")
            return
        out = self.output.get().strip()
        if not out.lower().endswith(".blend"):
            out += ".blend"
            self.output.set(out)
        out_dir = os.path.dirname(out)
        n, size = self._estimate()
        try:
            os.makedirs(out_dir, exist_ok=True)
            free = shutil.disk_usage(out_dir).free
        except OSError as e:
            messagebox.showerror(APP_NAME, "Can't use that output folder:\n%s" % e)
            return
        if free < size * 1.2:
            if not messagebox.askyesno(APP_NAME, "This export needs about %s but only %s is free on that drive.\n\n"
                                       "Continue anyway?" % (fmt_bytes(size), fmt_bytes(free))):
                return
        if os.path.exists(out) and not messagebox.askyesno(APP_NAME, "%s already exists. Replace it?" % out):
            return

        self._persist()
        stem = os.path.splitext(os.path.basename(out))[0]
        tmp = os.path.join(out_dir, ".gta4_tmp")
        os.makedirs(tmp, exist_ok=True)
        job = {
            "addon_dir": HERE,
            "game_dir": self.game_dir.get(),
            "cache_dir": os.path.join(out_dir, stem + "_textures"),
            "output": out,
            "areas": ",".join(areas),
            "lods": self.lods.get(),
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
        self._log("Exporting %d districts (%s placements) to %s" % (len(areas), "{:,}".format(n), out))
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                         text=True, encoding="utf-8", errors="replace",
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            messagebox.showerror(APP_NAME, "Couldn't start Blender:\n%s" % e)
            return
        self._busy(True)
        self.start_time = time.time()
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.status.configure(text="Starting Blender…")
        self._result = None
        threading.Thread(target=self._read_proc, args=(self.proc, tmp), daemon=True).start()

    def _read_proc(self, proc, tmp):
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("@@PROGRESS"):
                self.q.put(("progress", float(line.split()[1])))
            elif line.startswith("@@SAVING"):
                self.q.put(("saving", None))
            elif line.startswith("@@DONE"):
                self.q.put(("done", int(line.split()[1])))
            elif line.startswith("@@FAILED"):
                self.q.put(("failed", line[9:]))
            elif line.startswith("[GTA IV]") or "Error" in line or "Traceback" in line or line.startswith("  File"):
                self.q.put(("log", line.replace("[GTA IV] ", "")))
        code = proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
        self.q.put(("exit", code))

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self._log("Cancelled.")

    def _busy(self, busy):
        st = "disabled" if busy else "normal"
        self.export_btn.configure(state=st)
        self.rescan_btn.configure(state=st)
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if busy:
            self.open_btn.configure(state="disabled")

    def _persist(self):
        save_settings({
            "game_dir": self.game_dir.get(), "blender": self.blender.get(), "output": self.output.get(),
            "mode": self.mode.get(), "lods": self.lods.get(), "textures": self.textures.get(),
            "normals": self.normals.get(), "areas": self._selected(),
        })

    def open_blend(self):
        out = self.output.get()
        if os.path.isfile(out):
            subprocess.Popen([self.blender.get(), out])

    def open_folder(self):
        d = os.path.dirname(self.output.get())
        if os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo(APP_NAME, "The folder doesn't exist yet; it is created when you export.")

    def _on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(APP_NAME, "An export is running. Stop it and quit?"):
                return
            self.proc.kill()
        self._persist()
        self.destroy()

    # ------------------------------------------------------------ events
    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._log(val)
                    if self.proc is not None and self.progress["value"] == 0:
                        self.status.configure(text="Reading game archives… (about half a minute)")
                elif kind == "scanned":
                    gd, counts = val
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.rescan_btn.configure(state="normal")
                    self.area_counts = counts
                    self.scanned_dir = gd
                    self._populate_areas(keep_selection=bool(self.area_vars))
                    self.status.configure(text="Found %d districts. Pick what to export." % len(counts))
                elif kind == "scan_failed":
                    self.progress.stop()
                    self.rescan_btn.configure(state="normal")
                    self.status.configure(text="Couldn't read the game files.")
                    self._log("Error: " + val)
                    messagebox.showerror(APP_NAME, "Couldn't read the game files:\n%s" % val)
                elif kind == "progress":
                    self.progress.configure(value=val * 1000)
                    el = time.time() - self.start_time
                    eta = ""
                    if val > 0.05:
                        eta = " · about %s left" % self._dur(el / val - el)
                    phase = "Building models" if val < 0.8 else "Placing objects"
                    self.status.configure(text="%s… %d%%%s" % (phase, val * 100, eta))
                elif kind == "saving":
                    self.status.configure(text="Saving .blend file…")
                elif kind == "done":
                    self._result = val
                elif kind == "failed":
                    self._log("Error: " + val)
                    self._result = ("failed", val)
                elif kind == "exit":
                    self.proc = None
                    self._busy(False)
                    if isinstance(self._result, int):
                        self.progress.configure(value=1000)
                        msg = "Done in %s: %s placements saved to %s" % (
                            self._dur(time.time() - self.start_time), "{:,}".format(self._result), self.output.get())
                        self.status.configure(text=msg)
                        self._log(msg)
                        self.open_btn.configure(state="normal")
                    elif isinstance(self._result, tuple):
                        self.status.configure(text="Export failed: " + self._result[1])
                        messagebox.showerror(APP_NAME, "Export failed:\n%s\n\nSee the log for details." % self._result[1])
                    else:
                        self.status.configure(text="Export stopped (Blender exit code %s)." % val)
                        self.progress.configure(value=0)
        except queue.Empty:
            pass
        self.after(100, self._poll)

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
