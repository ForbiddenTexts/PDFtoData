# PDF to Data

A small Windows desktop app that wraps the `opendataloader-pdf` command-line tool, so you can
turn PDFs into AI-ready Markdown / JSON / HTML / text without typing a single command.

The app does **not** parse PDFs itself. It builds command lines and shells out to the real CLI:

- `opendataloader-pdf` — the base client
- `opendataloader-pdf-hybrid` — the local AI server used by Hybrid mode

Everything runs on your machine. Hybrid mode uses open-source local models (Docling, SmolVLM):
no cloud, no API key. The app starts the hybrid server bound to `127.0.0.1`, so it is not
reachable from your network.

---

## Prerequisites

1. **Java 11 or newer** — the extraction engine is Java. Check with:

   ```bash
   java -version
   ```

   If it is missing or older than 11, install a JDK/JRE from
   [Adoptium Temurin](https://adoptium.net/temurin/releases/?version=11).

2. **Python 3.10+**

3. **The CLI itself:**

   ```bash
   pip install opendataloader-pdf
   ```

   For Hybrid (AI) mode you need the extra, which adds the `opendataloader-pdf-hybrid`
   server and its AI dependencies:

   ```bash
   pip install "opendataloader-pdf[hybrid]"
   ```

4. **Optional — drag and drop:**

   ```bash
   pip install tkinterdnd2
   ```

   Without it the app still works; you just use the **Browse…** buttons instead of dragging.

The **6. Environment** tab checks all of this for you and shows the exact fix for anything missing.

---

## How to run

```bash
python app.py
```

**The interpreter running the GUI does not have to be the one holding the CLI.** The app resolves
`opendataloader-pdf.exe` itself, so the two can live in different environments. It searches, in
order: the current interpreter's `Scripts` folder, `%VIRTUAL_ENV%`, the base interpreter behind a
venv, then `.venv` / `venv` / `env` beside the app, in the current folder, and in your home
folder - and finally `PATH`.

That matters because the two pieces often land in different places. On this machine, for example:

| | `opendataloader-pdf` | `tkinterdnd2` |
| --- | --- | --- |
| global Python 3.10 | no | **yes** |
| `C:\Users\<you>\.venv` | **yes** | no |

Running plain `python app.py` uses the global interpreter, which gets you drag-and-drop *and*
finds the CLI in `.venv` automatically. Both combinations are tested and work; the only difference
is that drag-and-drop needs `tkinterdnd2` in whichever interpreter you launch with. Without it the
app says so in the log and the **Browse...** buttons do the same job.

If auto-detection ever fails, set the CLI paths by hand on the **Environment** tab - they are
remembered.

Settings (including the file queue) are saved to `settings.json` next to `app.py` and reloaded
at startup.

---

## Using it

| Tab | What it does |
| --- | --- |
| **1. Input** | Drag PDFs in or use Browse. A folder adds every PDF inside it, recursively. Non-PDFs and duplicates are warned about, not fatal. |
| **2. Mode** | *Local* = fast, rule-based, no AI. *Hybrid* = sends pages to the local AI server for better tables, formulas and image descriptions. |
| **3. Output** | Pick any combination of markdown / json / html / text, plus an optional accessibility-tagged PDF. Default output folder is `<folder of the first PDF>\output`. |
| **4. Advanced** | Pages, threads, table method, reading order, header/footer, strikethrough, sanitize, line breaks, password, image extraction, DPI, space ratio, and a free-form extra-arguments box. |
| **5. Hybrid server** | Start/Stop with a red/amber/green status dot. Convert in Hybrid mode auto-starts it if it is off. |
| **6. Environment** | Green/red rows for Java 11+, the base CLI, and the hybrid extra, each with a one-line fix. |
| **7. Log** | Timestamped app events and the CLI's raw stdout/stderr. Clear and Save log… |

Every control has a hover tooltip explaining what it does.

Conversion runs on a background thread, so the window never freezes, and **Stop** terminates the
running CLI process.

### Notes on the hybrid options

- **Deep mode** is the client flag `--hybrid-mode full`. Formula extraction and image descriptions
  require it, so the app turns it on automatically when either is selected.
- **Scanned/OCR**, **OCR languages**, **formula extraction** and **image descriptions** are
  *server* flags. They are applied when the server starts — if it is already running, stop and
  start it again to pick up a change.
- `--use-struct-tree` (Advanced) takes precedence over hybrid on tagged PDFs. The app logs a
  warning when you combine them.
- **Fall back to local extraction** (`--hybrid-fallback`) and **Request timeout**
  (`--hybrid-timeout`) let a hybrid run survive a slow or failing backend instead of producing
  nothing.

### Scanned PDFs

A scanned PDF is just page images with no text layer, so Local mode has nothing to extract.
Tick **Scanned document / force OCR** and set **OCR languages** (default `en`), then convert in
Hybrid mode. Measured on this machine, three 200-DPI grayscale page images:

| | Local | Hybrid + force OCR |
| --- | --- | --- |
| 1851 book scan, 3 pages | 112 chars, 2.2s | **3,845 chars**, 41.7s |
| clean modern page | 0 chars | **100% accurate**, 18.4s |

OCR accuracy tracks the quality of the scan, not the app. On the clean modern page every line came
back exactly right, headings included. On the 175-year-old book scan the text is recovered and
readable but visibly noisy - `childrcn` for "children", `thc` for "the", `elavery` for "slavery" -
which is normal for EasyOCR on period typography and foxed paper. Budget for a proofread on old
material; expect near-perfect results on modern documents.

Note the 41.7s figure *includes* EasyOCR downloading its recognition model mid-run, and it still
completed - so a cold start does not automatically doom a run.

### Your first hybrid run

The server itself starts in about a second, but it fetches its AI models from HuggingFace
**on the first conversion request**, not at startup - a few hundred MB, cached afterwards under
`%USERPROFILE%\.cache\huggingface`. In testing, the first cold hybrid conversion produced no
output while that download was in flight; the identical run immediately afterwards succeeded in
about 10 seconds and every run since has been fast.

So for the first hybrid conversion: tick **Fall back to local extraction**, and if it still comes
back empty, just run it again - the models are cached by then. Once warm, hybrid is reliable.

---

## Building a standalone .exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "PDFtoData" app.py
```

The executable lands in `dist\PDFtoData.exe`.

**Be honest with yourself about what this gets you.** The `.exe` bundles only the GUI. It does
**not** bundle the PDF engine, so on any machine you run it on you still need:

- **Java 11+** installed and on `PATH`,
- **`opendataloader-pdf` installed** in a Python environment the app can find (or the path set
  manually on the Environment tab),
- **`opendataloader-pdf[hybrid]`** as well, if you want Hybrid mode.

It is a convenience wrapper, not a self-contained installer. If you bundle `tkinterdnd2`,
PyInstaller sometimes misses its Tcl files; add `--collect-all tkinterdnd2` if drag-and-drop
stops working in the frozen build.

---

## Files

```
app.py             the whole application
requirements.txt   tkinterdnd2 only (optional); everything else is standard library
settings.json      created on first run; your last-used settings
```
