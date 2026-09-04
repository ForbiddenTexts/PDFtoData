Test release for the in-app updater.

This is a functional dry run of the update path: it verifies that an installed
v1.0.0 can discover this release, verify the download against the hash below,
back up the files it replaces, swap them, and relaunch.

**Changes since 1.0.0**
- No functional changes. Rebuilt at version 1.0.1 to exercise the update path
  end to end against a real GitHub release.

**Install**
- New users: download `PDFtoData-Setup-1.0.1.exe` and run it. It installs
  per-user under `%LOCALAPPDATA%\Programs\PDFtoData`, so there is no admin
  prompt. The exe is unsigned, so Windows SmartScreen will show
  "Windows protected your PC" — choose **More info → Run anyway**.
- Existing users: open the **Updates** tab and click **Update app**.

**Requirements**
Nothing to install. Java 17 and Python 3.11 with the conversion engine are
bundled. The first Hybrid (AI) conversion downloads its models from HuggingFace
into `%USERPROFILE%\.cache\huggingface` and runs fully offline afterwards.

sha256: 574e9d0bc912512bf6cedc16e67856fed28d4644a134695dc26bb2d820491555
