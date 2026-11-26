"""Run the pilot: for each message and model, one RESPONSE call and one DETECTION call.

    python3 pilot/run.py --estimate            # print the cost estimate and exit (no API calls)
    python3 pilot/run.py --run --max-usd 8     # run for real
    python3 pilot/run.py --rerun-incomplete --estimate        # estimate re-running only cut-off/empty replies
    python3 pilot/run.py --rerun-incomplete --run --max-usd 3 # re-run only those (appends new records; logs them)
A reply that is cut off by the token cap (finish != "stop") or empty is a HARNESS ERROR: it is retried once with a
higher cap; if it is still incomplete it is recorded as "incomplete" and excluded from pass rates, never counted
as a model failure. Caps live in config "limits" (response 4000, retry 8000 by default).
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
                    read_jsonl, LEVELS, limits_cfg, harness_problem, latest, OUT, OUT_LOCAL)
from prompts import RESPONSE_SYSTEM, DETECTION_SYSTEM, GRADER_SYSTEM

import os
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
    sysmsg = RESPONSE_SYSTEM if task == "response" else DETECTION_SYSTEM
    return [{"role": "system", "content": sysmsg}, {"role": "user", "content": r["text"]}]

def caps(task, lim):
    return ((lim["response_max_tokens"], lim["response_retry_max_tokens"]) if task == "response"
            else (lim["detection_max_tokens"], lim["detection_retry_max_tokens"]))

def do_one(m, r, task, lim, prior=None):
    """One call; if cut off or empty, ONE retry with the higher cap. Never raises: errors are recorded."""
    msgs = task_messages(task, r); cap, cap2 = caps(task, lim)
    rec = {"id": r["id"], "model": m["slug"], "task": task, "intended_level": r["intended_level"],
           "language": r["language"]}
    meta = {"cap_first": cap, "retried": False, "first_finish": None, "first_ctok": None}
    try:
        out = chat(m, msgs, cap, temperature=0)
        cost = out["cost"]; meta["first_finish"] = out["finish"]; meta["first_ctok"] = out["usage"].get("completion_tokens")
        if harness_problem({"finish": out["finish"], "output": out["text"]}):
            meta["retried"] = True
            out = chat(m, msgs, cap2, temperature=0)
            cost += out["cost"]; cap = cap2
        out["cost"] = cost
        incomplete = harness_problem({"finish": out["finish"], "output": out["text"]})
        text, n_red = redact(out["text"])
        rec.update({"output": text, "redactions": n_red, "finish": out["finish"], "temp_used": out["temp_used"],
                    "usage": out["usage"], "cost": cost, "attempts": out["attempts"], "max_tokens": cap,
                    "retried": meta["retried"]})
        if incomplete:
            rec["harness_status"] = "incomplete"      # excluded from pass rates, reported separately
        if prior:
            rec["rerun_of"] = {"finish": prior.get("finish"), "completion_tokens": (prior.get("usage") or {}).get("completion_tokens"),
                               "chars": len(prior.get("output") or "")}
        if task == "detection":
            obj = parse_json_obj(out["text"]) or {}
            lvl = str(obj.get("level", "")).strip().lower()
            rec["level"] = lvl if lvl in LEVELS else "invalid"
            reason, n2 = redact(str(obj.get("reason", "")))
            rec["reason"] = reason; rec["redactions"] += n2
            rec["output"] = json.dumps({"level": rec["level"], "reason": reason}, ensure_ascii=False)
    except Exception as e:
        rec["error"] = str(e)[:300]; rec["cost"] = 0.0
    return rec, meta

LOG_COLS = ["id", "model", "task", "intended_level", "original_finish", "original_completion_tokens", "original_chars",
            "cap_first", "first_finish", "retried", "final_finish", "final_completion_tokens", "final_chars", "final_status", "cost_usd"]

def log_rerun(rec, meta, prior):
    import csv
    path = (OUT_LOCAL / "rerun_log_acute.csv") if rec["intended_level"] == "acute" else (OUT / "rerun_log.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new: w.writerow(LOG_COLS)
        u = rec.get("usage") or {}
        w.writerow([rec["id"], rec["model"], rec["task"], rec["intended_level"], prior.get("finish"),
                    (prior.get("usage") or {}).get("completion_tokens"), len(prior.get("output") or ""),
                    meta["cap_first"], meta["first_finish"], meta["retried"], rec.get("finish"), u.get("completion_tokens"),
                    len(rec.get("output") or ""), ("error: " + rec["error"][:60]) if rec.get("error") else
                    ("incomplete" if rec.get("harness_status") else "complete"), f"{rec.get('cost', 0.0):.4f}"])

def incomplete_records():
    """Latest successful record per (id, model, task), public and local, that is cut off or empty."""
    recs, seen = [], set()
    for lvl in LEVELS:
        p = out_paths(lvl, "results")
        if p in seen: continue
        seen.add(p); recs += [r for r in read_jsonl(p) if not r.get("error")]
    return [r for r in latest(recs, lambda r: (r["id"], r["model"], r["task"])).values() if harness_problem(r)]

def estimate_rerun(recs, cfg, lim):
    """Expected cost = ~half the response cap used per call; worst case = both attempts use their full cap.
    Input tokens are tiny. Regrading each rerun reply is added separately (one short grader call)."""
    gm = {m["slug"]: m for m in cfg["models"]}
    exp = worst = grade = 0.0
    for r in recs:
        c = gm[r["model"]]["pricing"]["completion"]; cap, cap2 = caps(r["task"], lim)
        exp += (0.5 * cap) * c; worst += (cap + cap2) * c
        g = gm[cfg["grader_rotation"][r["model"]]]["pricing"]
        grade += 450 * g["prompt"] + (150 + 300) * g["completion"]     # incl. hidden reasoning allowance
    return exp, worst, grade

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate", action="store_true"); ap.add_argument("--run", action="store_true")
    ap.add_argument("--rerun-incomplete", action="store_true",
                    help="only re-run replies that were cut off (finish != stop) or empty; appends new records and logs them")
    ap.add_argument("--max-usd", type=float, default=8.0); ap.add_argument("--limit", type=int)
    ap.add_argument("--skip-acute", action="store_true"); ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    cfg, rows = load_config(), load_messages(include_acute=not a.skip_acute)
    lim = limits_cfg(cfg); msgs = {r["id"]: r for r in rows}
    if a.rerun_incomplete:
        return rerun(a, cfg, msgs, lim)
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
        rec, _ = do_one(*j, lim)
        with lock:
            if "HTTP 402" in rec.get("error", "") and "in_flight" not in rec.get("error", ""):
                stop[0] = True          # no credits: stop making calls
            spent[0] += rec.get("cost", 0.0)
            if spent[0] > a.max_usd: stop[0] = True
            append_jsonl(out_paths(rec["intended_level"], "results"), rec)
        return rec
    errs = inc = 0
    with ThreadPoolExecutor(a.workers) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            rec = f.result()
            if rec and rec.get("error"): errs += 1
            if rec and rec.get("harness_status"): inc += 1
    print(f"spent ${spent[0]:.3f}; errors {errs}; incomplete after retry {inc}; stopped_for_budget={stop[0]}")

def rerun(a, cfg, msgs, lim):
    recs = [r for r in incomplete_records() if r["id"] in msgs]
    gm = {m["slug"]: m for m in cfg["models"]}
    import collections
    print(f"{len(recs)} cut-off or empty records to re-run (response cap {lim['response_max_tokens']}, "
          f"retry cap {lim['response_retry_max_tokens']}):")
    for (m, lvl, t), n in sorted(collections.Counter((r["model"], r["intended_level"], r["task"]) for r in recs).items()):
        print(f"  {m} | {lvl} | {t}: {n}")
    exp, worst, grade = estimate_rerun(recs, cfg, lim)
    print(f"ESTIMATE rerun calls: expected ~${exp:.2f}, absolute worst case ~${worst:.2f} (every reply uses both caps in full)")
    print(f"ESTIMATE regrading these replies: ~${grade:.2f}")
    print(f"ESTIMATED TOTAL: expected ~${exp + grade:.2f}; worst case ~${worst + grade:.2f}; hard stop at ${a.max_usd:.2f}")
    if not a.run: return
    spent = [0.0]; lock = threading.Lock(); stop = [False]
    def work(prior):
        c = gm[prior["model"]]["pricing"]["completion"]; cap, cap2 = caps(prior["task"], lim)
        with lock:
            if stop[0] or spent[0] + (cap + cap2) * c > a.max_usd: stop[0] = True; return None   # never risk passing the cap
        rec, meta = do_one(gm[prior["model"]], msgs[prior["id"]], prior["task"], lim, prior=prior)
        with lock:
            if "HTTP 402" in rec.get("error", "") and "in_flight" not in rec.get("error", ""): stop[0] = True
            spent[0] += rec.get("cost", 0.0)
            if not rec.get("error"):
                append_jsonl(out_paths(rec["intended_level"], "results"), rec)
            log_rerun(rec, meta, prior)
        return rec
    res = []
    with ThreadPoolExecutor(a.workers) as ex:
        res = [f.result() for f in as_completed([ex.submit(work, p) for p in recs])]
    done = [r for r in res if r]
    print(f"re-ran {len(done)} of {len(recs)}; complete {sum(1 for r in done if not r.get('error') and not r.get('harness_status'))}; "
          f"still incomplete {sum(1 for r in done if r.get('harness_status'))}; errors {sum(1 for r in done if r.get('error'))}; "
          f"spent ${spent[0]:.3f}; stopped_early={stop[0]}")

if __name__ == "__main__":
    main()
