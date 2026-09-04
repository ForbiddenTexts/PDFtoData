Fixes conversions failing with "exited with code 1" on every machine except the
one the build was made on.

**If you are on any earlier version, download this build.** 1.0.0 and 1.0.1
cannot update themselves at all; 1.0.2 and 1.0.3 can, but their conversion is
broken, so a fresh download is the reliable route.

### Fixed

- **Conversions failed with exit code 1 and no error message.** The bundle
  invoked the engine through the small `.exe` shim that pip generates, and those
  shims have the absolute path of the interpreter they were installed with
  written inside them. On the machine the release was built on that path exists,
  so everything appeared to work; anywhere else the shim cannot start and exits
  silently before producing any output. The app now runs the engine as a module
  with its own bundled interpreter, which does not depend on where the folder
  lives.
- **The Environment tab now actually starts the engine** instead of reporting it
  as present because a file exists. That check was showing a green
  "opendataloader-pdf: OK" line while the engine was completely unable to run,
  which made the failure much harder to place. If the engine cannot start, the
  tab now says so and Convert explains why.

### Install

- **New users:** download `PDFtoData-Setup-1.0.4.exe` and run it. It installs
  per-user under `%LOCALAPPDATA%\Programs\PDFtoData`, so there is no admin
  prompt. The exe is unsigned, so Windows SmartScreen shows "Windows protected
  your PC" - choose **More info -> Run anyway**.
- **Portable:** unzip `PDFtoData-1.0.4-portable.zip` anywhere and run
  `PDFtoData.exe`.

Java 17 and Python 3.11 with the conversion engine are bundled; nothing else to
install. This build was verified by extracting the zip to a clean location, with
no Python or Java on PATH and no access to the build directory, and converting a
PDF from a standing start.

sha256: aa21925730bbc8dd5db7b81eeac614c3fd7934b46ca48923fcf98cbdee9c1ec4
