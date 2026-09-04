"""
updater.py - version detection, engine/app updates, backup and rollback for PDF to Data.

Design rules (enforced throughout):
  * This module is the ONLY code allowed to modify the install directory.
  * User data (%APPDATA%\\PDFtoData) is never touched by an update.
  * Nothing in the install directory is overwritten until a byte-verified backup
    of the exact files being replaced exists on disk.
  * Every network call and every file operation is safe to run on a worker thread;
    progress is reported through callbacks, never by touching Tk directly.

Standard library only (urllib, not requests) so the frozen exe stays small.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_SLUG = "PDFtoData"
ENGINE_PKG = "opendataloader-pdf"
HYBRID_PKG = "opendataloader-pdf-hybrid"

# Filled in by build_release.ps1 (written into version.json as "repo").
# Until a real repo exists this stays a placeholder and app-update checks report
# a friendly "not configured" instead of failing.
DEFAULT_REPO = "YOUR-GITHUB-USERNAME/PDFtoData"

PYPI_JSON = "https://pypi.org/pypi/{pkg}/json"
GITHUB_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_RAW_FALLBACK = "https://raw.githubusercontent.com/{repo}/main/version.json"
ENGINE_RELEASES = "https://github.com/opendataloader-project/opendataloader-pdf/releases"
ENGINE_PYPI_PAGE = "https://pypi.org/project/opendataloader-pdf/"

MIN_JAVA = 11
USER_AGENT = "PDFtoData-Updater"
NET_TIMEOUT = 20

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Status values used by the Updates tab.
OK, UPDATE, MISSING, ERROR, CHECKING = "ok", "update", "missing", "error", "checking"


class UpdateError(Exception):
    """Carries a human one-liner; .fix is an actionable suggestion."""

    def __init__(self, message, fix=""):
        super().__init__(message)
        self.message = message
        self.fix = fix


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def is_frozen():
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """Where the app is installed. For a frozen exe this is the exe's folder."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    """%APPDATA%\\PDFtoData - settings and logs. Never modified by updates."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    p = Path(base) / APP_SLUG
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def local_data_dir() -> Path:
    """%LOCALAPPDATA%\\PDFtoData - updater.log lives here (per the spec)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    p = Path(base) / APP_SLUG
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def updater_log_path() -> Path:
    return local_data_dir() / "updater.log"


def version_json_path() -> Path:
    return install_dir() / "version.json"


def started_ok_path() -> Path:
    return install_dir() / ".started-ok"


def failure_marker_path() -> Path:
    return install_dir() / ".update-failed.json"


def runtime_python() -> Path:
    return install_dir() / "runtime" / "python" / "python.exe"


def runtime_scripts() -> Path:
    return install_dir() / "runtime" / "python" / "Scripts"


def runtime_java() -> Path:
    return install_dir() / "runtime" / "jre" / "bin" / ("java.exe" if IS_WINDOWS else "java")


def helper_script() -> Path:
    return install_dir() / "update_helper.ps1"


def _creationflags():
    return CREATE_NO_WINDOW if IS_WINDOWS else 0


# ---------------------------------------------------------------------------
# updater.log
# ---------------------------------------------------------------------------
def log(message, component="app", level="INFO"):
    """Append one line to updater.log. Format: [ISO8601] [component] LEVEL message"""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] [{component}] {level} {message}\n"
    try:
        with open(updater_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    return line.rstrip()


def log_tail(lines=40):
    try:
        text = updater_log_path().read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return "(no updater.log yet)"
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------------------
# version.json
# ---------------------------------------------------------------------------
def read_version_json():
    # utf-8-sig: PowerShell's Set-Content writes a BOM, and a stray BOM would
    # otherwise make json.loads fail and hide the app's own version.
    try:
        data = json.loads(version_json_path().read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_version_json(data):
    version_json_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def configured_repo():
    repo = str(read_version_json().get("repo") or DEFAULT_REPO).strip()
    return repo


def repo_is_configured():
    repo = configured_repo()
    return bool(repo) and "YOUR-GITHUB-USERNAME" not in repo


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------
@dataclass
class Component:
    key: str
    name: str
    current: str = ""
    latest: str = ""
    status: str = CHECKING
    notes: str = ""
    checked: str = ""
    url: str = ""
    extra: dict = field(default_factory=dict)


def _run(cmd, timeout=120):
    """Run a command, return (returncode, merged output). Never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace",
                              creationflags=_creationflags())
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return -1, "executable not found"
    except subprocess.TimeoutExpired:
        return -1, "timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return -1, f"{type(exc).__name__}: {exc}"


def python_for_cli(cli_path):
    """Map .../Scripts/opendataloader-pdf.exe back to that environment's python.exe.

    The engine is frequently installed in a different interpreter than the one
    running the GUI (a venv beside the app, say). Reading pip from the GUI's own
    interpreter would then report "not installed" for an engine the app is
    happily using, so resolve pip relative to the CLI we actually found.
    """
    if not cli_path:
        return ""
    try:
        scripts = Path(cli_path).resolve().parent
    except OSError:
        return ""
    for cand in (scripts / "python.exe", scripts.parent / "python.exe",
                 scripts / "python", scripts.parent / "bin" / "python"):
        if cand.is_file():
            return str(cand)
    return ""


def _python_for_pip(cli_path=None):
    """Bundled runtime first, then the CLI's own interpreter, then this one."""
    rp = runtime_python()
    if rp.is_file():
        return str(rp)
    from_cli = python_for_cli(cli_path)
    if from_cli:
        return from_cli
    if not is_frozen():
        return sys.executable
    return ""


def pip_show(package, python_exe=None):
    """Return the parsed `pip show` fields, or None when not installed.

    NOTE: opendataloader-pdf has NO --version flag; calling the CLI with
    --version errors with "input_path required". pip show is the only
    supported way to read the installed engine version.
    """
    py = python_exe or _python_for_pip()
    if not py:
        return None
    code, out = _run([py, "-m", "pip", "show", package], timeout=90)
    if code != 0:
        return None
    fields = {}
    for line in out.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip().lower()] = v.strip()
    return fields if fields.get("version") else None


def exe_version_resource(path=None):
    """Read the Windows version resource of an exe. Returns '' when unavailable."""
    if not IS_WINDOWS:
        return ""
    exe = str(path or sys.executable)
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(exe, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(exe, 0, size, buf):
            return ""
        block = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(block), ctypes.byref(length)):
            return ""
        ffi = ctypes.cast(block, ctypes.POINTER(ctypes.c_uint * 4)).contents
        ms, ls = ffi[2], ffi[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return ""


def parse_java_major(text):
    """Major version from `java -version` output (which goes to STDERR)."""
    m = re.search(r'version\s+"([0-9][0-9._\-a-zA-Z+]*)"', text)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"\b(\d+)\.\d+\.\d+", text)
        if not m:
            return None
        raw = m.group(1)
    parts = re.split(r"[._\-+]", raw)
    try:
        first = int(parts[0])
    except (ValueError, IndexError):
        return None
    if first == 1 and len(parts) > 1:      # legacy 1.8.0_292 -> 8
        try:
            return int(parts[1])
        except ValueError:
            return 1
    return first


def child_env():
    """Environment for CLI subprocesses, with the bundled JRE made findable.

    The opendataloader-pdf wrapper resolves Java from PATH only - setting
    JAVA_HOME alone does nothing (verified: JAVA_HOME-only still fails with
    "'java' command not found", jre\\bin on PATH converts fine). Without this a
    shipped bundle carries a JRE it can never use, and every conversion fails on
    any machine that has no system-wide Java.

    Returns None when there is no bundled JRE, so the caller just inherits the
    environment and falls back to whatever Java is on PATH.
    """
    jre = install_dir() / "runtime" / "jre"
    java = jre / "bin" / ("java.exe" if IS_WINDOWS else "java")
    if not java.is_file():
        return None
    env = os.environ.copy()
    env["PATH"] = str(jre / "bin") + os.pathsep + env.get("PATH", "")
    env["JAVA_HOME"] = str(jre)
    return env


def java_executable():
    """Bundled JRE first, then PATH."""
    rj = runtime_java()
    if rj.is_file():
        return str(rj)
    return shutil.which("java") or ""


def detect_app():
    c = Component("app", "App (PDF to Data)")
    vj = read_version_json()
    c.current = str(vj.get("app") or "").strip()
    source = "version.json"
    if not c.current and is_frozen():
        # Only meaningful when frozen: for a source checkout sys.executable is
        # python.exe, whose version resource is Python's, not this app's.
        c.current = exe_version_resource()
        source = "exe version resource"
    if not c.current:
        if not is_frozen():
            c.current = "dev"
            c.status = OK
            c.notes = "running from source - no version.json in this folder"
            return c
        c.current = "unknown"
        c.status = ERROR
        c.notes = "No version.json and no version resource on the exe."
        return c
    c.status = OK
    c.notes = f"from {source}"
    return c


def detect_engine(cli_path=None):
    c = Component("engine", f"Engine ({ENGINE_PKG})")
    info = pip_show(ENGINE_PKG, _python_for_pip(cli_path))
    if info:
        c.current = info["version"]
        c.status = OK
        c.notes = "installed in the bundled runtime" if runtime_python().is_file() else "installed"
    else:
        c.current = "not installed"
        c.status = MISSING
        c.notes = 'Fix: pip install "opendataloader-pdf[hybrid]"'
    c.url = ENGINE_PYPI_PAGE
    return c


def detect_hybrid(cli_path=None):
    c = Component("hybrid", "Hybrid AI extra")
    py = _python_for_pip(cli_path)
    info = pip_show(HYBRID_PKG, py) or pip_show(ENGINE_PKG, py)
    server = runtime_scripts() / "opendataloader-pdf-hybrid.exe"
    if not server.is_file() and cli_path:
        sibling = Path(cli_path).resolve().parent / "opendataloader-pdf-hybrid.exe"
        if sibling.is_file():
            server = sibling
    if server.is_file():
        c.current = (info or {}).get("version", "installed")
        c.status = OK
        c.notes = "local AI server present"
    elif shutil.which("opendataloader-pdf-hybrid"):
        c.current = (info or {}).get("version", "installed")
        c.status = OK
        c.notes = "found on PATH"
    else:
        # Not an error: hybrid is optional.
        c.current = "not installed"
        c.status = MISSING
        c.notes = 'Optional. Fix: pip install "opendataloader-pdf[hybrid]"'
    return c


def detect_java():
    c = Component("java", "Java runtime")
    exe = java_executable()
    if not exe:
        c.current = "not found"
        c.status = MISSING
        c.notes = "Java 11+ required. Bundled JRE missing and none on PATH."
        c.url = "https://adoptium.net/temurin/releases/?version=17"
        return c
    code, out = _run([exe, "-version"], timeout=60)
    major = parse_java_major(out)
    first = next((ln for ln in out.splitlines() if ln.strip()), "").strip()
    if major is None:
        c.current = "unknown"
        c.status = ERROR
        c.notes = f"Could not parse: {first[:60]}"
    elif major < MIN_JAVA:
        c.current = str(major)
        c.status = ERROR
        c.notes = f"Java {major} is too old; {MIN_JAVA}+ required."
        c.url = "https://adoptium.net/temurin/releases/?version=17"
    else:
        c.current = str(major)
        c.status = OK
        c.notes = ("bundled JRE" if exe.lower().startswith(str(install_dir()).lower())
                   else f"from PATH: {exe}")
    return c


def detect_python_runtime():
    c = Component("python", "Python runtime")
    py = runtime_python()
    if py.is_file():
        code, out = _run([str(py), "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"],
                         timeout=60)
        c.current = out.strip().splitlines()[-1] if code == 0 and out.strip() else "error"
        c.status = OK if code == 0 else ERROR
        c.notes = "embedded runtime in the install folder"
    elif not is_frozen():
        c.current = ".".join(map(str, sys.version_info[:3]))
        c.status = OK
        c.notes = "running from source; no embedded runtime"
    else:
        c.current = "not found"
        c.status = MISSING
        c.notes = "runtime\\python is missing - reinstall the app."
    return c


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
def _ssl_context():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


def http_get(url, timeout=NET_TIMEOUT, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def http_json(url, timeout=NET_TIMEOUT):
    with http_get(url, timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _net_error(exc, what):
    if isinstance(exc, urllib.error.HTTPError):
        return UpdateError(f"{what} failed: HTTP {exc.code}.",
                           "Check your internet connection, or try again later.")
    if isinstance(exc, urllib.error.URLError):
        return UpdateError(f"{what} failed: no connection.",
                           "Check your internet connection or proxy settings.")
    return UpdateError(f"{what} failed: {type(exc).__name__}.", str(exc)[:200])


def latest_engine_version():
    """PyPI JSON API - no auth, not rate-limited for this use."""
    try:
        data = http_json(PYPI_JSON.format(pkg=ENGINE_PKG))
    except Exception as exc:
        raise _net_error(exc, "Checking PyPI for the engine version")
    version = str(((data or {}).get("info") or {}).get("version") or "").strip()
    if not version:
        raise UpdateError("PyPI returned no version for opendataloader-pdf.",
                          "Try again later; the package index may be having trouble.")
    return version


def latest_app_release(repo=None):
    """GitHub Releases API, falling back to a static version.json on rate limit.

    Returns dict: version, body, html_url, assets[{name,url,size}], source.
    """
    repo = repo or configured_repo()
    if not repo_is_configured():
        raise UpdateError("No update repository is configured for this build.",
                          "Set \"repo\" in version.json (build_release.ps1 does this).")
    try:
        data = http_json(GITHUB_LATEST.format(repo=repo))
        assets = [{"name": a.get("name", ""),
                   "url": a.get("browser_download_url", ""),
                   "size": int(a.get("size") or 0)}
                  for a in (data.get("assets") or [])]
        return {"version": str(data.get("tag_name") or "").lstrip("vV").strip(),
                "body": data.get("body") or "",
                "html_url": data.get("html_url") or f"https://github.com/{repo}/releases",
                "assets": assets,
                "source": "github-api"}
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            log(f"GitHub API rate-limited (HTTP {exc.code}); using static fallback", "check", "WARN")
            return _static_fallback(repo)
        if exc.code == 404:
            raise UpdateError("No published releases were found for this app.",
                              f"Create a release at https://github.com/{repo}/releases")
        raise _net_error(exc, "Checking GitHub for the latest release")
    except Exception as exc:
        raise _net_error(exc, "Checking GitHub for the latest release")


def _static_fallback(repo):
    try:
        data = http_json(GITHUB_RAW_FALLBACK.format(repo=repo))
    except Exception as exc:
        raise _net_error(exc, "Checking the fallback version file")
    version = str(data.get("app") or "").strip()
    if not version:
        raise UpdateError("The fallback version file has no \"app\" version.",
                          "Publish a version.json on the repo's main branch.")
    assets = []
    if data.get("portable_url"):
        assets.append({"name": os.path.basename(data["portable_url"]),
                       "url": data["portable_url"], "size": 0})
    body = data.get("notes") or ""
    if data.get("sha256"):
        body += f"\nsha256: {data['sha256']}"
    return {"version": version, "body": body,
            "html_url": data.get("notes_url") or f"https://github.com/{repo}/releases",
            "assets": assets, "source": "static-fallback"}


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------
def parse_version(v):
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest, current):
    if not latest or not current:
        return False
    try:
        return parse_version(latest) > parse_version(current)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Hashing / download / staging
# ---------------------------------------------------------------------------
def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# Case-insensitive on purpose: release notes are hand-written and "SHA-256:",
# "sha256 =" and "Sha256:" all appear in the wild.
SHA_RE = re.compile(r"sha[-_ ]?256\s*[:=]\s*([0-9a-fA-F]{64})", re.IGNORECASE)


def parse_sha256(text):
    m = SHA_RE.search(text or "")
    return m.group(1).lower() if m else ""


def download(url, dest, progress=None, cancel=None):
    """Download with progress(bytes_done, total). Returns dest. Removes partial files."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with http_get(url, timeout=60, accept="application/octet-stream") as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    if cancel is not None and cancel():
                        raise UpdateError("Download cancelled.", "")
                    block = resp.read(256 * 1024)
                    if not block:
                        break
                    fh.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
        tmp.replace(dest)
        return dest
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, UpdateError):
            raise
        raise _net_error(exc, "Downloading the update")


def extract_zip(zip_path, stage_dir):
    stage = Path(stage_dir)
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = (stage / member).resolve()
            if not str(target).startswith(str(stage.resolve())):
                raise UpdateError("The update archive contains an unsafe path.",
                                  "Download it again, or report this release as corrupt.")
        zf.extractall(stage)
    # A zip may wrap everything in a single top folder; unwrap it.
    entries = [p for p in stage.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return stage


def _iter_files(root):
    root = Path(root)
    for p in root.rglob("*"):
        if p.is_file():
            yield p.relative_to(root)


SKIP_PREFIXES = (".backup-", ".started-ok", ".update-failed", "settings.json")


def _skip(rel):
    name = rel.as_posix()
    first = name.split("/")[0]
    return first.startswith(".backup-") or name in SKIP_PREFIXES or name.endswith(".old")


def diff_trees(stage_root, install_root):
    """Files that differ by SHA-256 (name alone is not enough) or are new."""
    stage_root, install_root = Path(stage_root), Path(install_root)
    changed = []
    for rel in _iter_files(stage_root):
        if _skip(rel):
            continue
        src, dst = stage_root / rel, install_root / rel
        if not dst.exists():
            changed.append((rel, "new"))
            continue
        try:
            if sha256_file(src) != sha256_file(dst):
                changed.append((rel, "changed"))
        except OSError:
            changed.append((rel, "changed"))
    return changed


# ---------------------------------------------------------------------------
# Backup / rollback
# ---------------------------------------------------------------------------
def make_backup(install_root, rel_paths, replaced_version, new_version):
    """Copy the exact files about to be replaced into .backup-<timestamp>.

    Deliberate deviation from "move": copying leaves the install directory
    fully intact if anything goes wrong before the swap. The manifest records
    hashes so a restore can be verified.
    """
    install_root = Path(install_root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = install_root / f".backup-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    entries = []
    for rel in rel_paths:
        src = install_root / rel
        if not src.is_file():
            continue                      # a brand-new file has nothing to back up
        dst = backup / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        entries.append({"path": Path(rel).as_posix(),
                        "sha256": sha256_file(dst),
                        "size": dst.stat().st_size})
    manifest = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_version_replaced": replaced_version,
        "new_version": new_version,
        "files": entries,
        "version_json": read_version_json(),
    }
    (backup / ".backup-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"backup created: {backup.name} ({len(entries)} files)", "backup")
    return backup


def list_backups(install_root=None):
    root = Path(install_root or install_dir())
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir()
             if p.is_dir() and p.name.startswith(".backup-")
             and (p / ".backup-manifest.json").is_file()]
    return sorted(found, key=lambda p: p.name, reverse=True)


def read_manifest(backup_dir):
    try:
        return json.loads((Path(backup_dir) / ".backup-manifest.json")
                          .read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def restore_backup(backup_dir, install_root=None, log_cb=None):
    """Restore a backup per its manifest and put version.json back."""
    backup_dir = Path(backup_dir)
    install_root = Path(install_root or install_dir())
    manifest = read_manifest(backup_dir)
    files = manifest.get("files") or []
    if not files:
        raise UpdateError("That backup has no manifest entries to restore.",
                          "Reinstall the app if it is not working.")
    restored, failed = 0, []
    for entry in files:
        rel = entry["path"]
        src = backup_dir / rel
        dst = install_root / rel
        if not src.is_file():
            failed.append(rel)
            continue
        try:
            if entry.get("sha256") and sha256_file(src) != entry["sha256"]:
                failed.append(rel + " (hash mismatch)")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and dst.suffix.lower() == ".exe":
                # A running exe cannot be overwritten, but it can be renamed.
                try:
                    dst.replace(dst.with_suffix(dst.suffix + ".old"))
                except OSError:
                    pass
            shutil.copy2(src, dst)
            restored += 1
            if log_cb:
                log_cb(f"restored {rel}")
        except OSError as exc:
            failed.append(f"{rel} ({exc})")
    if manifest.get("version_json"):
        try:
            write_version_json(manifest["version_json"])
        except OSError:
            failed.append("version.json")
    log(f"rollback from {backup_dir.name}: {restored} restored, {len(failed)} failed", "rollback")
    if failed:
        raise UpdateError(f"Rolled back {restored} files but {len(failed)} could not be restored.",
                          "Details: " + ", ".join(failed[:5]))
    return restored


def prune_backups(keep=3, install_root=None):
    for old in list_backups(install_root)[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        log(f"pruned old backup {old.name}", "backup")


# ---------------------------------------------------------------------------
# Startup markers (auto-rollback protocol)
# ---------------------------------------------------------------------------
def write_started_ok(app_version=""):
    try:
        started_ok_path().write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "app": app_version or read_version_json().get("app", ""),
            "pid": os.getpid(),
        }), encoding="utf-8")
    except OSError:
        pass


def take_failure_marker():
    """Read and clear the marker the helper writes after an auto-rollback."""
    p = failure_marker_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {"reason": "The previous update was rolled back."}
    try:
        p.unlink()
    except OSError:
        pass
    return data


# ---------------------------------------------------------------------------
# Engine update
# ---------------------------------------------------------------------------
def update_engine(target_version, log_cb=None, include_hybrid=True, cli_path=None):
    """pip install -U "opendataloader-pdf[hybrid]"==<version> into the bundled runtime."""
    py = _python_for_pip(cli_path)
    if not py:
        raise UpdateError("No Python runtime was found to update the engine with.",
                          "Reinstall the app so runtime\\python is present.")
    baseline = pip_show(ENGINE_PKG, py)
    if not baseline:
        raise UpdateError("Could not read the current engine version, so no update was attempted.",
                          'Fix: run pip install "opendataloader-pdf[hybrid]" in the bundled runtime.')
    spec = f"{ENGINE_PKG}[hybrid]" if include_hybrid else ENGINE_PKG
    if target_version:
        spec = f"{spec}=={target_version}"
    cmd = [py, "-m", "pip", "install", "-U", "--no-input", spec]
    log(f"engine update start: {baseline['version']} -> {target_version or 'latest'}", "engine")
    if log_cb:
        log_cb("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=_creationflags())
    except Exception as exc:
        raise UpdateError(f"Could not start pip: {exc}", "Check that the bundled runtime is intact.")
    if proc.stdout is not None:
        for line in proc.stdout:
            line = line.rstrip()
            if line and log_cb:
                log_cb(line)
    proc.wait()
    if proc.returncode != 0:
        log(f"engine update failed rc={proc.returncode}", "engine", "ERROR")
        raise UpdateError(f"pip exited with code {proc.returncode}; the engine was not changed.",
                          "Open Details for the pip output; a network or version problem is usual.")
    after = pip_show(ENGINE_PKG, py)
    if not after:
        raise UpdateError("pip reported success but the engine can no longer be found.",
                          'Fix: pip install "opendataloader-pdf[hybrid]" in the bundled runtime.')
    log(f"engine update ok: now {after['version']}", "engine")
    return after["version"]


# ---------------------------------------------------------------------------
# App/bundle update
# ---------------------------------------------------------------------------
def find_portable_asset(release, version):
    exact = f"{APP_SLUG}-{version}-portable.zip"
    for a in release.get("assets") or []:
        if a.get("name", "").lower() == exact.lower():
            return a
    for a in release.get("assets") or []:
        name = a.get("name", "").lower()
        if name.endswith(".zip") and "portable" in name:
            return a
    raise UpdateError(f"The release has no {exact} asset to download.",
                      "Attach the portable zip to the GitHub release and try again.")


def expected_sha256(release):
    """Hash from the release body, else "next_sha256" in the installed version.json."""
    digest = parse_sha256(release.get("body", ""))
    if digest:
        return digest, "release notes"
    digest = str(read_version_json().get("next_sha256") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        return digest, "version.json next_sha256"
    return "", ""


def prepare_app_update(release, progress=None, log_cb=None, cancel=None):
    """Download + verify + stage + diff. Makes NO changes to the install dir.

    Returns dict(stage_root, changed, version, zip_path).
    """
    version = release["version"]
    asset = find_portable_asset(release, version)
    tmp = Path(tempfile.gettempdir()) / f"{APP_SLUG}-stage-{version}"
    zip_path = Path(tempfile.gettempdir()) / asset["name"]

    if log_cb:
        log_cb(f"Downloading {asset['name']}…")
    download(asset["url"], zip_path, progress=progress, cancel=cancel)

    want, source = expected_sha256(release)
    got = sha256_file(zip_path)
    if not want:
        raise UpdateError("This release publishes no SHA-256, so the download cannot be verified.",
                          'Add a line "sha256: <hex>" to the release notes, then retry.')
    if got.lower() != want.lower():
        log(f"sha mismatch: got {got} want {want}", "app", "ERROR")
        try:
            zip_path.unlink()
        except OSError:
            pass
        raise UpdateError("The download did not match the published SHA-256 and was discarded.",
                          f"Nothing on your machine was changed. (checked against {source})")
    if log_cb:
        log_cb(f"SHA-256 verified against {source}.")

    stage_root = extract_zip(zip_path, tmp)
    changed = diff_trees(stage_root, install_dir())
    if log_cb:
        log_cb(f"{len(changed)} file(s) differ from the installed copy.")
    log(f"staged {version}: {len(changed)} changed files", "app")
    return {"stage_root": stage_root, "changed": changed, "version": version, "zip_path": zip_path}


def apply_app_update(prepared, current_version, log_cb=None):
    """Back up, write the plan, and hand off to the bootstrapper. Exits the app."""
    stage_root = Path(prepared["stage_root"])
    changed = prepared["changed"]
    new_version = prepared["version"]
    if not changed:
        raise UpdateError("Every file already matches this release; nothing to update.", "")

    rels = [rel for rel, _ in changed]
    backup = make_backup(install_dir(), rels, current_version, new_version)

    plan = {
        "install_dir": str(install_dir()),
        "stage_dir": str(stage_root),
        "backup_dir": str(backup),
        "new_version": new_version,
        "old_version": current_version,
        "exe_name": Path(sys.executable).name if is_frozen() else f"{APP_SLUG}.exe",
        "app_pid": os.getpid(),
        "files": [Path(r).as_posix() for r in rels],
        "watchdog_sec": 15,
        "log": str(updater_log_path()),
    }
    plan_path = Path(tempfile.gettempdir()) / f"{APP_SLUG}-plan-{new_version}.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    helper = helper_script()
    if not helper.is_file():
        raise UpdateError("update_helper.ps1 is missing from the install folder.",
                          "Reinstall the app; the updater cannot swap files without it.")
    try:
        started_ok_path().unlink(missing_ok=True)
    except OSError:
        pass

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(helper), "-PlanFile", str(plan_path)]
    log(f"handing off to bootstrapper for {new_version} (pid {os.getpid()})", "app")
    if log_cb:
        log_cb("Closing the app so the update can be applied…")
    # DO NOT add DETACHED_PROCESS here. powershell.exe is a console application:
    # with no console at all it fails to initialise and exits without running a
    # single line, while Popen still hands back a PID - so the update silently
    # never happens. CREATE_NO_WINDOW gives it a hidden console, which works, and
    # CREATE_NEW_PROCESS_GROUP keeps Ctrl+C in this app from reaching it. The
    # child outlives us on Windows regardless.
    creation = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WINDOWS else 0
    subprocess.Popen(cmd, creationflags=creation, close_fds=True,
                     cwd=str(install_dir()))
    return plan_path


# ---------------------------------------------------------------------------
# Daily auto-check bookkeeping
# ---------------------------------------------------------------------------
def should_auto_check(last_iso, hours=24):
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(str(last_iso))
    except ValueError:
        return True
    if last.tzinfo:
        last = last.replace(tzinfo=None)
    return datetime.now() - last >= timedelta(hours=hours)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")
