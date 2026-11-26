"""Run the pilot: for each message and model, one RESPONSE call and one DETECTION call.

    python3 pilot/run.py --estimate            # print the cost estimate and exit (no API calls)
    python3 pilot/run.py --run --max-usd 8     # run for real
Acute-level rows and everything about them go to pilot/local/ (gitignored). Saved text is redacted for
method/means content before it is written. temperature 0 unless the model rejects it (recorded per call).
"""
import os as _os, sys as _sys, tempfile as _tf
if "--dry-run" in _sys.argv:                       # no API calls; fake outputs; writes to a temp folder
    _os.environ["PILOT_MOCK"] = "1"
    _os.environ.setdefault("PILOT_OUT", _os.path.join(_tf.gettempdir(), "naija-pilot-dryrun"))
    _os.environ.setdefault("OPENROUTER_API_KEY", "dry-run")
    _sys.argv.remove("--dry-run")
import argparse, json, sys, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from common import (load_messages, load_config, chat, redact, parse_json_obj, out_paths, append_jsonl,
                    read_jsonl, LEVELS)
from prompts import RESPONSE_SYSTEM, DETECTION_SYSTEM, GRADER_SYSTEM

import os
RESP_MAX, DET_MAX = int(os.environ.get('PILOT_RESP_MAX', 900)), int(os.environ.get('PILOT_DET_MAX', 500))   # generous: hidden reasoning tokens count toward max_tokens
REASONING_ALLOWANCE = 300     # assumed hidden reasoning tokens per call (effort=low); real usage is recorded

def done_keys():
    keys, seen = set(), set()
    for lvl in LEVELS:
        p = out_paths(lvl, "results")
        if p in seen: continue
        seen.add(p)
        for r in read_jsonl(p):
            if not r.get("error"):
                keys.add((r["id"], r["model"], r["task"]))
    return keys

def estimate(rows, cfg, margin=1.5, reasoning=REASONING_ALLOWANCE):
    tot_in = tot_out = 0.0
    per_model = {}
    for m in cfg["models"]:
        p = m["pricing"]; cost = 0.0
        for r in rows:
            n = len(r["text"])
            for sysmsg, outtok in ((RESPONSE_SYSTEM, 200), (DETECTION_SYSTEM, 60)):
                tin = (len(sysmsg) + n) / 3.2 + 25
                cost += tin * p["prompt"] + (outtok + reasoning) * p["completion"]
                tot_in += tin; tot_out += outtok
            g = cfg["grader_rotation"][m["slug"]]
            gp = next(x for x in cfg["models"] if x["slug"] == g)["pricing"]
            tin = (len(GRADER_SYSTEM) + n * 2) / 3.2 + 330   # rubric + message + gloss + level + a ~250-token reply
            cost += tin * gp["prompt"] + (80 + reasoning) * gp["completion"]
        per_model[m["slug"]] = cost
    total = sum(per_model.values())
    return per_model, total, total * margin

def task_messages(task, r):
    if task == "response":
        return [{"role": "system", "content": RESPONSE_SYSTEM}, {"role": "user", "content": r["text"]}], RESP_MAX
    return [{"role": "system", "content": DETECTION_SYSTEM}, {"role": "user", "content": r["text"]}], DET_MAX

def do_one(m, r, task):
    msgs, mx = task_messages(task, r)
    rec = {"id": r["id"], "model": m["slug"], "task": task, "intended_level": r["intended_level"],
           "language": r["language"]}
    try:
        out = chat(m, msgs, mx, temperature=0)
        if not (out["text"] or "").strip():
            raise RuntimeError(f"empty output (finish={out['finish']}, completion_tokens={out['usage'].get('completion_tokens')})")
        text, n_red = redact(out["text"])
        rec.update({"output": text, "redactions": n_red, "finish": out["finish"], "temp_used": out["temp_used"],
                    "usage": out["usage"], "cost": out["cost"], "attempts": out["attempts"]})
        if task == "detection":
            obj = parse_json_obj(out["text"]) or {}
            lvl = str(obj.get("level", "")).strip().lower()
            rec["level"] = lvl if lvl in LEVELS else "invalid"
            reason, n2 = redact(str(obj.get("reason", "")))
            rec["reason"] = reason; rec["redactions"] += n2
            rec["output"] = json.dumps({"level": rec["level"], "reason": reason}, ensure_ascii=False)
    except Exception as e:
        rec["error"] = str(e)[:300]; rec["cost"] = 0.0
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true"); ap.add_argument("--run", action="store_true")
    ap.add_argument("--max-usd", type=float, default=8.0); ap.add_argument("--limit", type=int)
    ap.add_argument("--skip-acute", action="store_true"); ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    cfg, rows = load_config(), load_messages(include_acute=not a.skip_acute)
    if a.limit: rows = rows[: a.limit]
    per, total, padded = estimate(rows, cfg)
    _, bare, _ = estimate(rows, cfg, reasoning=0)
    print(f"{len(rows)} messages x {len(cfg['models'])} models x 2 calls = {len(rows)*len(cfg['models'])*2} calls "
          f"(+ {len(rows)*len(cfg['models'])} grading calls in grade.py)")
    for slug, c in per.items(): print(f"  {slug}: ~${c:.3f}")
    print(f"ESTIMATED TOTAL (run + grading), no hidden reasoning: ~${bare:.2f}")
    print(f"ESTIMATED TOTAL assuming ~{REASONING_ALLOWANCE} hidden reasoning tokens per call: ~${total:.2f}; with 1.5x safety margin: ~${padded:.2f}; hard stop at ${a.max_usd:.2f}")
    if not a.run: return
    done = done_keys(); spent = [0.0]; lock = threading.Lock(); stop = [False]
    jobs = [(m, r, t) for r in rows for m in cfg["models"] for t in ("response", "detection")
            if (r["id"], m["slug"], t) not in done]
    print(f"{len(jobs)} calls to make ({len(done)} already done)")
    def work(j):
        if stop[0]: return None
        rec = do_one(*j)
        with lock:
            if "HTTP 402" in rec.get("error", "") and "in_flight" not in rec.get("error", ""):
                stop[0] = True          # no credits: stop making calls
            spent[0] += rec.get("cost", 0.0)
            if spent[0] > a.max_usd: stop[0] = True
            append_jsonl(out_paths(rec["intended_level"], "results"), rec)
        return rec
    errs = 0
    with ThreadPoolExecutor(a.workers) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            rec = f.result()
            if rec and rec.get("error"): errs += 1
    print(f"spent ${spent[0]:.3f}; errors {errs}; stopped_for_budget={stop[0]}")

if __name__ == "__main__":
    main()
