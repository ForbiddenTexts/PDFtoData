Fixes conversions failing on machines without a system-wide Java.

**If you are on 1.0.0 or 1.0.1, you must download this build manually.** Those
versions cannot update themselves (see below), so the in-app **Update app**
button will not work for them. From 1.0.2 onward self-update works normally.

### Fixed

- **Conversions failed with a bare "exited with code 1" on any machine that had
  no Java installed system-wide.** The bundle ships its own Java runtime, but it
  was only used for the Environment tab's status row - the conversion process
  never saw it. So the app reported Java as present and green while every
  conversion failed. The bundled runtime is now put on the conversion process's
  PATH, for both Local and Hybrid modes.
- **Failures now explain themselves.** A non-zero exit used to show only an exit
  code. It now says what went wrong: a missing Java runtime links to Adoptium, a
  silent failure points at the Environment tab, and if a bundled runtime is
  present the message says the app is at fault instead of blaming you.
- **The in-app updater never ran at all.** The helper that swaps files after the
  app closes was launched in a way that made PowerShell exit before executing a
  single line, with no error surfaced - so "Update app" closed the app and did
  nothing. This is why 1.0.0 and 1.0.1 cannot update themselves.

### Install

- **New users:** download `PDFtoData-Setup-1.0.2.exe` and run it. It installs
  per-user under `%LOCALAPPDATA%\Programs\PDFtoData`, so there is no admin
  prompt. The exe is unsigned, so Windows SmartScreen shows "Windows protected
  your PC" - choose **More info -> Run anyway**.
- **Portable:** unzip `PDFtoData-1.0.2-portable.zip` anywhere and run
  `PDFtoData.exe`. Nothing else to install.

Java 17 and Python 3.11 with the conversion engine are bundled. The first Hybrid
(AI) conversion downloads its models from HuggingFace into
`%USERPROFILE%\.cache\huggingface` and runs fully offline afterwards.

sha256: eb1571b4500772ae7b2ed4f207007cf631093fdf51d549df27d6636d59b9ced6
