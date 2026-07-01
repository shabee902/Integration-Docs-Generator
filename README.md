# Lobster Profile → Professional PDF Docs (Offline) — v2

**Goal:** Drop XML files in `input/`, get polished HTML/PDF in `output/`, and the XML is moved to `archive/`.
(Optionally fetch XMLs from SharePoint into `input/`.)

## Install (Windows/macOS/Linux)

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements.txt
# macOS/Linux
.venv/bin/pip install -r requirements.txt
```

**Recommended:** Install `wkhtmltopdf` and add it to PATH. If not present, WeasyPrint is tried, then the text-only ReportLab fallback.

## Run the local watcher

```bash
python watch_local.py
```
- Processes every `*.xml` inside `input/` **sequentially**
- Creates `{name}.html` and `{name}.pdf` in `output/`
- Moves processed XML into `archive/`

## Run one-off (single file)

```bash
python processor.py --xml input/your.xml --outdir output --mapping config/lobster_business.yaml
```

## Fetch from SharePoint (optional)

1) Edit `fetch_sharepoint.py` with your tenant URL, folder path, and credentials.
2) Install optional libs:
```bash
pip install Office365-REST-Python-Client requests
```
3) Run:
```bash
python fetch_sharepoint.py
```
Fetched files land in `input/` and will be auto-processed by the watcher.

## Mapping format
We ship `config/lobster_business.yaml`, producing sections:
- Business Context
- Input Data
- Mapping (from `<mappinginformation><mappingentry .../>`)
- Output Data
- Trigger

Customize labels and XPaths as needed.

## Force wkhtmltopdf path
If it’s not on PATH, open `watch_local.py` and set:
```python
WKHTMLTOPDF = r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"
```

Generated: 2025-08-13 13:00
⁣