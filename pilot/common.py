"""Shared helpers: paths, message loading, OpenRouter calls, redaction, cost.

Privacy rule: rows whose intended_level is "acute", and every model output about them,
are written ONLY under pilot/local/ (gitignored). Nothing here ever writes method/means detail:
every saved model text goes through redact() first.
"""
from __future__ import annotations
import csv, json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL = HERE / "local"
PUBLIC_MESSAGES = HERE / "messages.csv"            # tracked: none/low/elevated only
ACUTE_MESSAGES = LOCAL / "messages_acute.csv"      # gitignored
CONFIG = HERE / "config.json"
LEVELS = ("none", "low", "elevated", "acute")
BASE = "https://openrouter.ai/api/v1"

WITHHELD = "[withheld"   # public messages.csv has placeholder rows with this text for the acute-level items

def load_messages(include_acute: bool = True):
    """Public rows, plus the real acute rows if (and only if) pilot/local/messages_acute.csv exists.
    Without the local file the withheld placeholder rows are skipped and the run covers the rest."""
    rows = list(csv.DictReader(open(PUBLIC_MESSAGES, encoding="utf-8")))
    withheld = [r for r in rows if r["text"].startswith(WITHHELD)]
    rows = [r for r in rows if not r["text"].startswith(WITHHELD)]
    if include_acute:
        if ACUTE_MESSAGES.exists():
            rows += list(csv.DictReader(open(ACUTE_MESSAGES, encoding="utf-8")))
        elif withheld:
            import sys
            print(f"note: {len(withheld)} acute-level items are withheld in this public copy; "
                  f"running on the other {len(rows)} messages.", file=sys.stderr)
    for r in rows:
        r["is_hard_negative"] = str(r.get("is_hard_negative", "")).strip().lower() == "true"
        r["intended_level"] = r["intended_level"].strip().lower()
        assert r["intended_level"] in LEVELS, r["id"]
    return sorted(rows, key=lambda r: r["id"])

def gloss_of(r) -> str:
    n = r.get("notes", "") or ""
    return n.split("Gloss:", 1)[1].split("\n")[0].strip() if "Gloss:" in n else n

OUT = Path(os.environ.get("PILOT_OUT", HERE))      # tests redirect outputs here; default = this folder
OUT_LOCAL = OUT / "local"

def out_paths(level: str, name: str):
    """results routing: acute -> local/ (gitignored), everything else -> pilot/ (tracked)."""
    return (OUT_LOCAL / f"{name}_acute.jsonl") if level == "acute" else (OUT / f"{name}.jsonl")

# ---- redaction: never keep method/means detail ----------------------------------------------
_TERMS = re.compile(
    r"\b(overdos\w*|pills?|tablets?|poison\w*|hang(ing|ed)?|rope|noose|jump(ing|ed)?\s+(off|from)|"
    r"gun|knife|blade|wrists?|drown\w*|suffocat\w*|bleach|paracetamol|tramadol|insecticide|sniper|"
    r"cut(ting)?\s+(myself|yourself|wrists?)|lethal|dose[sd]?)\b", re.I)
REDACTION = "[REDACTED: possible method/means content]"

def redact(text: str):
    """Replace any sentence that contains a method/means term. Returns (clean_text, n_redactions)."""
    if not text:
        return text, 0
    parts = re.split(r"(?<=[.!?\n])\s+", text)
    n, out = 0, []
    for p in parts:
        if _TERMS.search(p):
            out.append(REDACTION); n += 1
        else:
            out.append(p)
    return " ".join(out), n

# ---- OpenRouter ------------------------------------------------------------------------------
def api_key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY")
    if not k:
        raise SystemExit("set the OPENROUTER_API_KEY environment variable (create a key at https://openrouter.ai/keys); or add --dry-run to test without a key")
    return k

def _post(body: dict, timeout=90):
    req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + api_key(), "Content-Type": "application/json",
                 "X-Title": "naija-distress-pilot"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def _mock(model_cfg, messages):
    sysmsg = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "classif" in sysmsg.lower():
        text = '{"level": "low", "reason": "mock"}'
    elif "grad" in sysmsg.lower():
        text = '{"recognised": true, "warm": true, "proportionate": true, "safe": true, "note": "mock"}'
    else:
        text = "I hear you. I'm here with you. Tell me more about what's going on?"
    return {"text": text, "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "cost": 0.0,
            "finish": "stop", "temp_used": 0, "attempts": 1}

def chat(model_cfg: dict, messages: list, max_tokens: int, temperature=0, tries=6):
    """One call with retry/backoff. Returns dict(text, usage, cost, finish, temp_used, attempts)."""
    if os.environ.get("PILOT_MOCK") == "1":
        return _mock(model_cfg, messages)
    body = {"model": model_cfg["slug"], "messages": messages, "max_tokens": max_tokens,
            "usage": {"include": True}}
    if model_cfg.get("reasoning") is not None:
        body["reasoning"] = model_cfg["reasoning"]
    use_temp = temperature is not None and model_cfg.get("temperature_ok", True)
    if use_temp:
        body["temperature"] = temperature
    last = None
    for attempt in range(1, tries + 1):
        try:
            data = _post(body)
            if "error" in data:  # OpenRouter can return 200 + error object
                raise RuntimeError(json.dumps(data["error"])[:300])
            ch = data["choices"][0]
            u = data.get("usage", {}) or {}
            cost = u.get("cost")
            if cost is None:
                p = model_cfg.get("pricing", {})
                cost = u.get("prompt_tokens", 0) * p.get("prompt", 0) + u.get("completion_tokens", 0) * p.get("completion", 0)
            return {"text": (ch.get("message", {}).get("content") or ""), "usage": u, "cost": float(cost),
                    "finish": ch.get("finish_reason"), "temp_used": body.get("temperature", "provider-default"),
                    "attempts": attempt}
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:400]
            last = f"HTTP {e.code}: {msg}"
            if e.code == 400 and "temperature" in msg.lower() and "temperature" in body:
                body.pop("temperature")  # model rejects temperature: record it, retry without
                continue
            if e.code == 402 and "in_flight" not in msg:   # out of credits: retrying cannot help
                raise RuntimeError(last)
            if e.code in (400, 401, 403, 404):
                raise RuntimeError(last)
        except Exception as e:  # network, 429/5xx surfaced as HTTPError above are retried below
            last = repr(e)[:300]
        time.sleep(min(2 ** attempt, 30))
    raise RuntimeError("gave up: " + str(last))

def parse_json_obj(text: str):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None

def load_config():
    return json.loads(CONFIG.read_text())

def append_jsonl(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def read_jsonl(path: Path):
    return [json.loads(l) for l in open(path, encoding="utf-8")] if path.exists() else []

