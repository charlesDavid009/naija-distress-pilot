"""Split a FULL messages CSV into the public file and the local acute file.

Usage (maintainer only; the full file is never committed): put the full reviewed CSV at
pilot/local/messages_full.csv (gitignored), then:   python3 pilot/split.py
Writes
  pilot/messages.csv                 all ids; acute rows replaced by a placeholder (text withheld, notes blank)
  pilot/local/messages_acute.csv     the real acute rows (gitignored)
"""
import csv, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "local" / "messages_full.csv"
rows = list(csv.DictReader(open(src, encoding="utf-8")))
cols = list(rows[0].keys())
PLACEHOLDER = "[withheld: available to researchers on request]"
pub, acu = [], []
for r in rows:
    if r["intended_level"].strip().lower() == "acute":
        acu.append(r)
        pub.append({**r, "text": PLACEHOLDER, "is_hard_negative": "false", "notes": ""})
    else:
        pub.append(r)
(HERE / "local").mkdir(exist_ok=True)
for path, subset in ((HERE / "messages.csv", pub), (HERE / "local" / "messages_acute.csv", acu)):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(subset)
print(f"public: {len(pub)} rows ({len(acu)} acute withheld) -> pilot/messages.csv; acute: {len(acu)} rows -> pilot/local/messages_acute.csv")
