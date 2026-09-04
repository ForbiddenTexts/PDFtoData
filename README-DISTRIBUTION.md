# PDF to Data — distribution and self-update

How to turn this project into something you can hand to someone who has never
installed Python or Java, and how the in-app updater keeps it current afterwards.

---

## What ships

```
<InstallDir>\                        %LOCALAPPDATA%\Programs\PDFtoData by default
  PDFtoData.exe                      the GUI (PyInstaller onefile)
  update_helper.ps1                  post-exit file swap + rollback watchdog
  version.json                       what this install is
  LICENSE.txt
  runtime\
    python\                          embedded CPython 3.11 + opendataloader-pdf[hybrid]
    jre\                             portable Temurin JRE 17
```

The app looks in `runtime\python\Scripts` for the CLI and `runtime\jre\bin\java.exe`
for Java **before** anything on `PATH`, so a shipped copy never picks up some other
Python on the machine.

User data lives somewhere else entirely and is never touched by an update or an
uninstall:

```
%APPDATA%\PDFtoData\settings.json    settings + file queue
%LOCALAPPDATA%\PDFtoData\updater.log every update step, ever
```

On first run, a `settings.json` sitting next to an older exe is **copied** (not moved)
into `%APPDATA%\PDFtoData`, so the old install keeps working.

### Size, honestly

The `[hybrid]` extra pulls in torch, docling and easyocr. On this machine that
environment measures **1,307 MB installed**, so expect a portable zip in the
several-hundred-MB range and an installer to match. If your users only need Local
mode, build with `-NoHybrid` for a fraction of the size; they can add hybrid later
from the Updates tab.

---

## Building a release

```powershell
.\build_release.ps1 -Version 1.0.0 -ChangelogFile notes.md
```

Steps: PyInstaller → assemble bundle (embedded Python + pip install engine + JRE +
`version.json`) → zip → Inno Setup → optional draft GitHub release.

Useful switches:

| Switch | Effect |
| --- | --- |
| `-SkipRelease` | build only, never touch GitHub |
| `-NoHybrid` | slim bundle, `opendataloader-pdf` without the AI extra |
| `-Repo owner/name` | written into `version.json`; the updater checks this repo |

The script prints a line like `sha256: 3f2a…` and also writes
`dist\PDFtoData-<ver>-portable.zip.sha256`. **Paste that line into the GitHub
release notes.** The in-app updater refuses to install a download whose hash it
cannot verify, so a release without it can be downloaded but never applied.

Two prerequisites are not bundled: [Inno Setup 6](https://jrsoftware.org/isdl.php)
for the wizard and the [GitHub CLI](https://cli.github.com/) for the release step.
Both are installed on this machine (Inno Setup 6.7.3, gh 2.100.0). The script
searches their standard install locations as well as `PATH`, because a tool
installed *after* your shell opened is on the machine PATH but invisible to that
shell — which would otherwise skip the step with no obvious reason.

`gh` also needs `gh auth login` once. The script checks and stops early with the
assets left ready to attach by hand, rather than failing mid-publish.

### Embedded Python gotcha

`python-3.11.9-embed-amd64.zip` ships with `import site` **commented out** in
`python311._pth`. If you leave it that way, `pip install` appears to succeed but
nothing is ever importable and the CLI silently does not exist. `build_release.ps1`
uncomments it and adds `Lib\site-packages`; if you assemble a bundle by hand, do
the same.

---

## First release checklist

1. Create the repo `github.com/ForbiddenTexts/PDFtoData` and push the source.
2. Build: `.\build_release.ps1 -Version 1.0.0 -Repo ForbiddenTexts/PDFtoData -SkipRelease`
3. Create a release tagged `v1.0.0`, attach **both** `PDFtoData-Setup-1.0.0.exe`
   and `PDFtoData-1.0.0-portable.zip`, and put the `sha256:` line in the notes.
4. Share the **setup exe** link. That is all an end user needs.

Optional rate-limit fallback: commit a `version.json` to the repo's `main` branch:

```json
{
  "app": "1.0.0",
  "sha256": "<hash of the portable zip>",
  "portable_url": "https://github.com/ForbiddenTexts/PDFtoData/releases/download/v1.0.0/PDFtoData-1.0.0-portable.zip",
  "notes_url": "https://github.com/ForbiddenTexts/PDFtoData/releases/tag/v1.0.0"
}
```

Unauthenticated GitHub API calls are limited to 60/hour per IP. On a 403 the updater
silently falls back to this raw file, which has no such limit.

---

## What end users see

**Installing.** Double-click `PDFtoData-Setup-<ver>.exe` → welcome → license →
install location → progress → shortcuts → finish, with a "launch now" checkbox.
No admin prompt: it installs per-user under `%LOCALAPPDATA%\Programs`. That is a
deliberate choice — it means the updater can replace files later without UAC.

**SmartScreen.** The exe is unsigned, so the first run of a newly published build
shows *"Windows protected your PC"*. Users must click **More info → Run anyway**.
Tell them this up front; an unexplained security warning is what makes people
abandon an install. To remove it properly you need an OV or EV code-signing
certificate (roughly $200–500/year from Sectigo, DigiCert and others), then sign
with `signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 ...`
in `build_release.ps1` after PyInstaller and again on the setup exe. An OV
certificate still needs reputation to accumulate before the warning stops; an EV
certificate clears it immediately. There is no free way to silence SmartScreen.

**First hybrid run.** The AI models are not in the installer. The first hybrid
conversion downloads a few hundred MB from HuggingFace into
`%USERPROFILE%\.cache\huggingface` and can take minutes; afterwards it is fast and
fully offline. Nothing is ever sent to a cloud service.

**Updating.** The Updates tab shows one row per component. "Update engine" runs pip
inside the bundled runtime and touches nothing else. "Update app" shows the release
notes, downloads the zip with a progress bar, verifies SHA-256, backs up every file
it will replace, closes the app, swaps the files, and relaunches. If the new version
fails to start within 15 seconds the previous one is restored automatically and the
app explains what happened on next launch.

---

## File formats

### `version.json` (install dir)

```json
{
  "app": "1.0.0",
  "engine": "2.5.7",
  "jre": 17,
  "built": "2026-09-03T22:10:00Z",
  "repo": "you/PDFtoData",
  "next_sha256": "optional 64-hex hash of the NEXT release's zip"
}
```

| Field | Meaning |
| --- | --- |
| `app` | bundle version; the authority for "Current" in the Updates tab |
| `engine` | engine version at build time (live value always comes from `pip show`) |
| `jre` | bundled Java major version |
| `built` | UTC ISO-8601 build timestamp |
| `repo` | `owner/name` the updater checks; a placeholder disables app updates gracefully |
| `next_sha256` | optional pre-published hash, used when release notes carry none |

Written BOM-less UTF-8. The rollback path in `update_helper.ps1` rewrites this file
from the backup manifest, also BOM-less — PowerShell's `Set-Content -Encoding utf8`
would add a BOM that breaks `json.loads` on the Python side.

### `.started-ok` (install dir)

Written by the app every time it finishes starting:

```json
{"at": "2026-09-03T22:15:00+00:00", "app": "1.0.0", "pid": 12345}
```

The bootstrapper deletes it before relaunching and waits 15 s for it to reappear.
No file ⇒ the new build did not start ⇒ automatic rollback. Safe to delete.

### `.backup-manifest.json` (inside `.backup-<timestamp>\`)

```json
{
  "created": "2026-09-03T22:20:00+00:00",
  "app_version_replaced": "1.0.0",
  "new_version": "1.1.0",
  "files": [{"path": "PDFtoData.exe", "sha256": "<64 hex>", "size": 9123456}],
  "version_json": { "...the version.json being replaced..." }
}
```

Paths are relative and forward-slashed. Every entry carries the hash of the
**backed-up copy**, and a restore verifies it before overwriting, so a corrupted
backup fails loudly instead of installing garbage. `version_json` lets a rollback
restore the version number as well as the files. Only files that already existed
are listed — a brand-new file has nothing to preserve, and rollback simply leaves
it in place.

The three newest backups are kept; older ones are pruned after a successful update.

### `updater.log` (`%LOCALAPPDATA%\PDFtoData\`)

```
[2026-09-03T22:20:01] [app]      INFO  staged 1.1.0: 3 changed files
[2026-09-03T22:20:02] [backup]   INFO  backup created: .backup-20260903-222002 (3 files)
[2026-09-03T22:20:03] [helper]   INFO  === update start: 1.0.0 -> 1.1.0 ===
[2026-09-03T22:20:05] [helper]   INFO  swapped 3 file(s)
[2026-09-03T22:20:09] [helper]   ERROR FAILURE: the new version did not start within 15s
[2026-09-03T22:20:10] [helper]   INFO  rollback complete; failure marker written
```

`[ISO8601] [component] LEVEL message`. Components: `app`, `engine`, `check`,
`backup`, `rollback`, `helper`. Levels: `INFO`, `WARN`, `ERROR`. Written BOM-less
UTF-8 and appended by both Python and PowerShell. The Details pane of any update
error shows the last 60 lines. Append-only and never rotated; delete it freely.

### `.update-failed.json` (install dir)

Written by the bootstrapper after an automatic rollback; the app reads it once on
startup, shows the explanation, and deletes it.

---

## Update safety rules

These are enforced in code, not just documented:

1. **`updater.py` is the only thing that writes to the install dir.**
2. **Nothing is overwritten before a verified backup exists.** The backup copies
   (rather than moves) the originals, so an abort mid-way leaves the install intact.
3. **Downloads are hash-checked before extraction.** A mismatch aborts and deletes
   the download; nothing on disk changes.
4. **Staging is separate.** Archives extract to `%TEMP%\PDFtoData-stage-<ver>`, never
   over the live install, and zip entries that escape the staging root are rejected.
5. **Diffs are by SHA-256, not filename**, so unchanged files are never rewritten.
6. **Updates are refused while a conversion runs or the hybrid server is alive.**
7. **User data is out of reach** — it is not in the install dir, and `settings.json`
   is excluded from diffing regardless.
8. **A failed launch rolls back by itself**, on a 15-second watchdog.
9. **If the app will not exit, the helper aborts** after 60 s and changes nothing.

### Recovering by hand

Everything is plain files, so nothing here requires the app to work:

- Restore manually: copy the contents of the newest `.backup-*` folder over the
  install dir.
- `PDFtoData.exe.old` is the previous exe, kept until the next update.
- Worst case, re-run the setup exe over the top; your settings are in `%APPDATA%`
  and are not affected.

---

## Verification status

The full pipeline has been run end to end on Windows 11 with Inno Setup 6.7.3
(installed per-user via winget, no admin) and PyInstaller 6.20:

| Artifact | Size | Status |
| --- | --- | --- |
| `PDFtoData.exe` | 10 MB | built, launches, reads `version.json` |
| `PDFtoData-1.0.0-portable.zip` | 546 MB | built, SHA-256 matches the published hash |
| `PDFtoData-Setup-1.0.0.exe` | 343 MB | built, installs per-user in ~1 min, uninstalls clean |

Verified on a real silent install (`/VERYSILENT /DIR=... /NOICONS`), then removed:

- bundle layout is exactly `PDFtoData.exe` + `runtime\python` + `runtime\jre` +
  `version.json` + `update_helper.ps1`, 1.7 GB installed
- **a PDF converted using only the bundled runtime** — embedded Python 3.11.9,
  pip-installed engine 2.5.7, bundled Temurin JRE 17 — with no system Python or
  Java involved, which is what proves the `import site` `_pth` patch worked
- the frozen exe starts and writes `.started-ok` with the right version, so the
  auto-rollback protocol is live in the shipped build
- component detection against the installed bundle resolves the **bundled** Python
  and JRE ahead of anything on `PATH`
- uninstall leaves no files behind, and `%APPDATA%\PDFtoData\settings.json`
  survives, as intended

Also unit-tested: backup/restore round-trip, SHA-256 diffing, zip-traversal
rejection, `pip show` detection, the live PyPI lookup, graceful behaviour with no
repo configured, and the bootstrapper in three scenarios — successful swap,
watchdog rollback of a build that never starts, and abort when the app will not exit.

**Still unproven:** the `gh release create` step (the GitHub CLI is not installed
here) and therefore the whole in-app *app-update* download path against a real
release. Engine updates, backup, rollback and the file swap are all tested; what
has never run is "fetch a real GitHub release and install it". Cut a `v0.0.1`
test release and update to it from a `v0.0.0` install before trusting that path.

**Current artifacts in `dist/`** were built with `-Repo ForbiddenTexts/PDFtoData`:
`version.json` is BOM-less, carries `"repo": "ForbiddenTexts/PDFtoData"`, and the zip's
SHA-256 is `17008a64b8fd657b6c2c74ee6e154d41d53a011446ed015e8f6f7b5ed3109558`.
App-update checks are therefore live and point at that repo. Until the repo has a
published release, the Updates tab shows the App row as *"No published releases
were found for this app"* with a link to create one — which is the expected state,
not an error.
