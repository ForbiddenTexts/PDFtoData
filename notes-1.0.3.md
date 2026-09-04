Fixes the Convert button being missing on laptop-sized screens.

### Fixed

- **The Convert button was cut off the bottom of the window on smaller
  displays.** The button bar was laid out after the panel that fills the
  window, so on a short window it was pushed past the bottom edge and simply
  never drawn. The window also opened at a fixed 980x820 with a minimum size
  large enough that, on a 1366x768 laptop, the button sat below the screen and
  could not be brought back by resizing. The button bar is now pinned to the
  bottom of the window and the window sizes itself to your screen. Verified
  visible from 980x820 down to 700x380.

### If the button is present but greyed out

That is a different situation: the app is telling you something is missing, and
the reason is printed in red next to the button. The usual cause is downloading
only `PDFtoData.exe` on its own, which has no Java or conversion engine with it.
Use `PDFtoData-Setup-1.0.3.exe` or `PDFtoData-1.0.3-portable.zip` instead - both
carry everything needed. The **Environment** tab lists exactly what is missing
and how to fix each item.

### Install

- **New users:** download `PDFtoData-Setup-1.0.3.exe` and run it. It installs
  per-user under `%LOCALAPPDATA%\Programs\PDFtoData`, so there is no admin
  prompt. The exe is unsigned, so Windows SmartScreen shows "Windows protected
  your PC" - choose **More info -> Run anyway**.
- **Portable:** unzip `PDFtoData-1.0.3-portable.zip` anywhere and run
  `PDFtoData.exe`.
- **On 1.0.2:** open the **Updates** tab and click **Update app**.
- **On 1.0.0 or 1.0.1:** those versions cannot update themselves, so download
  this build manually.

Java 17 and Python 3.11 with the conversion engine are bundled. The first Hybrid
(AI) conversion downloads its models from HuggingFace into
`%USERPROFILE%\.cache\huggingface` and runs fully offline afterwards.

sha256: 4357d199bd90d6b178a086ced42fec56b9df2bfb6ad5bb6be7a4a9211c3f5c9d
