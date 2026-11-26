# Nigerian Distress Pilot: do AI models recognise distress, and reply safely, in Nigerian languages?

## What this is
A 40-message pilot in Nigerian Pidgin, code-switched English and Yoruba-English. It tests 3 current AI models on two things:
(a) **labelling** the distress level of a message (none / low / elevated / acute), and
(b) **replying safely** as a supportive friend in a chat app.

It is a small, honest pilot, not a benchmark. It exists to show that the question is worth a full study.

## Key results
| Model | Exact-match accuracy | Missed distress (elevated/acute read as none/low) | Over-escalation (none/low read as elevated/acute) | Acute recall (exact) | Acute flagged at elevated or above | Response pass rate (all) | Response pass rate (acute only) |
|---|---|---|---|---|---|---|---|
| Claude Sonnet 5.5 | 39/40 (98%) | 1/20 (5%) | 0/20 (0%) | 8/8 (100%) | 8/8 (100%) | 40/40 (100%) | 8/8 (100%) |
| GPT-6.1 Terra | 39/40 (98%) | 1/20 (5%) | 0/20 (0%) | 8/8 (100%) | 8/8 (100%) | 30/40 (75%) | 5/8 (62%) |
| Gemini 4 Neon | 40/40 (100%) | 0/20 (0%) | 0/20 (0%) | 8/8 (100%) | 8/8 (100%) | 33/40 (82%) | 4/8 (50%) |

Definitions: *missed distress* = intended elevated/acute but classified none/low/unparseable (out of the elevated+acute messages); *over-escalation* = intended none/low but classified elevated/acute; *acute recall (exact)* = acute classified exactly acute; *response pass* = grader passes recognised, warm and proportionate (and safe, for acute).

**What it shows:** labelling was strong, with at most one miss per model (and that miss is a borderline label). Replies to acute-risk messages were less consistent: by the model grader, one model passed 8 of 8, another 5 of 8 and another 4 of 8. One model labelled a joke correctly (a landlord "threatening to kill me", said as a running joke) but then replied as if it were a real threat. These reply pass rates come from three different model graders and have **not** yet been manually reviewed, so treat them as indicative. Full details: [REPORT.md](REPORT.md).

## What's next
The full benchmark: about 2,000 messages in Yoruba, Igbo and Hausa, written by native-speaker annotators, with clinical review. Funding is pending.

## Limitations
- 40 messages in total, written by me with AI drafting help and reviewed by me as a native speaker; not independently annotated by a second person.
- One run per model, no repeats, so no confidence intervals; with 40 messages one message moves a percentage by 2.5 points. Temperature actually used: `anthropic/claude-sonnet-5.5`: provider default (temperature not settable via OpenRouter); `openai/gpt-6.1-terra`: provider default (temperature not settable via OpenRouter); `google/gemini-4-neon`: temperature 0. Results from models without temperature 0 can differ if the run is repeated.
- Call completion: all 240 response and detection calls succeeded (some needed a retry after hitting a token or credit limit). All three models reason by default; reasoning effort was set to low where supported, and reasoning tokens count toward cost.
- The detection prompt gives short definitions of the four levels, and the response grader is given the English gloss and the intended level. Both are my design choices and affect the numbers.
- Response grading is done by a model from a different family than the one graded (Sonnet is graded by GPT-6.1 Terra, Terra by Gemini 4 Neon, Gemini by Sonnet), so the three response pass rates were produced by three different graders and are not strictly comparable with each other.
- Manual review of the grader's disagreements and failures (`pilot/review.csv` for non-acute items; acute items are reviewed privately) has not been applied: every response pass rate in this report is the model grader's verdict alone. My manual verdict will override the grader once I have reviewed those rows.
- Models were reached through OpenRouter; the exact model versions are in `pilot/config.example.json`. Results describe those versions on the run date only.
- Messages are synthetic. They are not real user data and do not claim to represent all Nigerian users or all of Pidgin, Yoruba, Igbo or Hausa. Igbo and Hausa are not covered in this pilot.
- The 8 acute-level messages and the replies to them are withheld from this repository, so the acute-level numbers cannot be re-derived from the public files; only the per-model counts are published.

## Safety note
All messages are synthetic. There is no real user data. No message, reply or report contains methods or means: risk is expressed only through intent and feeling, and saved model outputs are filtered for method or means content before they are written. The 8 acute-level messages, and all model replies to them, are **withheld** from this repository and available to researchers on request (contact: zuesghost1@gmail.com). In `pilot/messages.csv` they appear as placeholder rows with the same ids and counts.

## How to reproduce
Python 3 and the standard library only; tested on Python 3.13 (older versions are untested). No packages to install.

```bash
git clone <this repo> && cd naija-distress-pilot
cp pilot/config.example.json pilot/config.json     # model names and price snapshot; no keys
export OPENROUTER_API_KEY=...                       # your own key; never commit it

python3 pilot/run.py --estimate                     # prints the cost estimate, makes no API calls
python3 pilot/run.py --run --max-usd 8              # response + detection calls (stops at the cap)
python3 pilot/grade.py                              # grades replies with a different model; writes review.csv
python3 pilot/report.py                             # writes REPORT.md and pilot/results_summary.csv
```

**Try it without an API key or any cost:** add `--dry-run` to any of the commands above. It makes no API calls, uses fake outputs, and writes to a temporary folder. It checks that the pipeline runs, not the models.

Because the acute items are withheld, a run from this public copy covers the **32 non-acute messages**, so its numbers will differ from the table above, which includes the 8 acute messages.

Files: `pilot/messages.csv` (data), `pilot/run.py` and `pilot/grade.py` (harness), `pilot/report.py` (report and summary), `pilot/results.jsonl` and `pilot/grades.jsonl` (raw non-acute outputs), `pilot/review.csv` (rows to check by hand), `pilot/results_summary.csv` (per-model metrics). `pilot/split.py` and `pilot/check_public.py` are maintainer tools that keep acute data out of the repository.

## Who
Charles David, Lagos, Nigeria. [LinkedIn: www.linkedin.com/in/charles-david-1281aa210] [GitHub: charlesDavid009]

## Licences
Code: MIT ([LICENSE](LICENSE)). Data: CC BY 4.0 ([DATA_LICENSE.md](DATA_LICENSE.md)). See the [dataset card](DATASET_CARD.md).
