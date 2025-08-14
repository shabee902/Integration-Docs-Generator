#!/usr/bin/env python3
import time, shutil, sys
from pathlib import Path
import subprocess

INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")
ARCHIVE_DIR = Path("archive")
MAPPING = Path("config/lobster_business.yaml")
TEMPLATES = Path("templates")
WKHTMLTOPDF = r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"

def process_file(xml_path: Path):
    cmd = [sys.executable, "processor.py",
           "--xml", str(xml_path),
           "--outdir", str(OUTPUT_DIR),
           "--mapping", str(MAPPING),
           "--templates", str(TEMPLATES),
           "--wkhtmltopdf", WKHTMLTOPDF]
    print(f"[RUN] {' '.join(cmd)}")
    res = subprocess.run(cmd)
    if res.returncode == 0:
        ARCHIVE_DIR.mkdir(exist_ok=True, parents=True)
        dest = ARCHIVE_DIR / xml_path.name
        shutil.move(str(xml_path), str(dest))
        print(f"[OK] Archived: {dest.name}")
    else:
        print(f"[ERR] Failed processing: {xml_path.name}")

def main():
    INPUT_DIR.mkdir(exist_ok=True, parents=True)
    OUTPUT_DIR.mkdir(exist_ok=True, parents=True)
    ARCHIVE_DIR.mkdir(exist_ok=True, parents=True)
    print("[WATCH] Watching 'input' for XML files. Press Ctrl+C to stop.")
    while True:
        try:
            files = sorted(INPUT_DIR.glob("*.xml"))
            for f in files:
                process_file(f)
            time.sleep(3)
        except KeyboardInterrupt:
            print("\n[STOP] Watcher stopped by user.")
            break

if __name__ == "__main__":
    main()
