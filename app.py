"""
PDF to Data - a personal Windows desktop GUI for the `opendataloader-pdf` CLI.

This app does NOT parse PDFs itself. It builds command lines and shells out to:
  * opendataloader-pdf         (base client)
  * opendataloader-pdf-hybrid  (local AI server, for --hybrid docling-fast)

Standard library only, except for the optional `tkinterdnd2` drag-and-drop extra.

Run:  python app.py
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------------------
# Optional drag-and-drop support. The app is fully usable without it (Browse... buttons).
# --------------------------------------------------------------------------------------
try:  # pragma: no cover - depends on local install
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAVE_DND = True
except Exception:  # ImportError, TclError on a broken install, ...
    DND_FILES = None
    TkinterDnD = None
    HAVE_DND = False


import updater

APP_NAME = "PDF to Data"
APP_DIR = Path(__file__).resolve().parent

# Where the app is installed vs. where the user's data lives. These are
# deliberately different: an update replaces files in INSTALL_DIR and must never
# be able to touch settings or logs.
INSTALL_DIR = updater.install_dir()
USER_DATA_DIR = updater.user_data_dir()
SETTINGS_PATH = USER_DATA_DIR / "settings.json"
BUNDLED_SCRIPTS = updater.runtime_scripts()


def _migrate_legacy_settings():
    """First run after upgrading: copy a settings.json that sits next to an older
    exe into %APPDATA%. Copy, never move - the old install stays usable."""
    try:
        if SETTINGS_PATH.exists():
            return
    except OSError:
        return
    for legacy in (INSTALL_DIR / "settings.json", APP_DIR / "settings.json"):
        try:
            if legacy.is_file() and legacy.resolve() != SETTINGS_PATH.resolve():
                SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy, SETTINGS_PATH)
                return
        except OSError:
            pass


_migrate_legacy_settings()

ADOPTIUM_URL = "https://adoptium.net/temurin/releases/?version=11"
PIP_BASE = 'pip install opendataloader-pdf'
PIP_HYBRID = 'pip install "opendataloader-pdf[hybrid]"'

IS_WINDOWS = os.name == "nt"
# Keep console windows from flashing up for every subprocess we spawn.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _creationflags() -> int:
    if IS_WINDOWS:
        return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    return 0


# --------------------------------------------------------------------------------------
# Tooltips
# --------------------------------------------------------------------------------------
class Tooltip:
    """Hover tooltip: appears after `delay` ms, word-wraps, auto-hides."""

    def __init__(self, widget, text, delay=450, wraplength=380, autohide=15000):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.wraplength = wraplength
        self.autohide = autohide
        self._after_show = None
        self._after_hide = None
        self._tip = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")
        widget.bind("<Destroy>", self._on_leave, add="+")

    def _on_enter(self, _event=None):
        self._cancel_show()
        self._after_show = self.widget.after(self.delay, self._show)

    def _on_leave(self, _event=None):
        self._cancel_show()
        self._hide()

    def _cancel_show(self):
        if self._after_show is not None:
            try:
                self.widget.after_cancel(self._after_show)
            except Exception:
                pass
            self._after_show = None

    def _show(self):
        self._after_show = None
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except Exception:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        try:
            tip.attributes("-topmost", True)
        except Exception:
            pass
        label = tk.Label(
            tip,
            text=self.text,
            justify="left",
            wraplength=self.wraplength,
            background="#ffffe0",
            foreground="#1a1a1a",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=5,
            font=("Segoe UI", 9),
        )
        label.pack()
        self._tip = tip
        self._after_hide = self.widget.after(self.autohide, self._hide)

    def _hide(self):
        if self._after_hide is not None:
            try:
                self.widget.after_cancel(self._after_hide)
            except Exception:
                pass
            self._after_hide = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


def tip(widget, text):
    """Attach a tooltip and return the widget (so it can be used inline)."""
    Tooltip(widget, text)
    return widget


# --------------------------------------------------------------------------------------
# Locating the CLI executables
# --------------------------------------------------------------------------------------
def _candidate_scripts_dirs():
    """Plausible Scripts/bin directories, most specific first."""
    dirs = []

    def add(p):
        if p:
            p = Path(p)
            if p not in dirs:
                dirs.append(p)

    sub = "Scripts" if IS_WINDOWS else "bin"

    # 0. The runtime bundled with an installed copy of the app always wins, so a
    #    shipped build never accidentally picks up some other Python on the box.
    add(BUNDLED_SCRIPTS)
    # 1. The interpreter running this GUI (its venv, if any).
    add(Path(sys.prefix) / sub)
    add(Path(sys.executable).resolve().parent)
    # 2. An activated virtual environment.
    add(os.environ.get("VIRTUAL_ENV") and Path(os.environ["VIRTUAL_ENV"]) / sub)
    # 3. The base interpreter behind a venv.
    base = getattr(sys, "base_prefix", None)
    if base and base != sys.prefix:
        add(Path(base) / sub)
    # 4. Common personal-venv spots, so the app still works when launched with a
    #    different interpreter than the one that has opendataloader-pdf installed.
    for root in (APP_DIR, APP_DIR.parent, Path.cwd(), Path.home()):
        for name in (".venv", "venv", "env"):
            add(root / name / sub)
    return dirs


def _exe_names(stem):
    return [stem + ".exe", stem] if IS_WINDOWS else [stem]


def find_cli(stem, override=""):
    """Locate a CLI executable. Returns an absolute path string, or ''.

    `override` wins if it points at a real file (or at a directory holding one).
    """
    override = (override or "").strip().strip('"')
    if override:
        p = Path(override)
        if p.is_file():
            return str(p)
        if p.is_dir():
            for name in _exe_names(stem):
                cand = p / name
                if cand.is_file():
                    return str(cand)

    for d in _candidate_scripts_dirs():
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        for name in _exe_names(stem):
            cand = d / name
            if cand.is_file():
                return str(cand)

    found = shutil.which(stem)
    return found or ""


def parse_drop_data(data):
    """Parse a tkdnd <<Drop>> data string into a list of paths.

    Deliberately does NOT use Tk's splitlist. On Windows, Tcl applies backslash
    escape processing to unbraced list items, which silently corrupts real paths:
    "C:\\Users\\alice\\notes.pdf" comes back as "C:Users\x07lice\notes.pdf" because
    \a is a BEL escape, and "C:\\pdfs\\sample.pdf" loses its separators entirely.
    tkdnd braces only the paths that contain spaces, so the unbraced ones - the
    common case - are exactly the ones that get mangled.
    """
    if not data:
        return []
    raw = data.strip()
    # A single unbraced path that happens to contain spaces.
    if os.path.exists(raw):
        return [raw]

    items, buf, i, n = [], "", 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "{":
            end = raw.find("}", i + 1)
            if end == -1:            # unbalanced brace: treat it as literal text
                buf += ch
                i += 1
                continue
            items.append(raw[i + 1:end])
            i = end + 1
        elif ch.isspace():
            if buf.strip():
                items.append(buf)
            buf = ""
            i += 1
        else:
            buf += ch
            i += 1
    if buf.strip():
        items.append(buf)
    return [s for s in (x.strip() for x in items) if s]


def parse_java_major(text):
    """Pull the major version out of `java -version` output. Returns int or None."""
    m = re.search(r'version\s+"([0-9][0-9._\-a-zA-Z+]*)"', text)
    if not m:
        m = re.search(r"\b(\d+)\.\d+\.\d+", text)
        if not m:
            return None
        raw = m.group(1)
    else:
        raw = m.group(1)
    parts = re.split(r"[._\-+]", raw)
    try:
        first = int(parts[0])
    except (ValueError, IndexError):
        return None
    # Legacy scheme: 1.8.0_292 -> 8
    if first == 1 and len(parts) > 1:
        try:
            return int(parts[1])
        except ValueError:
            return 1
    return first


# --------------------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------------------
class PDFToDataApp:
    DEFAULT_SETTINGS = {
        "mode": "local",
        "deep": False,
        "force_ocr": False,
        "ocr_lang": "en",
        "enrich_formula": False,
        "enrich_picture": False,
        "keep_server": False,
        "hybrid_fallback": False,
        "hybrid_timeout": "",
        "port": "5002",
        "fmt_markdown": True,
        "fmt_json": False,
        "fmt_html": False,
        "fmt_text": False,
        "tagged_pdf": False,
        "output_dir": "",
        "pages": "",
        "threads": "",
        "table_method": "",
        "reading_order": "",
        "include_header_footer": False,
        "detect_strikethrough": False,
        "sanitize": False,
        "keep_line_breaks": False,
        "use_struct_tree": False,
        "markdown_with_html": False,
        "password": "",
        "image_output": "",
        "image_format": "",
        "image_dir": "",
        "image_resolution": "",
        "space_ratio": "",
        "content_safety_off": "",
        "quiet": False,
        "extra_args": "",
        "cli_path": "",
        "hybrid_cli_path": "",
        "advanced_open": False,
        "auto_check": True,
        "last_update_check": "",
    }

    # ---------------------------------------------------------------- construction
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        # Fit the actual screen. A fixed 980x820 with minsize(860,680) pushed the
        # Convert button below the bottom edge on a 1366x768 laptop, and the
        # minsize stopped the user shrinking the window enough to get it back.
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        win_w = max(640, min(980, screen_w - 80))
        win_h = max(460, min(820, screen_h - 120))   # leave room for the taskbar
        self.root.geometry(f"{win_w}x{win_h}")
        self.root.minsize(min(720, win_w), min(460, win_h))

        self.msgq = queue.Queue()
        self.files = []                  # list[str] of absolute .pdf paths
        self.server_proc = None
        self.active_proc = None          # currently-running conversion subprocess
        self.worker = None
        self.server_state = "stopped"    # stopped | starting | running | error
        self.server_port_running = None
        self.last_output_dir = ""
        self.last_cli_output = []
        self.converting = False

        self.upd_components = {}         # key -> updater.Component
        self.upd_latest_release = None   # cached GitHub release dict
        self.upd_busy = False
        self.upd_badge = False
        self.upd_notified = False
        self.env_java = None             # None = unknown, else (ok: bool, detail: str)
        self.env_base = None
        self.env_hybrid = None

        self.v = {}                      # settings key -> tk variable
        self._build_vars()
        self._load_settings()

        self._build_ui()
        self._wire_traces()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._pump)

        self.log(f"{APP_NAME} started.")
        self.log(f"Drag and drop: {'enabled (tkinterdnd2)' if HAVE_DND else 'unavailable - use Browse...'}")

        # Tell the update bootstrapper we came up cleanly; without this file it
        # rolls the update back after its watchdog expires.
        updater.write_started_ok()
        self._surface_update_failure()
        bundled_jre = INSTALL_DIR / "runtime" / "jre" / "bin" / "java.exe"
        if bundled_jre.is_file():
            self.log(f"Using the bundled Java runtime: {bundled_jre}")

        self.check_environment(startup=True)
        self.root.after(1200, self.check_updates_async)
        self.root.after(3000, self._maybe_auto_check)

    def _build_vars(self):
        for key, default in self.DEFAULT_SETTINGS.items():
            if isinstance(default, bool):
                self.v[key] = tk.BooleanVar(value=default)
            else:
                self.v[key] = tk.StringVar(value=str(default))

    # ---------------------------------------------------------------- settings I/O
    def _load_settings(self):
        if not SETTINGS_PATH.is_file():
            return
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Could not read settings.json: {exc}", file=sys.stderr)
            return
        if not isinstance(data, dict):
            return
        for key, var in self.v.items():
            if key not in data:
                continue
            value = data[key]
            try:
                if isinstance(var, tk.BooleanVar):
                    var.set(bool(value))
                else:
                    var.set("" if value is None else str(value))
            except Exception:
                pass
        files = data.get("files")
        if isinstance(files, list):
            self.files = [f for f in files if isinstance(f, str) and Path(f).is_file()]

    def _save_settings(self):
        data = {key: var.get() for key, var in self.v.items()}
        data["files"] = self.files
        try:
            SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"Could not write settings.json: {exc}", file=sys.stderr)

    # ---------------------------------------------------------------- UI scaffolding
    def _build_ui(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Convert.TButton", font=("Segoe UI", 12, "bold"), padding=10)
        style.configure("Reason.TLabel", foreground="#a33")
        style.configure("Ok.TLabel", foreground="#1a7f37")
        style.configure("Bad.TLabel", foreground="#c02020")
        style.configure("Muted.TLabel", foreground="#666")
        style.configure("Link.TLabel", foreground="#0b5ed7")

        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        # Pack the Convert bar FIRST, anchored to the bottom. pack() hands space
        # out in call order, so anything packed after an expand=True widget gets
        # whatever is left - which is nothing on a short window, silently
        # clipping the most important control in the app.
        self._build_action_bar(outer)

        paned = ttk.PanedWindow(outer, orient="vertical")
        paned.pack(fill="both", expand=True)

        top = ttk.Frame(paned)
        paned.add(top, weight=4)

        self.nb = ttk.Notebook(top)
        self.nb.pack(fill="both", expand=True)
        self._build_tab_input()
        self._build_tab_mode()
        self._build_tab_output()
        self._build_tab_advanced()
        self._build_tab_server()
        self._build_tab_env()
        self._build_tab_updates()

        bottom = ttk.Frame(paned)
        paned.add(bottom, weight=2)
        self._build_log(bottom)

    # ------------------------------------------------------------------- 1) INPUT
    def _build_tab_input(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="1. Input")

        hint = ttk.Label(
            f,
            text=("Drag PDFs here, or use Browse. Folders are scanned recursively for PDFs."
                  if HAVE_DND else
                  "Use the Browse buttons to add PDFs. Folders are scanned recursively for PDFs."),
            style="Muted.TLabel",
        )
        hint.pack(anchor="w", pady=(0, 6))

        body = ttk.Frame(f)
        body.pack(fill="both", expand=True)

        listwrap = ttk.Frame(body)
        listwrap.pack(side="left", fill="both", expand=True)

        self.listbox = tk.Listbox(listwrap, selectmode="extended", activestyle="none",
                                  font=("Consolas", 9))
        vs = ttk.Scrollbar(listwrap, orient="vertical", command=self.listbox.yview)
        hs = ttk.Scrollbar(listwrap, orient="horizontal", command=self.listbox.xview)
        self.listbox.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        listwrap.rowconfigure(0, weight=1)
        listwrap.columnconfigure(0, weight=1)
        tip(self.listbox, "The PDFs queued for conversion. Select one or more rows and click "
                          "Remove selected to take them out of the queue.")

        if HAVE_DND:
            try:
                self.listbox.drop_target_register(DND_FILES)
                self.listbox.dnd_bind("<<Drop>>", self._on_drop)
            except Exception as exc:
                print(f"Drag-and-drop registration failed: {exc}", file=sys.stderr)

        side = ttk.Frame(body, padding=(10, 0, 0, 0))
        side.pack(side="left", fill="y")

        b = ttk.Button(side, text="Browse files…", command=self.add_files)
        b.pack(fill="x", pady=2)
        tip(b, "Pick one or more PDF files to add to the queue. Works with or without "
               "drag-and-drop support installed.")

        b = ttk.Button(side, text="Browse folder…", command=self.add_folder)
        b.pack(fill="x", pady=2)
        tip(b, "Pick a folder; every .pdf inside it and its subfolders is added to the queue.")

        b = ttk.Button(side, text="Remove selected", command=self.remove_selected)
        b.pack(fill="x", pady=(12, 2))
        tip(b, "Remove the highlighted files from the queue. This never touches the files on disk.")

        b = ttk.Button(side, text="Clear all", command=self.clear_files)
        b.pack(fill="x", pady=2)
        tip(b, "Empty the queue completely. This never deletes anything from disk.")

        self.count_label = ttk.Label(f, text="0 PDFs selected")
        self.count_label.pack(anchor="w", pady=(8, 0))
        tip(self.count_label, "How many PDFs are currently queued. Convert stays disabled while this is zero.")

        self._refresh_listbox()

    # -------------------------------------------------------------------- 2) MODE
    def _build_tab_mode(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="2. Mode")

        r1 = ttk.Radiobutton(f, text="Local (fast, no AI)", value="local", variable=self.v["mode"])
        r1.pack(anchor="w", pady=3)
        tip(r1, "Fast, rule-based extraction that runs entirely on your machine - no AI, no internet. "
                "Best for normal digital PDFs with selectable text.")

        r2 = ttk.Radiobutton(f, text="Hybrid (AI)", value="hybrid", variable=self.v["mode"])
        r2.pack(anchor="w", pady=3)
        tip(r2, "Sends difficult pages to a local AI server (Docling + SmolVLM) for better layout, "
                "tables, formulas and image descriptions. Still 100% local - no cloud, no API key - "
                "but much slower and it needs the hybrid server running.")

        self.hybrid_panel = ttk.LabelFrame(f, text="Hybrid options", padding=10)

        row = ttk.Frame(self.hybrid_panel)
        row.pack(fill="x", pady=2)
        lbl = ttk.Label(row, text="Engine:")
        lbl.pack(side="left")
        eng = ttk.Label(row, text="docling-fast", style="Muted.TLabel")
        eng.pack(side="left", padx=6)
        tip(lbl, "The hybrid backend used by the client (--hybrid docling-fast). "
                 "docling-fast is currently the only engine this CLI supports.")
        tip(eng, "The hybrid backend used by the client (--hybrid docling-fast). "
                 "docling-fast is currently the only engine this CLI supports.")

        c = ttk.Checkbutton(self.hybrid_panel, text="Deep mode (send every page to the AI backend)",
                            variable=self.v["deep"])
        c.pack(anchor="w", pady=3)
        tip(c, "Client flag --hybrid-mode full: skips triage and sends all pages to the AI backend "
               "instead of only the tricky ones. Required for formula extraction and image "
               "descriptions; slower on long documents.")

        c = ttk.Checkbutton(self.hybrid_panel, text="Scanned document / force OCR",
                            variable=self.v["force_ocr"])
        c.pack(anchor="w", pady=3)
        tip(c, "Server flag --force-ocr: runs full-page OCR on every page, even pages that already "
               "have embedded text. Turn this on for scanned PDFs or photos of documents. "
               "Applied when the hybrid server starts.")

        row = ttk.Frame(self.hybrid_panel)
        row.pack(fill="x", pady=3)
        lbl = ttk.Label(row, text="OCR languages:")
        lbl.pack(side="left")
        e = ttk.Entry(row, textvariable=self.v["ocr_lang"], width=24)
        e.pack(side="left", padx=6)
        for w in (lbl, e):
            tip(w, "Server flag --ocr-lang: comma-separated language codes for OCR, e.g. 'en' or "
                   "'en,ko'. The default OCR engine (EasyOCR) uses two-letter ISO 639-1 codes.")

        c = ttk.Checkbutton(self.hybrid_panel, text="Formula extraction (LaTeX)",
                            variable=self.v["enrich_formula"])
        c.pack(anchor="w", pady=3)
        tip(c, "Server flag --enrich-formula: recognises mathematical formulas and writes them as "
               "LaTeX. Needs Deep mode; this app turns Deep mode on automatically.")

        c = ttk.Checkbutton(self.hybrid_panel, text="Chart / image descriptions (alt text)",
                            variable=self.v["enrich_picture"])
        c.pack(anchor="w", pady=3)
        tip(c, "Server flag --enrich-picture-description: generates written descriptions of charts "
               "and images using the local SmolVLM model. Needs Deep mode; this app turns Deep mode "
               "on automatically.")

        c = ttk.Checkbutton(self.hybrid_panel,
                            text="Fall back to local extraction if the AI backend fails",
                            variable=self.v["hybrid_fallback"])
        c.pack(anchor="w", pady=3)
        tip(c, "Client flag --hybrid-fallback: if the AI backend errors or times out, fall back to "
               "the fast local engine instead of failing the whole conversion. Worth enabling for "
               "your first hybrid run, when the server is still downloading its AI models.")

        row = ttk.Frame(self.hybrid_panel)
        row.pack(fill="x", pady=3)
        lbl = ttk.Label(row, text="Request timeout (ms):")
        lbl.pack(side="left")
        e = ttk.Entry(row, textvariable=self.v["hybrid_timeout"], width=10)
        e.pack(side="left", padx=6)
        for w in (lbl, e):
            tip(w, "Client flag --hybrid-timeout: how long to wait for each AI request, in "
                   "milliseconds. Empty or 0 uses the backend's own default. Raise it for very "
                   "long documents or a slow first run.")

        c = ttk.Checkbutton(self.hybrid_panel, text="Keep server running after conversion",
                            variable=self.v["keep_server"])
        c.pack(anchor="w", pady=3)
        tip(c, "Leave the hybrid server up when the conversion finishes. Startup loads AI models and "
               "takes a while, so keep it running if you plan to convert more files soon.")

        row = ttk.Frame(self.hybrid_panel)
        row.pack(fill="x", pady=3)
        lbl = ttk.Label(row, text="Server port:")
        lbl.pack(side="left")
        e = ttk.Entry(row, textvariable=self.v["port"], width=8)
        e.pack(side="left", padx=6)
        for w in (lbl, e):
            tip(w, "TCP port for the local hybrid server (default 5002). Change it only if something "
                   "else on your machine already uses that port.")

        note = ttk.Label(self.hybrid_panel, style="Muted.TLabel", wraplength=620, justify="left",
                         text="OCR, formula and picture-description settings are applied when the "
                              "server starts. If the server is already running, stop and start it "
                              "again to pick up changes.")
        note.pack(anchor="w", pady=(8, 0))

        self._sync_hybrid_panel()

    # ------------------------------------------------------------------ 3) OUTPUT
    def _build_tab_output(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="3. Output")

        fmt = ttk.LabelFrame(f, text="Formats", padding=10)
        fmt.pack(fill="x")

        specs = [
            ("fmt_markdown", "Markdown (.md)",
             "Clean Markdown with headings, lists and tables. The best general-purpose format for "
             "feeding documents to an LLM or a RAG pipeline."),
            ("fmt_json", "JSON (.json)",
             "Structured JSON containing the full document tree with element types and coordinates. "
             "Use it when you want to process the layout programmatically."),
            ("fmt_html", "HTML (.html)",
             "HTML preserving the document structure. Handy for previewing the extraction in a "
             "browser or embedding it in a web page."),
            ("fmt_text", "Plain text (.txt)",
             "Plain text with no markup at all. Smallest output; use it when you only need the words."),
        ]
        for key, label, text in specs:
            c = ttk.Checkbutton(fmt, text=label, variable=self.v[key])
            c.pack(anchor="w", pady=2)
            tip(c, text)

        ttk.Separator(fmt, orient="horizontal").pack(fill="x", pady=8)
        c = ttk.Checkbutton(fmt, text="Tagged PDF (accessibility)", variable=self.v["tagged_pdf"])
        c.pack(anchor="w", pady=2)
        tip(c, "Writes an accessibility-tagged copy of the PDF (--format tagged-pdf) instead of a "
               "text format: the original pages with a proper structure tree added, so screen "
               "readers can follow the reading order.")

        out = ttk.LabelFrame(f, text="Output folder", padding=10)
        out.pack(fill="x", pady=(10, 0))

        row = ttk.Frame(out)
        row.pack(fill="x")
        e = ttk.Entry(row, textvariable=self.v["output_dir"])
        e.pack(side="left", fill="x", expand=True)
        tip(e, "Where converted files are written. Leave it empty to use an 'output' folder next to "
               "the first PDF in the queue.")
        b = ttk.Button(row, text="Browse…", width=10, command=self.pick_output_dir)
        b.pack(side="left", padx=(6, 0))
        tip(b, "Choose the folder that converted files are written to.")

        row2 = ttk.Frame(out)
        row2.pack(fill="x", pady=(8, 0))
        self.open_out_btn = ttk.Button(row2, text="Open output folder", command=self.open_output_dir)
        self.open_out_btn.pack(side="left")
        tip(self.open_out_btn, "Open the output folder in File Explorer. Enabled once the folder "
                               "exists or a conversion has finished.")
        self._refresh_open_button()

        note = ttk.Label(out, style="Muted.TLabel", wraplength=620, justify="left",
                         text="Default when empty: <folder of the first PDF>\\output")
        note.pack(anchor="w", pady=(6, 0))

    # ---------------------------------------------------------------- 4) ADVANCED
    def _build_tab_advanced(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="4. Advanced")

        self.adv_btn = ttk.Button(f, text="", command=self._toggle_advanced)
        self.adv_btn.pack(anchor="w")
        tip(self.adv_btn, "Show or hide the advanced options. Everything in here is optional - "
                          "blank fields are simply not passed to the CLI.")

        self.adv_body = ttk.Frame(f, padding=(0, 10, 0, 0))

        grid = ttk.LabelFrame(self.adv_body, text="Extraction", padding=10)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        r = 0

        def add_entry(parent, row, label, key, tiptext, width=22, show=None):
            lbl = ttk.Label(parent, text=label)
            lbl.grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
            ent = ttk.Entry(parent, textvariable=self.v[key], width=width, show=show)
            ent.grid(row=row, column=1, sticky="w", pady=3)
            tip(lbl, tiptext)
            tip(ent, tiptext)
            return ent

        def add_combo(parent, row, label, key, values, tiptext, width=20):
            lbl = ttk.Label(parent, text=label)
            lbl.grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
            cb = ttk.Combobox(parent, textvariable=self.v[key], values=values,
                              width=width, state="readonly")
            cb.grid(row=row, column=1, sticky="w", pady=3)
            tip(lbl, tiptext)
            tip(cb, tiptext)
            return cb

        add_entry(grid, r, "Pages:", "pages",
                  "CLI --pages: limit extraction to certain pages, e.g. '1-5' or '1,3,5-7'. "
                  "Empty means every page.")
        r += 1
        add_entry(grid, r, "Threads:", "threads",
                  "CLI --threads: number of worker threads for per-page processing. Default is 1 "
                  "(sequential and stable); higher values are experimental, faster, and may vary "
                  "slightly on some PDFs. Ignored in Hybrid mode.", width=8)
        r += 1
        add_combo(grid, r, "Table method:", "table_method", ["", "default", "cluster"],
                  "CLI --table-method: how tables are detected. 'default' uses ruling lines; "
                  "'cluster' also groups text by position, which helps on borderless tables. "
                  "Empty = the CLI default.")
        r += 1
        add_combo(grid, r, "Reading order:", "reading_order", ["", "xycut", "off"],
                  "CLI --reading-order: algorithm that decides the order text is emitted in. "
                  "'xycut' handles columns properly; 'off' keeps the raw PDF order. "
                  "Empty = the CLI default (xycut).")
        r += 1
        add_entry(grid, r, "Space ratio:", "space_ratio",
                  "CLI --space-ratio: how big a gap between two characters must be before a space is "
                  "inserted, as a multiple of the font size (default 0.17). Lower it if words run "
                  "together, raise it if words get split.", width=8)
        r += 1
        add_combo(grid, r, "Content safety off:", "content_safety_off",
                  ["", "all", "hidden-text", "off-page", "tiny", "hidden-ocg", "background"],
                  "CLI --content-safety-off: stops the filter that hides suspicious content such as "
                  "invisible text, off-page or tiny elements. Only disable a filter if you know the "
                  "PDF is trustworthy and you are missing content.")
        r += 1
        add_entry(grid, r, "Password:", "password",
                  "CLI --password: the open password for an encrypted PDF. It is stored in "
                  "settings.json in plain text, so clear it after use if that matters to you.",
                  show="*")

        toggles = ttk.LabelFrame(self.adv_body, text="Text handling", padding=10)
        toggles.pack(fill="x", pady=(10, 0))
        for key, label, text in [
            ("include_header_footer", "Include headers and footers",
             "CLI --include-header-footer: keeps running page headers and footers in the output. "
             "They are dropped by default because they repeat on every page."),
            ("detect_strikethrough", "Detect strikethrough",
             "CLI --detect-strikethrough (experimental): marks struck-through text as ~~text~~ in "
             "Markdown or <del> in HTML instead of treating it as normal text."),
            ("sanitize", "Sanitize sensitive data",
             "CLI --sanitize: replaces emails, phone numbers, IP addresses, credit-card numbers and "
             "URLs with placeholders. Useful before sending a document to any external service."),
            ("keep_line_breaks", "Keep line breaks",
             "CLI --keep-line-breaks: preserves the original line breaks instead of joining lines "
             "into flowing paragraphs. Good for poetry, code listings and forms."),
            ("use_struct_tree", "Use PDF structure tree",
             "CLI --use-struct-tree: trusts the PDF's own accessibility tags for reading order and "
             "structure. Quality depends entirely on how well the PDF was tagged. Note: on a tagged "
             "PDF this takes precedence over Hybrid mode, so the AI backend is not called."),
            ("markdown_with_html", "Allow HTML inside Markdown",
             "CLI --markdown-with-html: lets the Markdown output use raw HTML tags for structures "
             "Markdown cannot express, such as tables with merged cells."),
            ("quiet", "Quiet CLI output",
             "CLI --quiet: suppresses the CLI's own console logging. The log pane below will show "
             "much less detail."),
        ]:
            c = ttk.Checkbutton(toggles, text=label, variable=self.v[key])
            c.pack(anchor="w", pady=2)
            tip(c, text)

        images = ttk.LabelFrame(self.adv_body, text="Images", padding=10)
        images.pack(fill="x", pady=(10, 0))
        images.columnconfigure(1, weight=1)
        add_combo(images, 0, "Image output:", "image_output", ["", "external", "embedded", "off"],
                  "CLI --image-output: 'external' saves images as files and links to them, "
                  "'embedded' inlines them as Base64 data URIs, 'off' drops images entirely. "
                  "Empty = the CLI default (external).")
        add_combo(images, 1, "Image format:", "image_format", ["", "png", "jpeg"],
                  "CLI --image-format: file type for extracted images. PNG is lossless and better "
                  "for diagrams; JPEG is smaller and better for photos.")
        lbl = ttk.Label(images, text="Image folder:")
        lbl.grid(row=2, column=0, sticky="w", pady=3, padx=(0, 8))
        irow = ttk.Frame(images)
        irow.grid(row=2, column=1, sticky="ew", pady=3)
        ent = ttk.Entry(irow, textvariable=self.v["image_dir"])
        ent.pack(side="left", fill="x", expand=True)
        btn = ttk.Button(irow, text="…", width=3, command=self.pick_image_dir)
        btn.pack(side="left", padx=(6, 0))
        for w in (lbl, ent, btn):
            tip(w, "CLI --image-dir: folder that extracted images are written to. Only applies when "
                   "Image output is 'external'. Empty = alongside the converted document.")
        add_entry(images, 3, "Image resolution (DPI):", "image_resolution",
                  "CLI --image-resolution: rendering resolution in DPI (default 144). Higher gives "
                  "sharper images but uses more memory; lower is faster and lighter.", width=8)

        extra = ttk.LabelFrame(self.adv_body, text="Extra CLI arguments", padding=10)
        extra.pack(fill="x", pady=(10, 0))
        e = ttk.Entry(extra, textvariable=self.v["extra_args"])
        e.pack(fill="x")
        tip(e, "Anything typed here is appended to the opendataloader-pdf command line verbatim, "
               "parsed like a shell would. For flags this app has no control for, e.g. "
               "--markdown-page-separator \"---\".")

        self._sync_advanced()

    # ----------------------------------------------------------- 5) HYBRID SERVER
    def _build_tab_server(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="5. Hybrid server")

        info = ttk.Label(
            f, wraplength=760, justify="left", style="Muted.TLabel",
            text="Hybrid mode needs a local AI server running on your machine. Starting it loads the "
                 "Docling and SmolVLM models, which can take a minute the first time. Nothing leaves "
                 "your computer: the server is bound to 127.0.0.1 (this machine only).")
        info.pack(anchor="w", pady=(0, 10))

        row = ttk.Frame(f)
        row.pack(fill="x")
        self.dot = tk.Canvas(row, width=16, height=16, highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_id = self.dot.create_oval(3, 3, 13, 13, fill="#c02020", outline="#8a1a1a")
        tip(self.dot, "Red = the hybrid server is not running. Amber = it is starting up. "
                      "Green = it is accepting connections and ready to convert.")
        self.server_status = ttk.Label(row, text="Not running")
        self.server_status.pack(side="left", padx=8)
        tip(self.server_status, "Current state of the local hybrid server process.")

        btns = ttk.Frame(f)
        btns.pack(anchor="w", pady=12)
        self.start_btn = ttk.Button(btns, text="Start server", command=self.start_server_clicked)
        self.start_btn.pack(side="left")
        tip(self.start_btn, "Launch opendataloader-pdf-hybrid with the options from the Mode tab and "
                            "wait until it accepts connections.")
        self.stop_btn = ttk.Button(btns, text="Stop server", command=self.stop_server_clicked,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        tip(self.stop_btn, "Shut the hybrid server down and free its port and memory.")

        note = ttk.Label(f, wraplength=760, justify="left", style="Muted.TLabel",
                         text="If you press Convert in Hybrid mode while the server is off, it is "
                              "started automatically first.")
        note.pack(anchor="w")

    # ------------------------------------------------------- 6) ENVIRONMENT/DOCTOR
    def _build_tab_env(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="6. Environment")

        b = ttk.Button(f, text="Check environment", command=lambda: self.check_environment())
        b.pack(anchor="w")
        tip(b, "Re-run the checks below: Java version, the base CLI, and the hybrid extra. "
               "Run it again after installing anything.")

        rows = ttk.Frame(f, padding=(0, 12, 0, 0))
        rows.pack(fill="x")
        rows.columnconfigure(2, weight=1)

        self.env_rows = {}
        for i, (key, title) in enumerate(
            [("java", "Java 11+"), ("base", "opendataloader-pdf"), ("hybrid", "opendataloader-pdf-hybrid")]
        ):
            dot = tk.Canvas(rows, width=16, height=16, highlightthickness=0)
            dot.grid(row=i, column=0, pady=4)
            oval = dot.create_oval(3, 3, 13, 13, fill="#999", outline="#777")
            name = ttk.Label(rows, text=title, width=26)
            name.grid(row=i, column=1, sticky="w", padx=(6, 10))
            status = ttk.Label(rows, text="not checked yet", style="Muted.TLabel")
            status.grid(row=i, column=2, sticky="w")
            fix = ttk.Label(rows, text="", style="Muted.TLabel", wraplength=700, justify="left")
            fix.grid(row=i, column=1, columnspan=2, sticky="w", padx=(32, 0))
            self.env_rows[key] = {"dot": dot, "oval": oval, "status": status, "fix": fix}

        tip(self.env_rows["java"]["status"],
            "opendataloader-pdf runs a bundled Java engine, so a Java 11 or newer runtime must be on "
            "your PATH. Checked by running 'java -version'.")
        tip(self.env_rows["base"]["status"],
            "The base command-line tool this app drives. Required for every conversion.")
        tip(self.env_rows["hybrid"]["status"],
            "The local AI server executable. Only needed when you choose Hybrid mode.")

        paths = ttk.LabelFrame(f, text="CLI locations (override if auto-detection fails)", padding=10)
        paths.pack(fill="x", pady=(16, 0))
        paths.columnconfigure(1, weight=1)

        for i, (key, label, tiptext) in enumerate([
            ("cli_path", "opendataloader-pdf:",
             "Full path to opendataloader-pdf.exe. Leave empty to auto-detect from the active "
             "virtual environment or your PATH."),
            ("hybrid_cli_path", "opendataloader-pdf-hybrid:",
             "Full path to opendataloader-pdf-hybrid.exe. Leave empty to auto-detect from the active "
             "virtual environment or your PATH."),
        ]):
            lbl = ttk.Label(paths, text=label)
            lbl.grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
            ent = ttk.Entry(paths, textvariable=self.v[key])
            ent.grid(row=i, column=1, sticky="ew", pady=3)
            btn = ttk.Button(paths, text="…", width=3,
                             command=lambda k=key: self.pick_cli_path(k))
            btn.grid(row=i, column=2, padx=(6, 0))
            for w in (lbl, ent, btn):
                tip(w, tiptext)

        self.resolved_label = ttk.Label(f, text="", style="Muted.TLabel", wraplength=760,
                                        justify="left")
        self.resolved_label.pack(anchor="w", pady=(10, 0))

    # --------------------------------------------------------------------- 7) LOG
    def _build_log(self, parent):
        frame = ttk.LabelFrame(parent, text="7. Log", padding=6)
        frame.pack(fill="both", expand=True)

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        self.logbox = tk.Text(wrap, wrap="none", height=6, font=("Consolas", 9),
                              state="disabled", background="#1e1e1e", foreground="#dcdcdc",
                              insertbackground="#dcdcdc")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.logbox.yview)
        hs = ttk.Scrollbar(wrap, orient="horizontal", command=self.logbox.xview)
        self.logbox.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.logbox.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.logbox.tag_configure("cmd", foreground="#6fb3ff")
        self.logbox.tag_configure("err", foreground="#ff8080")
        self.logbox.tag_configure("ok", foreground="#8ce08c")
        tip(self.logbox, "Everything the app does, plus the raw stdout and stderr of the CLI, with "
                         "timestamps. Read this first when a conversion fails.")

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(6, 0))
        b = ttk.Button(bar, text="Clear", command=self.clear_log)
        b.pack(side="left")
        tip(b, "Empty the log pane. This does not affect files already written to disk.")
        b = ttk.Button(bar, text="Save log…", command=self.save_log)
        b.pack(side="left", padx=6)
        tip(b, "Write the current log to a .txt file, for keeping a record or reporting a problem.")

    # ------------------------------------------------------------------ 8) CONVERT
    def _build_action_bar(self, parent):
        bar = ttk.Frame(parent, padding=(0, 8, 0, 0))
        bar.pack(side="bottom", fill="x")

        self.convert_btn = ttk.Button(bar, text="Convert", style="Convert.TButton",
                                      command=self.convert_clicked)
        self.convert_btn.pack(side="left")
        tip(self.convert_btn, "Run the conversion on every queued PDF using the settings above. "
                              "In Hybrid mode the local AI server is started first if needed.")

        self.cancel_btn = ttk.Button(bar, text="Stop", command=self.cancel_clicked, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        tip(self.cancel_btn, "Terminate the conversion that is currently running. Files already "
                             "written stay on disk.")

        self.reason_label = ttk.Label(bar, text="", style="Reason.TLabel", wraplength=620,
                                      justify="left")
        self.reason_label.pack(side="left", padx=12)
        tip(self.reason_label, "Why Convert is unavailable right now. It disappears once everything "
                               "needed is in place.")

        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.progress.pack(side="right")
        tip(self.progress, "Animates while a conversion or server start is in progress.")

    # ---------------------------------------------------------------- var plumbing
    def _wire_traces(self):
        for key in ("mode", "cli_path", "hybrid_cli_path", "fmt_markdown", "fmt_json",
                    "fmt_html", "fmt_text", "tagged_pdf", "output_dir"):
            self.v[key].trace_add("write", lambda *_: self._on_setting_changed())
        self._refresh_convert_state()

    def _on_setting_changed(self):
        self._sync_hybrid_panel()
        self._refresh_convert_state()
        self._refresh_open_button()

    def _sync_hybrid_panel(self):
        if not hasattr(self, "hybrid_panel"):
            return
        if self.v["mode"].get() == "hybrid":
            self.hybrid_panel.pack(fill="x", pady=(12, 0))
        else:
            self.hybrid_panel.pack_forget()

    def _toggle_advanced(self):
        self.v["advanced_open"].set(not self.v["advanced_open"].get())
        self._sync_advanced()

    def _sync_advanced(self):
        open_ = self.v["advanced_open"].get()
        self.adv_btn.configure(text=("▼ Advanced options" if open_ else "▶ Advanced options"))
        if open_:
            self.adv_body.pack(fill="both", expand=True)
        else:
            self.adv_body.pack_forget()

    # -------------------------------------------------------------- file handling
    def _on_drop(self, event):
        paths = parse_drop_data(event.data)
        if not paths:
            self.log("Nothing usable was dropped.", tag="err")
            return
        self.add_paths(paths)

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select PDF files",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if paths:
            self.add_paths(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="Select a folder containing PDFs")
        if folder:
            self.add_paths([folder])

    def add_paths(self, paths):
        added, dupes, skipped = 0, [], []
        for raw in paths:
            p = Path(str(raw).strip().strip('"'))
            if p.is_dir():
                found = sorted(
                    q for q in p.rglob("*") if q.is_file() and q.suffix.lower() == ".pdf"
                )
                if not found:
                    skipped.append(f"{p} (no PDFs inside)")
                for q in found:
                    a, d = self._add_one(q)
                    added += a
                    dupes.extend(d)
            elif p.is_file():
                if p.suffix.lower() != ".pdf":
                    skipped.append(f"{p.name} (not a .pdf)")
                    continue
                a, d = self._add_one(p)
                added += a
                dupes.extend(d)
            else:
                skipped.append(f"{p} (not found)")

        self._refresh_listbox()
        if added:
            self.log(f"Added {added} PDF(s). Queue now holds {len(self.files)}.")
        if dupes:
            self.log(f"Skipped {len(dupes)} duplicate(s): " + ", ".join(Path(d).name for d in dupes[:5])
                     + (" …" if len(dupes) > 5 else ""), tag="err")
        if skipped:
            for s in skipped:
                self.log(f"Skipped {s}", tag="err")
            messagebox.showwarning(
                APP_NAME,
                "Some items were skipped:\n\n" + "\n".join(skipped[:10])
                + ("\n…" if len(skipped) > 10 else "")
                + "\n\nOnly .pdf files (and folders containing them) can be converted.",
            )
        if added and not self.v["output_dir"].get().strip():
            self.v["output_dir"].set(str(Path(self.files[0]).parent / "output"))
        self._refresh_convert_state()

    def _add_one(self, path):
        resolved = str(Path(path).resolve())
        if resolved in self.files:
            return 0, [resolved]
        self.files.append(resolved)
        return 1, []

    def remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            self.log("Nothing selected to remove.", tag="err")
            return
        for i in reversed(sel):
            del self.files[i]
        self._refresh_listbox()
        self._refresh_convert_state()
        self.log(f"Removed {len(sel)} file(s). Queue now holds {len(self.files)}.")

    def clear_files(self):
        if not self.files:
            return
        n = len(self.files)
        self.files.clear()
        self._refresh_listbox()
        self._refresh_convert_state()
        self.log(f"Cleared {n} file(s) from the queue.")

    def _refresh_listbox(self):
        self.listbox.delete(0, "end")
        for f in self.files:
            self.listbox.insert("end", f)
        n = len(self.files)
        self.count_label.configure(text=f"{n} PDF{'s' if n != 1 else ''} selected")

    # ------------------------------------------------------------------ output dir
    def resolved_output_dir(self):
        manual = self.v["output_dir"].get().strip().strip('"')
        if manual:
            return manual
        if self.files:
            return str(Path(self.files[0]).parent / "output")
        return ""

    def pick_output_dir(self):
        start = self.resolved_output_dir() or str(Path.home())
        folder = filedialog.askdirectory(title="Select output folder", initialdir=start)
        if folder:
            self.v["output_dir"].set(folder)

    def pick_image_dir(self):
        folder = filedialog.askdirectory(title="Select folder for extracted images")
        if folder:
            self.v["image_dir"].set(folder)

    def pick_cli_path(self, key):
        types = [("Executables", "*.exe"), ("All files", "*.*")] if IS_WINDOWS else [("All files", "*.*")]
        path = filedialog.askopenfilename(title="Select the CLI executable", filetypes=types)
        if path:
            self.v[key].set(path)
            self.check_environment()

    def open_output_dir(self):
        target = self.last_output_dir or self.resolved_output_dir()
        if not target:
            messagebox.showinfo(APP_NAME, "No output folder yet - add a PDF or choose a folder first.")
            return
        p = Path(target)
        if not p.is_dir():
            messagebox.showinfo(APP_NAME, f"The folder does not exist yet:\n{p}")
            return
        try:
            if IS_WINDOWS:
                os.startfile(str(p))  # noqa: S606 - intentional shell-open on Windows
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the folder:\n{exc}")

    def _refresh_open_button(self):
        if not hasattr(self, "open_out_btn"):
            return
        target = self.last_output_dir or self.resolved_output_dir()
        state = "normal" if target and Path(target).is_dir() else "disabled"
        self.open_out_btn.configure(state=state)

    # ------------------------------------------------------------------------ log
    def log(self, text, tag=None):
        """Thread-safe: queues a line for the UI thread."""
        self.msgq.put(("log", text, tag))

    def _append_log(self, text, tag=None):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"[{stamp}] {text}\n", tag or ())
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    def save_log(self):
        path = filedialog.asksaveasfilename(
            title="Save log", defaultextension=".txt",
            initialfile=f"pdftodata-log-{datetime.now():%Y%m%d-%H%M%S}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.logbox.get("1.0", "end"), encoding="utf-8")
            self.log(f"Log saved to {path}", tag="ok")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save the log:\n{exc}")

    # ------------------------------------------------------------------ UI pumping
    def _pump(self):
        try:
            while True:
                msg = self.msgq.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._append_log(msg[1], msg[2])
                elif kind == "server":
                    self._apply_server_state(msg[1], msg[2])
                elif kind == "env":
                    self._apply_env(msg[1])
                elif kind == "convert_done":
                    self._apply_convert_done(msg[1], msg[2])
                elif kind == "busy":
                    self._apply_busy(msg[1])
                elif kind == "upd_rows":
                    self._apply_update_rows(msg[1])
                elif kind == "upd_status":
                    self._apply_update_status(msg[1], msg[2])
                elif kind == "upd_progress":
                    self._apply_update_progress(msg[1])
                elif kind == "upd_busy":
                    self._apply_update_busy(msg[1])
                elif kind == "upd_error":
                    self._show_details_error(msg[1], msg[2])
                elif kind == "upd_quit":
                    self._quit_for_update()
                elif kind == "upd_stamp":
                    self.v["last_update_check"].set(msg[1])
                elif kind == "upd_balloon":
                    self._balloon(msg[1], msg[2])
                elif kind == "upd_recheck_env":
                    self.check_environment()
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _apply_busy(self, busy):
        self.converting = busy
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        self._refresh_convert_state()

    # ------------------------------------------------------------------- doctor
    def _snapshot(self):
        """Copy every setting into a plain dict.

        MUST be called on the UI thread: Tk variables may only be read from the
        thread running the main loop. Worker threads read this dict instead.
        """
        return {key: var.get() for key, var in self.v.items()}

    def check_environment(self, startup=False):
        self.log("Checking environment…")
        snap = self._snapshot()
        threading.Thread(target=self._check_env_worker, args=(snap,), daemon=True).start()

    def _check_env_worker(self, snap):
        result = {}

        # --- Java -------------------------------------------------------------
        java = updater.java_executable()   # bundled runtime\\jre first, then PATH
        if not java:
            result["java"] = (False, "java not found on PATH",
                              "Install a Java 11+ runtime (Adoptium Temurin), then re-check.")
        else:
            try:
                proc = subprocess.run([java, "-version"], capture_output=True, text=True,
                                      timeout=30, creationflags=_creationflags())
                out = (proc.stderr or "") + (proc.stdout or "")
                major = parse_java_major(out)
                first = next((ln for ln in out.splitlines() if ln.strip()), "").strip()
                if major is None:
                    result["java"] = (False, f"could not parse the version from: {first}",
                                      "Install a Java 11+ runtime (Adoptium Temurin), then re-check.")
                elif major < 11:
                    result["java"] = (False, f"Java {major} found - too old ({first})",
                                      "Install a Java 11+ runtime (Adoptium Temurin), then re-check.")
                else:
                    result["java"] = (True, f"Java {major} - {first}", "")
            except Exception as exc:
                result["java"] = (False, f"running 'java -version' failed: {exc}",
                                  "Install a Java 11+ runtime (Adoptium Temurin), then re-check.")

        # --- base + hybrid CLIs ------------------------------------------------
        base = find_cli("opendataloader-pdf", snap["cli_path"])
        if base:
            # Running it is the only honest check: a pip .exe wrapper whose baked-in
            # interpreter path is gone still exists on disk but cannot start.
            argv = updater.cli_argv("opendataloader-pdf", base)
            ok, why = updater.cli_works(argv)
            how = "via python -m" if len(argv) > 1 else "via the script wrapper"
            if ok:
                result["base"] = (True, f"{base}  ({how})", "")
            else:
                result["base"] = (False, f"found but will not run: {why}",
                                  "The install looks incomplete - reinstall the app, "
                                  f"or run:  {PIP_BASE}")
        else:
            result["base"] = (False, "not found in this venv or on PATH",
                              f"Run:  {PIP_BASE}   (or set the path below)")

        hybrid = find_cli("opendataloader-pdf-hybrid", snap["hybrid_cli_path"])
        if hybrid:
            result["hybrid"] = (True, hybrid, "")
        else:
            result["hybrid"] = (False, "not found (only needed for Hybrid mode)",
                                f"Run:  {PIP_HYBRID}   (or set the path below)")

        self.msgq.put(("env", result))

    def _apply_env(self, result):
        self.env_java = result["java"]
        self.env_base = result["base"]
        self.env_hybrid = result["hybrid"]

        for key in ("java", "base", "hybrid"):
            ok, detail, fix = result[key]
            row = self.env_rows[key]
            row["dot"].itemconfigure(row["oval"],
                                     fill="#1a7f37" if ok else "#c02020",
                                     outline="#14602a" if ok else "#8a1a1a")
            row["status"].configure(text=detail, style="Ok.TLabel" if ok else "Bad.TLabel")
            row["fix"].configure(text=fix)

        # Make the Adoptium fix clickable.
        jrow = self.env_rows["java"]
        if not result["java"][0]:
            jrow["fix"].configure(text=f"Fix: install Java 11+ from {ADOPTIUM_URL}",
                                  style="Link.TLabel", cursor="hand2")
            jrow["fix"].bind("<Button-1>", lambda _e: webbrowser.open(ADOPTIUM_URL))
        else:
            jrow["fix"].configure(style="Muted.TLabel", cursor="")
            jrow["fix"].unbind("<Button-1>")

        self.resolved_label.configure(
            text=f"Resolved base CLI: {result['base'][1] if result['base'][0] else '(none)'}\n"
                 f"Resolved hybrid CLI: {result['hybrid'][1] if result['hybrid'][0] else '(none)'}"
        )

        for key, label in (("java", "Java"), ("base", "opendataloader-pdf"),
                           ("hybrid", "opendataloader-pdf-hybrid")):
            ok, detail, fix = result[key]
            self.log(f"{'OK ' if ok else 'MISSING'} {label}: {detail}", tag="ok" if ok else "err")
            if fix:
                self.log(f"    Fix: {fix}", tag="err")

        self._refresh_convert_state()

    # -------------------------------------------------------------- validation
    def selected_formats(self, snap=None):
        src = snap if snap is not None else self._snapshot()
        fmts = []
        for key, name in (("fmt_markdown", "markdown"), ("fmt_json", "json"),
                          ("fmt_html", "html"), ("fmt_text", "text")):
            if src[key]:
                fmts.append(name)
        if src["tagged_pdf"]:
            fmts.append("tagged-pdf")
        return fmts

    def validation_error(self):
        """Returns a human-readable reason Convert must stay disabled, or ''."""
        if self.converting:
            return "A conversion is already running."
        if not self.files:
            return "Add at least one PDF on the Input tab."
        if self.env_java is None or self.env_base is None:
            return "Checking the environment…"
        if not self.env_java[0]:
            return f"Java 11+ is required. {self.env_java[1]} - install from adoptium.net."
        if not self.env_base[0]:
            if "will not run" in str(self.env_base[1]):
                return ("The conversion engine is present but cannot start - "
                        "see the Environment tab.")
            return f"opendataloader-pdf not found. Run: {PIP_BASE}"
        if self.v["mode"].get() == "hybrid" and not self.env_hybrid[0]:
            return f"Hybrid mode needs the hybrid extra. Run: {PIP_HYBRID}"
        if not self.selected_formats():
            return "Choose at least one output format on the Output tab."
        return ""

    def _refresh_convert_state(self):
        if not hasattr(self, "convert_btn"):
            return
        reason = self.validation_error()
        self.convert_btn.configure(state="disabled" if reason else "normal")
        self.reason_label.configure(text=reason)

    # ------------------------------------------------------------ server control
    def _apply_server_state(self, state, detail):
        self.server_state = state
        colors = {"stopped": ("#c02020", "#8a1a1a"), "starting": ("#d18b00", "#9c6800"),
                  "running": ("#1a7f37", "#14602a"), "error": ("#c02020", "#8a1a1a")}
        fill, outline = colors.get(state, ("#999", "#777"))
        self.dot.itemconfigure(self.dot_id, fill=fill, outline=outline)
        self.server_status.configure(text=detail)
        self.start_btn.configure(state="disabled" if state in ("starting", "running") else "normal")
        self.stop_btn.configure(state="normal" if state in ("running", "starting") else "disabled")

    def server_alive(self):
        return self.server_proc is not None and self.server_proc.poll() is None

    def port_value(self, snap=None):
        raw = str((snap["port"] if snap is not None else self.v["port"].get()) or "").strip()
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
        return 5002

    def start_server_clicked(self):
        snap = self._snapshot()
        threading.Thread(target=self._ensure_server, args=(snap,), daemon=True).start()

    def stop_server_clicked(self):
        threading.Thread(target=self._stop_server, daemon=True).start()

    def _port_open(self, port, timeout=0.6):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except OSError:
            return False

    def _ensure_server(self, snap):
        """Start the hybrid server if needed and wait until it accepts connections.

        Runs on a worker thread, reading settings from `snap`. Returns True when ready.
        """
        port = self.port_value(snap)

        if self.server_alive() and self._port_open(port):
            self.msgq.put(("server", "running", f"Running on port {port}"))
            return True

        if self._port_open(port):
            self.log(f"Port {port} already accepts connections - reusing that server.", tag="ok")
            self.msgq.put(("server", "running", f"Running on port {port} (external process)"))
            return True

        exe = find_cli("opendataloader-pdf-hybrid", snap["hybrid_cli_path"])
        if not exe:
            self.log(f"opendataloader-pdf-hybrid not found. Fix: {PIP_HYBRID}", tag="err")
            self.msgq.put(("server", "error", "Not installed"))
            return False

        cmd = updater.cli_argv("opendataloader-pdf-hybrid", exe) + [
            "--host", "127.0.0.1", "--port", str(port)]
        if snap["force_ocr"]:
            cmd.append("--force-ocr")
            lang = str(snap["ocr_lang"] or "").strip()
            if lang:
                cmd += ["--ocr-lang", lang]
        if snap["enrich_formula"]:
            cmd.append("--enrich-formula")
        if snap["enrich_picture"]:
            cmd.append("--enrich-picture-description")

        self.msgq.put(("server", "starting", f"Starting on port {port}…"))
        self.log("$ " + " ".join(shlex.quote(c) for c in cmd), tag="cmd")
        self.log("Loading AI models - the first start can take a minute.")

        try:
            self.server_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=_creationflags(),
                env=updater.child_env(),
            )
        except Exception as exc:
            self.log(f"Could not start the hybrid server: {exc}", tag="err")
            self.msgq.put(("server", "error", f"Failed to start: {exc}"))
            return False

        threading.Thread(target=self._drain, args=(self.server_proc, "server"), daemon=True).start()

        deadline = time.time() + 60
        while time.time() < deadline:
            if self.server_proc.poll() is not None:
                self.log(f"The hybrid server exited early (code {self.server_proc.returncode}). "
                         "See the log lines above.", tag="err")
                self.msgq.put(("server", "error", "Exited during startup"))
                return False
            if self._port_open(port):
                self.server_port_running = port
                self.log(f"Hybrid server is ready on port {port}.", tag="ok")
                self.msgq.put(("server", "running", f"Running on port {port}"))
                return True
            time.sleep(1.0)

        self.log("The hybrid server did not become ready within 60 seconds. On a first run it may "
                 "still be downloading its AI models; that download is cached, so starting it again "
                 "usually succeeds.", tag="err")
        self.msgq.put(("server", "error", "Timed out waiting for the port"))
        return False

    def _stop_server(self):
        proc = self.server_proc
        if proc is None or proc.poll() is not None:
            self.server_proc = None
            self.msgq.put(("server", "stopped", "Not running"))
            return
        self.log("Stopping the hybrid server…")
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, creationflags=_creationflags())
            else:
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as exc:
            self.log(f"Problem stopping the server: {exc}", tag="err")
        self.server_proc = None
        self.server_port_running = None
        self.log("Hybrid server stopped.", tag="ok")
        self.msgq.put(("server", "stopped", "Not running"))

    def _drain(self, proc, prefix):
        """Pump a subprocess's merged stdout/stderr into the log."""
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log(f"[{prefix}] {line}")
        except Exception:
            pass

    # ---------------------------------------------------------------- conversion
    def build_command(self, exe, output_dir, snap):
        # Never invoke pip's .exe wrapper directly - see updater.cli_argv.
        cmd = list(updater.cli_argv("opendataloader-pdf", exe))
        hybrid = snap["mode"] == "hybrid"

        if hybrid:
            cmd += ["--hybrid", "docling-fast"]
            deep = snap["deep"] or snap["enrich_formula"] or snap["enrich_picture"]
            if deep:
                cmd += ["--hybrid-mode", "full"]
            port = self.port_value(snap)
            if port != 5002:
                cmd += ["--hybrid-url", f"http://127.0.0.1:{port}"]
            if snap["hybrid_fallback"]:
                cmd.append("--hybrid-fallback")
            timeout_ms = str(snap["hybrid_timeout"] or "").strip()
            if timeout_ms:
                cmd += ["--hybrid-timeout", timeout_ms]

        cmd += ["-o", output_dir]
        cmd += ["-f", ",".join(self.selected_formats(snap))]

        s = lambda k: str(snap[k] or "").strip()  # noqa: E731 - terse local helper

        if s("pages"):
            cmd += ["--pages", s("pages")]
        if s("threads"):
            cmd += ["--threads", s("threads")]
        if s("table_method"):
            cmd += ["--table-method", s("table_method")]
        if s("reading_order"):
            cmd += ["--reading-order", s("reading_order")]
        if s("space_ratio"):
            cmd += ["--space-ratio", s("space_ratio")]
        if s("content_safety_off"):
            cmd += ["--content-safety-off", s("content_safety_off")]
        if s("password"):
            cmd += ["-p", s("password")]
        if s("image_output"):
            cmd += ["--image-output", s("image_output")]
        if s("image_format"):
            cmd += ["--image-format", s("image_format")]
        if s("image_dir"):
            cmd += ["--image-dir", s("image_dir")]
        if s("image_resolution"):
            cmd += ["--image-resolution", s("image_resolution")]

        for key, flag in (("include_header_footer", "--include-header-footer"),
                          ("detect_strikethrough", "--detect-strikethrough"),
                          ("sanitize", "--sanitize"),
                          ("keep_line_breaks", "--keep-line-breaks"),
                          ("use_struct_tree", "--use-struct-tree"),
                          ("markdown_with_html", "--markdown-with-html"),
                          ("quiet", "--quiet")):
            if snap[key]:
                cmd.append(flag)

        extra = s("extra_args")
        if extra:
            try:
                cmd += shlex.split(extra, posix=False)
            except ValueError as exc:
                self.log(f"Could not parse the extra arguments ({exc}); ignoring them.", tag="err")

        cmd += list(self.files)
        return cmd

    def convert_clicked(self):
        reason = self.validation_error()
        if reason:
            messagebox.showwarning(APP_NAME, reason)
            return
        self._save_settings()
        # Read every setting on the UI thread, then hand the worker a plain dict.
        snap = self._snapshot()
        output_dir = self.resolved_output_dir()
        self.worker = threading.Thread(target=self._convert_worker,
                                       args=(snap, output_dir), daemon=True)
        self.worker.start()

    def cancel_clicked(self):
        proc = self.active_proc
        if proc is not None and proc.poll() is None:
            self.log("Stopping the conversion…", tag="err")
            try:
                if IS_WINDOWS:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, creationflags=_creationflags())
                else:
                    proc.terminate()
            except Exception as exc:
                self.log(f"Could not stop it: {exc}", tag="err")

    def _convert_worker(self, snap, output_dir):
        self.msgq.put(("busy", True))
        hybrid = snap["mode"] == "hybrid"
        ok = False
        try:
            try:
                Path(output_dir).mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                self.log(f"Could not create the output folder {output_dir}: {exc}", tag="err")
                return

            if hybrid:
                if snap["enrich_formula"] or snap["enrich_picture"]:
                    if not snap["deep"]:
                        self.log("Formula or picture enrichment is on, so deep mode "
                                 "(--hybrid-mode full) is being used.")
                if snap["use_struct_tree"]:
                    self.log("Note: --use-struct-tree takes precedence over hybrid on tagged PDFs, "
                             "so the AI backend may not be called for those files.", tag="err")
                if not self._ensure_server(snap):
                    self.log("Conversion aborted: the hybrid server is not available.", tag="err")
                    return

            exe = find_cli("opendataloader-pdf", snap["cli_path"])
            if not exe:
                self.log(f"opendataloader-pdf not found. Fix: {PIP_BASE}", tag="err")
                return

            cmd = self.build_command(exe, output_dir, snap)
            self.log(f"Converting {len(self.files)} file(s) into {output_dir}")
            self.log("$ " + " ".join(shlex.quote(c) for c in cmd), tag="cmd")

            code, out_tail = self._run_streamed(cmd)
            if code == 0:
                ok = True
                self.log(f"Done. Output written to {output_dir}", tag="ok")
            else:
                self._explain_cli_failure(code, out_tail)
        except Exception as exc:
            self.log(f"Unexpected error during conversion: {exc}", tag="err")
        finally:
            if hybrid and not snap["keep_server"]:
                self._stop_server()
            self.msgq.put(("busy", False))
            self.msgq.put(("convert_done", ok, output_dir))

    def _run_streamed(self, cmd):
        """Run the CLI, stream its output to the log, return (code, output_tail)."""
        self.last_cli_output = []
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # A --windowed build has no valid stdin; handing the child an
                # invalid handle can make it misbehave, so give it a real one.
                stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=_creationflags(),
                # Puts the bundled runtime\jre on PATH. Without this the engine
                # cannot find Java on a machine with no system-wide install.
                env=updater.child_env(),
            )
        except Exception as exc:
            self.log(f"Could not launch the CLI: {exc}", tag="err")
            return -1, []
        self.active_proc = proc
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        self.log(line)
                        self.last_cli_output.append(line)
                        del self.last_cli_output[:-40]
            proc.wait()
        finally:
            self.active_proc = None
        return proc.returncode, list(self.last_cli_output)

    def _explain_cli_failure(self, code, out_tail):
        """Turn a bare exit code into something the user can act on."""
        blob = "\n".join(out_tail).lower()
        self.log(f"The CLI exited with code {code}. Nothing further was run.", tag="err")

        if "java" in blob and ("not found" in blob or "not recognized" in blob):
            jre = INSTALL_DIR / "runtime" / "jre" / "bin" / "java.exe"
            if jre.is_file():
                msg = ("The engine could not find Java, even though this install has a bundled "
                       f"one at {jre}. Please report this - the log above has the details.")
            else:
                msg = ("The engine needs Java 11+ and could not find any. This copy has no "
                       "bundled runtime\\jre folder, so install Java from adoptium.net "
                       "(or reinstall the app using the full installer).")
            self.log(msg, tag="err")
            self.msgq.put(("upd_error", "The conversion failed: Java was not found.", msg))
            return

        if not out_tail:
            msg = (f"The conversion tool exited with code {code} without printing anything. "
                   "The most common causes are a missing Java runtime or an incomplete "
                   "install - check the Environment tab.")
            self.log(msg, tag="err")
            self.msgq.put(("upd_error", f"The conversion tool exited with code {code}.", msg))
            return

        self.log("See the CLI output above for the reason.", tag="err")

    def _apply_convert_done(self, ok, output_dir):
        if ok and output_dir:
            self.last_output_dir = output_dir
        self._refresh_open_button()
        self._refresh_convert_state()
        if ok:
            self.nb.select(2)  # bring the Output tab (with Open output folder) forward


    # ================================================================ 8) UPDATES ====
    def _build_tab_updates(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Updates")
        self.updates_tab = f

        intro = ttk.Label(
            f, style="Muted.TLabel", wraplength=780, justify="left",
            text="Each part of the app is versioned separately. The engine comes from PyPI; the app "
                 "itself comes from GitHub Releases. Updates never touch your settings or logs, and "
                 "every replaced file is backed up first.")
        intro.pack(anchor="w", pady=(0, 8))

        cols = ("component", "current", "latest", "status", "notes")
        self.upd_tree = ttk.Treeview(f, columns=cols, show="headings", height=6, selectmode="browse")
        for key, title, width, anchor in (
            ("component", "Component", 190, "w"),
            ("current", "Current", 120, "w"),
            ("latest", "Latest", 120, "w"),
            ("status", "Status", 130, "w"),
            ("notes", "Notes", 330, "w"),
        ):
            self.upd_tree.heading(key, text=title)
            self.upd_tree.column(key, width=width, anchor=anchor, stretch=(key == "notes"))
        self.upd_tree.tag_configure("ok", foreground="#1a7f37")
        self.upd_tree.tag_configure("update", foreground="#b26a00")
        self.upd_tree.tag_configure("bad", foreground="#c02020")
        self.upd_tree.tag_configure("checking", foreground="#666666")
        self.upd_tree.pack(fill="x")
        tip(self.upd_tree, "One row per component. Green means up to date, amber means an update is "
                           "available, red means missing or broken. Hover a row to see when it was "
                           "last checked.")
        self._attach_row_tooltip(self.upd_tree)

        for key, name in (("app", "App (PDF to Data)"), ("engine", "Engine (opendataloader-pdf)"),
                          ("hybrid", "Hybrid AI extra"), ("java", "Java runtime"),
                          ("python", "Python runtime")):
            self.upd_tree.insert("", "end", iid=key,
                                 values=(name, "…", "…", "checking…", ""), tags=("checking",))

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(10, 0))

        self.upd_check_btn = ttk.Button(bar, text="Check now", command=self.check_updates_async)
        self.upd_check_btn.pack(side="left")
        tip(self.upd_check_btn, "Re-read every installed version and ask PyPI and GitHub what the "
                                "latest ones are. Nothing is downloaded or changed by checking.")

        self.upd_engine_btn = ttk.Button(bar, text="Update engine", command=self.update_engine_clicked,
                                         state="disabled")
        self.upd_engine_btn.pack(side="left", padx=6)
        tip(self.upd_engine_btn, "Upgrade opendataloader-pdf (and the hybrid extra) inside the app's "
                                 "own Python runtime with pip. Only the engine changes; the app "
                                 "itself is untouched.")

        self.upd_app_btn = ttk.Button(bar, text="Update app", command=self.update_app_clicked,
                                      state="disabled")
        self.upd_app_btn.pack(side="left", padx=6)
        tip(self.upd_app_btn, "Download the new portable bundle, verify its SHA-256, back up every "
                              "file it replaces, then restart into the new version. Aborts safely on "
                              "any mismatch.")

        self.upd_notes_btn = ttk.Button(bar, text="Open release notes", command=self.open_release_notes)
        self.upd_notes_btn.pack(side="left", padx=6)
        tip(self.upd_notes_btn, "Open the release page for the selected row in your browser - the "
                                "GitHub release for the app, or the PyPI/GitHub page for the engine.")

        self.upd_undo_btn = ttk.Button(bar, text="Undo last update", command=self.undo_update_clicked)
        tip(self.upd_undo_btn, "Restore the files replaced by the most recent update from its backup, "
                               "and put the previous version number back. Only shown when a backup "
                               "exists.")

        prog = ttk.Frame(f)
        prog.pack(fill="x", pady=(10, 0))
        self.upd_progress = ttk.Progressbar(prog, mode="determinate", length=260, maximum=100)
        self.upd_progress.pack(side="left")
        tip(self.upd_progress, "Download and installation progress for the current update.")
        self.upd_status = ttk.Label(prog, text="", style="Muted.TLabel", wraplength=470,
                                    justify="left")
        self.upd_status.pack(side="left", padx=10)
        tip(self.upd_status, "What the updater is doing right now.")

        auto = ttk.Checkbutton(f, text="Check for updates automatically (once a day, in the background)",
                               variable=self.v["auto_check"])
        auto.pack(anchor="w", pady=(12, 0))
        tip(auto, "When on, the app quietly checks once every 24 hours at startup and shows an amber "
                  "dot on this tab if something is available. It never installs anything on its own.")

        self._refresh_undo_button()

    def _attach_row_tooltip(self, tree):
        """Per-row hover tooltip showing when that component was last checked."""
        state = {"row": None, "tip": None}

        def hide():
            if state["tip"] is not None:
                try:
                    state["tip"].destroy()
                except Exception:
                    pass
                state["tip"] = None
            state["row"] = None

        def on_motion(event):
            row = tree.identify_row(event.y)
            if row == state["row"]:
                return
            hide()
            if not row:
                return
            comp = self.upd_components.get(row)
            when = (comp.checked if comp and comp.checked else "not checked yet")
            note = (comp.notes if comp and comp.notes else "")
            text = f"Last checked: {when}" + (f"\n{note}" if note else "")
            win = tk.Toplevel(tree)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{tree.winfo_rootx() + event.x + 16}+{tree.winfo_rooty() + event.y + 20}")
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(win, text=text, justify="left", wraplength=360, background="#ffffe0",
                     foreground="#1a1a1a", relief="solid", borderwidth=1, padx=8, pady=5,
                     font=("Segoe UI", 9)).pack()
            state["row"], state["tip"] = row, win

        tree.bind("<Motion>", on_motion, add="+")
        tree.bind("<Leave>", lambda _e: hide(), add="+")

    # ---------------------------------------------------------------- UI plumbing
    def _apply_update_rows(self, components):
        for comp in components:
            self.upd_components[comp.key] = comp
            tag = {updater.OK: "ok", updater.UPDATE: "update", updater.MISSING: "bad",
                   updater.ERROR: "bad", updater.CHECKING: "checking"}.get(comp.status, "checking")
            label = {updater.OK: "Up to date", updater.UPDATE: "Update available",
                     updater.MISSING: "Missing", updater.ERROR: "Error",
                     updater.CHECKING: "checking…"}.get(comp.status, comp.status)
            if not self.upd_tree.exists(comp.key):
                self.upd_tree.insert("", "end", iid=comp.key, values=())
            self.upd_tree.item(comp.key, tags=(tag,),
                               values=(comp.name, comp.current or "-", comp.latest or "-",
                                       label, comp.notes or ""))

        engine = self.upd_components.get("engine")
        app = self.upd_components.get("app")
        self.upd_engine_btn.configure(
            state="normal" if (engine and engine.status == updater.UPDATE) else "disabled")
        self.upd_app_btn.configure(
            state="normal" if (app and app.status == updater.UPDATE) else "disabled")

        available = any(c.status == updater.UPDATE for c in self.upd_components.values())
        self._set_update_badge(available)
        self._refresh_undo_button()

    def _set_update_badge(self, on):
        self.upd_badge = bool(on)
        try:
            self.nb.tab(self.updates_tab, text=("Updates ●" if on else "Updates"))
        except Exception:
            pass

    def _apply_update_status(self, text, busy=None):
        self.upd_status.configure(text=text)
        if busy is not None:
            self._apply_update_busy(busy)

    def _apply_update_progress(self, fraction):
        if fraction is None:
            self.upd_progress.configure(value=0)
        else:
            self.upd_progress.configure(value=max(0, min(100, fraction * 100)))

    def _apply_update_busy(self, busy):
        self.upd_busy = bool(busy)
        state = "disabled" if busy else "normal"
        self.upd_check_btn.configure(state=state)
        if busy:
            self.upd_engine_btn.configure(state="disabled")
            self.upd_app_btn.configure(state="disabled")
        else:
            self._apply_update_rows(list(self.upd_components.values()))

    def _refresh_undo_button(self):
        if not hasattr(self, "upd_undo_btn"):
            return
        if updater.list_backups():
            if not self.upd_undo_btn.winfo_ismapped():
                self.upd_undo_btn.pack(side="left", padx=6)
        else:
            self.upd_undo_btn.pack_forget()

    def _show_details_error(self, message, fix=""):
        """One-line human message plus a Details pane holding the updater.log tail."""
        win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} - update")
        win.transient(self.root)
        win.geometry("640x200")
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, wraplength=600, justify="left",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        if fix:
            ttk.Label(frame, text=fix, wraplength=600, justify="left",
                      style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        body = ttk.Frame(frame)
        shown = {"on": False}

        def toggle():
            if shown["on"]:
                body.pack_forget()
                win.geometry("640x200")
                det.configure(text="Details ▸")
            else:
                body.pack(fill="both", expand=True, pady=(10, 0))
                win.geometry("640x460")
                det.configure(text="Details ▾")
            shown["on"] = not shown["on"]

        row = ttk.Frame(frame)
        row.pack(anchor="w", pady=(10, 0))
        det = ttk.Button(row, text="Details ▸", command=toggle)
        det.pack(side="left")
        tip(det, "Show the last lines of updater.log, which records every step of every update.")
        close = ttk.Button(row, text="Close", command=win.destroy)
        close.pack(side="left", padx=6)
        tip(close, "Dismiss this message. Nothing was changed on disk.")

        text = tk.Text(body, wrap="none", font=("Consolas", 9), height=14,
                       background="#1e1e1e", foreground="#dcdcdc")
        scroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        text.insert("1.0", updater.log_tail(60))
        text.configure(state="disabled")

    def _surface_update_failure(self):
        marker = updater.take_failure_marker()
        if not marker:
            return
        reason = marker.get("reason", "The previous update did not start correctly.")
        self.log(f"Previous update was rolled back: {reason}", tag="err")
        self.root.after(800, lambda: self._show_details_error(
            "The last update was rolled back automatically and your previous version was restored.",
            f"Reason: {reason}"))

    # ------------------------------------------------------------------- checking
    def _engine_cli_hint(self):
        """The CLI path the app actually uses - resolved on the UI thread so the
        Updates tab reports the same engine the Environment tab found."""
        try:
            return find_cli("opendataloader-pdf", self.v["cli_path"].get())
        except Exception:
            return ""

    def check_updates_async(self):
        if self.upd_busy:
            return
        self.msgq.put(("upd_busy", True))
        self.msgq.put(("upd_status", "Checking…", None))
        hint = self._engine_cli_hint()
        threading.Thread(target=self._check_updates_worker, args=(False, hint),
                         daemon=True).start()

    def _maybe_auto_check(self):
        if not self.v["auto_check"].get():
            return
        if not updater.should_auto_check(self.v["last_update_check"].get()):
            return
        hint = self._engine_cli_hint()
        threading.Thread(target=self._check_updates_worker, args=(True, hint),
                         daemon=True).start()

    def _check_updates_worker(self, silent, cli_hint=""):
        stamp = updater.now_iso()
        comps = []
        try:
            app = updater.detect_app()
            engine = updater.detect_engine(cli_hint)
            hybrid = updater.detect_hybrid(cli_hint)
            java = updater.detect_java()
            python = updater.detect_python_runtime()

            # --- latest engine (PyPI: no auth, not rate limited) ---------------
            try:
                latest_engine = updater.latest_engine_version()
                engine.latest = latest_engine
                if engine.status != updater.MISSING and updater.is_newer(latest_engine, engine.current):
                    engine.status = updater.UPDATE
                    engine.notes = f"{engine.current} → {latest_engine} available on PyPI"
            except updater.UpdateError as exc:
                engine.latest = "unknown"
                if engine.status == updater.OK:
                    engine.notes = exc.message

            # --- latest app (GitHub releases, static fallback on rate limit) ---
            if updater.repo_is_configured():
                try:
                    release = updater.latest_app_release()
                    self.upd_latest_release = release
                    app.latest = release["version"]
                    if updater.is_newer(release["version"], app.current):
                        app.status = updater.UPDATE
                        app.notes = f"{app.current} → {release['version']} ({release['source']})"
                    elif app.status == updater.OK:
                        app.notes = f"latest release ({release['source']})"
                except updater.UpdateError as exc:
                    app.latest = "unknown"
                    app.notes = exc.message
            else:
                app.latest = "n/a"
                app.notes = "No update repository configured for this build."

            hybrid.latest = engine.latest
            for c in (app, engine, hybrid, java, python):
                c.checked = stamp
            comps = [app, engine, hybrid, java, python]
            self.msgq.put(("upd_rows", comps))

            # Tk variables may only be touched on the UI thread.
            self.msgq.put(("upd_stamp", stamp))
            available = [c.name for c in comps if c.status == updater.UPDATE]
            if silent:
                if available:
                    updater.log(f"auto-check: updates available: {', '.join(available)}", "check")
                    self.msgq.put(("upd_status", f"Update available: {', '.join(available)}", False))
                    if not self.upd_notified:
                        self.upd_notified = True
                        self.msgq.put(("log", "Update available: " + ", ".join(available), "ok"))
                        self.msgq.put(("upd_balloon", "Update available",
                                       ", ".join(available) + "\nOpen the Updates tab."))
            else:
                self.msgq.put(("upd_status",
                               ("Update available: " + ", ".join(available)) if available
                               else "Everything is up to date.", False))
        except Exception as exc:
            updater.log(f"check failed: {type(exc).__name__}: {exc}", "check", "ERROR")
            self.msgq.put(("upd_status", f"Check failed: {exc}", False))
        finally:
            if not silent:
                self.msgq.put(("upd_busy", False))

    def _balloon(self, title, message, ms=9000):
        """Small non-modal toast in the bottom-right of the app window."""
        try:
            win = tk.Toplevel(self.root)
            win.wm_overrideredirect(True)
            self.root.update_idletasks()
            x = self.root.winfo_rootx() + self.root.winfo_width() - 330
            y = self.root.winfo_rooty() + self.root.winfo_height() - 120
            win.wm_geometry(f"320x86+{max(0, x)}+{max(0, y)}")
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            frame = tk.Frame(win, background="#fff8e1", highlightbackground="#b26a00",
                             highlightthickness=1)
            frame.pack(fill="both", expand=True)
            tk.Label(frame, text=title, background="#fff8e1", foreground="#7a4a00",
                     font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(8, 0))
            tk.Label(frame, text=message, background="#fff8e1", foreground="#333",
                     justify="left", wraplength=300, font=("Segoe UI", 9)).pack(anchor="w", padx=10)
            win.after(ms, win.destroy)
            win.bind("<Button-1>", lambda _e: win.destroy())
        except Exception:
            pass

    # -------------------------------------------------------------------- guards
    def _update_guard(self):
        """Updates are refused while the app is doing real work."""
        if self.converting:
            return "A conversion is running. Stop the current job first."
        if self.server_alive():
            return "The hybrid server is running. Stop it on the Hybrid server tab first."
        if self.upd_busy:
            return "Another update is already in progress."
        return ""

    # ------------------------------------------------------------- engine update
    def update_engine_clicked(self):
        reason = self._update_guard()
        if reason:
            messagebox.showwarning(APP_NAME, reason)
            return
        engine = self.upd_components.get("engine")
        if not engine or not engine.latest:
            messagebox.showwarning(APP_NAME, "Check for updates first.")
            return
        if not messagebox.askyesno(
                APP_NAME,
                f"Update the engine from {engine.current} to {engine.latest}?\n\n"
                "This runs pip inside the app's own Python runtime. Only the engine "
                "changes - the app itself and your settings are untouched."):
            return
        self.msgq.put(("upd_busy", True))
        self.msgq.put(("upd_status", f"Updating engine to {engine.latest}…", None))
        hint = self._engine_cli_hint()
        threading.Thread(target=self._engine_worker, args=(engine.latest, hint),
                         daemon=True).start()

    def _engine_worker(self, target, cli_hint=""):
        try:
            new_version = updater.update_engine(target, log_cb=lambda line: self.log(line),
                                                cli_path=cli_hint)
            self.log(f"Engine updated to {new_version}.", tag="ok")
            self.msgq.put(("upd_status", f"Engine updated to {new_version}.", False))
            self.msgq.put(("upd_busy", False))
            self._check_updates_worker(True, cli_hint)
            self.msgq.put(("upd_recheck_env",))
        except updater.UpdateError as exc:
            self.log(f"Engine update failed: {exc.message}", tag="err")
            self.msgq.put(("upd_status", "Engine update failed.", False))
            self.msgq.put(("upd_error", exc.message, exc.fix))
        except Exception as exc:
            updater.log(f"engine worker crashed: {exc}", "engine", "ERROR")
            self.msgq.put(("upd_error", "The engine update failed unexpectedly.", str(exc)[:200]))
        finally:
            self.msgq.put(("upd_busy", False))

    # ---------------------------------------------------------------- app update
    def update_app_clicked(self):
        reason = self._update_guard()
        if reason:
            messagebox.showwarning(APP_NAME, reason)
            return
        release = self.upd_latest_release
        app = self.upd_components.get("app")
        if not release or not app:
            messagebox.showwarning(APP_NAME, "Check for updates first.")
            return
        if not self._confirm_release(release, app):
            return
        self.msgq.put(("upd_busy", True))
        self.msgq.put(("upd_status", "Downloading update…", None))
        threading.Thread(target=self._app_worker, args=(release, app.current), daemon=True).start()

    def _confirm_release(self, release, app):
        """Show the release notes and require an explicit yes."""
        win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} {release['version']} - release notes")
        win.transient(self.root)
        win.grab_set()
        win.geometry("660x480")
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Update from {app.current} to {release['version']}?",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, style="Muted.TLabel", wraplength=620, justify="left",
                  text="The download is checked against its published SHA-256, every file it "
                       "replaces is backed up first, and the app restarts into the new version. "
                       "Your settings and logs are not touched.").pack(anchor="w", pady=(4, 8))

        box = ttk.Frame(frame)
        box.pack(fill="both", expand=True)
        text = tk.Text(box, wrap="word", font=("Consolas", 9), height=16)
        scroll = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        text.insert("1.0", release.get("body") or "(this release has no notes)")
        text.configure(state="disabled")

        result = {"go": False}
        row = ttk.Frame(frame)
        row.pack(anchor="e", pady=(10, 0))

        def go():
            result["go"] = True
            win.destroy()

        ok = ttk.Button(row, text=f"Update to {release['version']}", command=go)
        ok.pack(side="left")
        tip(ok, "Download, verify, back up and install this release.")
        cancel = ttk.Button(row, text="Cancel", command=win.destroy)
        cancel.pack(side="left", padx=6)
        tip(cancel, "Close without changing anything.")
        self.root.wait_window(win)
        return result["go"]

    def _app_worker(self, release, current_version):
        try:
            def progress(done, total):
                if total:
                    self.msgq.put(("upd_progress", done / total))
                    self.msgq.put(("upd_status",
                                   f"Downloading… {done // 1048576} / {total // 1048576} MB", None))

            prepared = updater.prepare_app_update(
                release, progress=progress, log_cb=lambda line: self.log(line))
            self.msgq.put(("upd_progress", 1.0))

            if not prepared["changed"]:
                self.msgq.put(("upd_status", "Already identical to that release; nothing to do.", False))
                self.msgq.put(("upd_busy", False))
                return

            self.msgq.put(("upd_status",
                           f"Backing up {len(prepared['changed'])} file(s) and restarting…", None))
            updater.apply_app_update(prepared, current_version, log_cb=lambda line: self.log(line))
            updater.prune_backups(keep=3)
            self.msgq.put(("upd_quit",))
        except updater.UpdateError as exc:
            self.log(f"App update failed: {exc.message}", tag="err")
            self.msgq.put(("upd_progress", None))
            self.msgq.put(("upd_status", "Update failed - nothing was changed.", False))
            self.msgq.put(("upd_error", exc.message, exc.fix))
            self.msgq.put(("upd_busy", False))
        except Exception as exc:
            updater.log(f"app worker crashed: {exc}", "app", "ERROR")
            self.msgq.put(("upd_progress", None))
            self.msgq.put(("upd_error", "The app update failed unexpectedly.", str(exc)[:200]))
            self.msgq.put(("upd_busy", False))

    def _quit_for_update(self):
        self.log("Closing so the updater can replace files…", tag="ok")
        try:
            self._save_settings()
        except Exception:
            pass
        try:
            if self.server_alive():
                self._stop_server()
        except Exception:
            pass
        self.root.after(400, self.root.destroy)

    # -------------------------------------------------------------------- rollback
    def undo_update_clicked(self):
        backups = updater.list_backups()
        if not backups:
            messagebox.showinfo(APP_NAME, "There is no update backup to undo.")
            self._refresh_undo_button()
            return
        reason = self._update_guard()
        if reason:
            messagebox.showwarning(APP_NAME, reason)
            return
        newest = backups[0]
        manifest = updater.read_manifest(newest)
        replaced = manifest.get("app_version_replaced", "?")
        count = len(manifest.get("files", []))
        if not messagebox.askyesno(
                APP_NAME,
                f"Restore version {replaced} from {newest.name}?\n\n"
                f"{count} file(s) will be put back. Your settings and logs are not affected.\n"
                "The app will close so the files can be swapped."):
            return
        try:
            restored = updater.restore_backup(newest, log_cb=lambda m: self.log(m))
            self.log(f"Rolled back {restored} file(s) to version {replaced}.", tag="ok")
            messagebox.showinfo(APP_NAME,
                                f"Restored version {replaced}. Please start the app again.")
            self._quit_for_update()
        except updater.UpdateError as exc:
            self._show_details_error(exc.message, exc.fix)

    def open_release_notes(self):
        sel = self.upd_tree.selection()
        key = sel[0] if sel else "app"
        if key in ("engine", "hybrid"):
            comp = self.upd_components.get("engine")
            version = (comp.latest if comp and comp.latest else "").strip()
            url = (f"{updater.ENGINE_RELEASES}/tag/v{version}" if version and version != "unknown"
                   else updater.ENGINE_RELEASES)
        elif key == "java":
            url = "https://adoptium.net/temurin/releases/?version=17"
        else:
            release = self.upd_latest_release
            url = (release or {}).get("html_url") or (
                f"https://github.com/{updater.configured_repo()}/releases"
                if updater.repo_is_configured() else updater.ENGINE_RELEASES)
        self.log(f"Opening {url}")
        webbrowser.open(url)

    # --------------------------------------------------------------------- close
    def _on_close(self):
        if self.converting:
            if not messagebox.askyesno(APP_NAME, "A conversion is still running. Quit anyway?"):
                return
            self.cancel_clicked()
        self._save_settings()
        if self.server_alive():
            self._stop_server()
        self.root.destroy()


def main():
    if HAVE_DND:
        try:
            root = TkinterDnD.Tk()
        except Exception as exc:  # broken tkinterdnd2 install - degrade gracefully
            print(f"tkinterdnd2 failed to initialise ({exc}); falling back to plain Tk.",
                  file=sys.stderr)
            globals()["HAVE_DND"] = False
            root = tk.Tk()
    else:
        root = tk.Tk()

    PDFToDataApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
