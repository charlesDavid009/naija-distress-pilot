"""Guard for any public release: fail if tracked/public files contain acute message text or acute ids.
Run before committing:  python3 pilot/check_public.py     (also installed as a git pre-commit hook)
"""
import csv, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
acute = ROOT / "pilot" / "local" / "messages_acute.csv"
if not acute.exists():
    print("no local acute file; nothing to compare"); sys.exit(0)
rows = list(csv.DictReader(open(acute, encoding="utf-8")))
needles = [r["text"].strip() for r in rows if r["text"].strip()] + [r["id"] for r in rows]
tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=ROOT, capture_output=True, text=True).stdout.split()
bad = []
for f in set(tracked) | set(staged):
    p = ROOT / f
    if not p.is_file() or f.endswith((".gitignore",)):
        continue
    t = p.read_text(encoding="utf-8", errors="ignore")
    for n in needles:
        if len(n) >= 6 and n in t and not (n.startswith("M") and len(n) <= 4):
            bad.append((f, n[:30]))
if bad:
    print("BLOCKED: acute content found in public files:", bad); sys.exit(1)
print("ok: no acute message text found in tracked or staged files")
