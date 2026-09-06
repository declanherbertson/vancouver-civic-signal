# Vancouver Votes

A local, reproducible pipeline and interactive report-card interface for City of Vancouver council voting records.

The current build uses the supplied `council-voting-records.csv` as its voting source. The raw file is preserved unchanged and the requested trailing five-year window is normalized into analysis-friendly files. A separate, resumable enrichment job can attach exact motion text from official meeting minutes before the Codex classification job runs.

## What is included

- `data/raw/council-voting-records.csv` — unchanged supplied source (38,717 rows; 2021-01-19 to 2026-04-21).
- `data/processed/motions.*` — one row per unique motion.
- `data/processed/votes.*` — one row per member vote, joined by `motion_id` and `member_id`.
- `data/processed/members.*` — one row per Council member with coverage and vote totals.
- `data/processed/motions_to_classify.jsonl` — compact Codex input queue.
- `data/enrichment/motions.json` — vote-specific minutes excerpts and provenance.
- `data/derived/motion-stages.json` — conservative deterministic direct-amendment labels and evidence.
- `data/enrichment/missing_meetings.csv` — meetings that still need a source document.
- `data/processed/metadata.json` — provenance, source SHA-256, applied window, and counts.
- `data/classifications/motions.json` — complete, resumable structured classification output.
- `config/member-parties.json` — sourced party and affiliation-history metadata for every member.
- `config/featured-motions.json` — a researched pool of 40 alignment questions with context and tradeoffs.
- `web/` — static, responsive report cards, motion explorer, guided alignment ballot, and personal ballot comparison.

The current five-year build contains **33,162 member vote records**, **3,058 motions**, and **18 Council members**, with actual selected records from 2021-09-21 through 2026-04-21. The requested window is 2021-09-05 through 2026-09-05; the source file contains no records after 2026-04-21. Classification is complete for all 3,058 motions. Official minutes were matched to 2,600 motions; the remaining 458 are retained and classified conservatively from the City agenda description.

## Methodology at a glance

### Collection and provenance

The voting source is the complete CSV downloaded from the City of Vancouver Open Data portal, not a live API request. The original file is kept unchanged at `data/raw/council-voting-records.csv`. Its headers, row count, date coverage, selection window, and SHA-256 checksum are recorded in `data/processed/metadata.json`, making it possible to identify exactly which source snapshot produced a build.

The default preparation step applies an inclusive trailing five-calendar-year window and retains all meeting types present in that window unless `--meeting-type` is supplied. The current reproducible build uses an as-of date of 2026-09-05.

### Normalization

The preparation script validates required columns and unique City vote-detail IDs before transforming the CSV. A motion is the unique combination of City `Meeting ID` and `Vote Number`, exposed as `motion_id` in the form `MEETING_ID-VOTE_NUMBER`. It verifies that date, meeting type, agenda description, decision, and start time agree across every councillor row grouped into that motion.

The resulting files separate:

- motions: one row per meeting/vote;
- votes: one row per councillor/motion; and
- members: one row per councillor, with observed date coverage and status totals.

Published vote statuses—including In Favour, In Opposition, Abstain, Absent, Conflict, Ineligible, No Vote, and any other source value—are preserved rather than imputed or collapsed.

### Minutes enrichment

The City says its meeting minutes are the authoritative record. The enrichment job therefore searches official minutes and first tries to connect the open-data vote number to the corresponding `Vote No.` marker. A matched record stores the motion excerpt, mover, seconder, recorded outcome, official source URL, PDF page, matching method, and extraction warnings. Exact matches and the small number of same-date title-inferred matches are labelled separately. Prior-date lookback files must contain the exact vote number; title-only inference is restricted to same-date minutes.

The current snapshot has exact or otherwise validated minutes matches for 2,600 of 3,058 motions. The 458 unmatched motions remain visible, and their missing source meetings are listed in `data/enrichment/missing_meetings.csv`; they are not silently dropped or presented as minutes-backed.

### Classification and summaries

Classification runs as a separate, resumable Codex batch job. For matched motions, the exact minutes excerpt is the primary evidence and the agenda description is supporting context. For unmatched motions, only the City agenda description is used, confidence is kept conservative, and ambiguous items are flagged for review. The classifier does not browse or add outside facts.

Every classification must pass a JSON Schema and includes a primary category, optional secondary categories, policy direction, public-impact level, up to five keywords, confidence, `needs_review`, rationale, and a neutral plain-language summary of no more than 30 words. The taxonomy, schema, and complete prompt are versioned in `config/taxonomy.json`, `config/classification-batch.schema.json`, and `prompts/classify-motions.md`.

The 40 Find your match questions are a separate manually reviewed editorial layer. They are selected from high-impact, divided, minutes-matched votes and exclude motions positively identified as direct amendments. Their prompts explain what the recorded vote would do; their context and tradeoff text summarize material scope and good-faith arguments on each side. Where extra context was needed, a City staff report or agenda source is linked. These are paraphrases, not quotations or claims about a councillor's motive.

### Aggregation and presentation

Report cards aggregate the City's recorded statuses; they do not assign grades or infer ideology. Conservative deterministic rules identify direct debate amendments for exclusion from headline totals, while keeping those records available for inspection. Unanimous decisions are hidden by default but can be restored with the decision-scope control. The exact denominators for participation, vote direction, abstentions, and user alignment are documented below in **How report-card numbers work**.

## Quick start

Clone the repository and run the already-built site:

```bash
git clone https://github.com/declanherbertson/votevancouver.git
cd votevancouver
python3 scripts/serve.py
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

No npm install or third-party Python packages are required to serve the existing build. `make serve` is an equivalent shortcut. Stop the server with Ctrl-C.

## GitHub Pages deployment

The public site is deployed from the contents of `web/` by `.github/workflows/pages.yml`. A push to `main` automatically publishes a new build to [https://declanherbertson.github.io/votevancouver/](https://declanherbertson.github.io/votevancouver/). The workflow can also be run manually from the repository's **Actions** tab.

GitHub Pages serves only the already-generated static interface and JSON. Data preparation, minutes enrichment, and Codex classification run locally; commit and push the refreshed `web/data/` files to publish their results. Browser ballots remain private to that browser's `localStorage` and are not sent to GitHub or another application server.

## Rebuild the pipeline

The checked-in `web/data` files are ready to view. To refresh every stage from the current raw CSV, run these commands in order:

```bash
cd votevancouver

# Reproduce the current five-year selection and normalized files
python3 scripts/prepare_data.py --as-of 2026-09-05

# One-time environment setup for PDF/minutes enrichment
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# Resume minutes enrichment, then resume Codex classification
.venv/bin/python scripts/enrich_motions.py --execute
python3 scripts/classify_with_codex.py --execute

# Rebuild browser-ready joins, run checks, and serve
python3 scripts/build_site_data.py
python3 -m unittest discover -s tests -v
node --check web/app.js
python3 scripts/serve.py
```

The enrichment and classification jobs save progress and skip completed records, so rerunning them resumes rather than starting over. Classification requires the `codex` CLI to be installed and signed in. The detailed options, pilot commands, retry rules, and review guidance are in the following sections.

## Refresh the prepared data

The default is a trailing five-calendar-year window ending today:

```bash
python3 scripts/prepare_data.py
```

Useful alternatives:

```bash
# Reproduce the current build exactly
python3 scripts/prepare_data.py --as-of 2026-09-05

# Include the complete supplied file from January 2021
python3 scripts/prepare_data.py --all-dates

# Include only Council meeting records
python3 scripts/prepare_data.py --meeting-type Council

# Include several selected meeting types
python3 scripts/prepare_data.py \
  --meeting-type Council \
  --meeting-type "Special Council"
```

Preparing data also refreshes the compact files under `web/data/`.

## Enrich motions from official minutes

Do this before classifying the full dataset. The enrichment job matches `vote_number` from the open-data record to `Vote No.` printed in the City meeting minutes. This supplies the exact amendment or motion wording, mover, seconder, outcome, source URL, and PDF page when available.

Create a local Python environment once:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Preview the work without downloading anything:

```bash
.venv/bin/python scripts/enrich_motions.py
```

Run a one-meeting pilot:

```bash
.venv/bin/python scripts/enrich_motions.py --limit-meetings 1 --execute
```

Then continue through every remaining meeting:

```bash
caffeinate -i .venv/bin/python scripts/enrich_motions.py --execute
```

The job writes after every meeting, so it is safe to stop with Ctrl-C and run the same command again. Cached PDFs and extracted page text are reused. Existing matched records are skipped.

The City document host sometimes rejects automated downloads. The script automatically tries an Internet Archive copy while retaining the official City URL as the citation. Anything that cannot be matched is listed in `data/enrichment/missing_meetings.csv`.

The date lookback supports meetings that reconvened after their original start date. A prior-date minutes file is accepted only when it contains the exact exported vote number; title-based inference is limited to same-date minutes to prevent similarly named agenda items from being attached to the wrong vote.

For a missing meeting, download its minutes PDF in a browser, place it in a folder, and retry with:

```bash
.venv/bin/python scripts/enrich_motions.py \
  --source-dir /path/to/downloaded-minutes \
  --retry-unmatched \
  --execute
```

The downloaded filename may be the official filename from `expected_minutes_url` in the missing-meetings CSV (for example, `regu20260414min.pdf`) or the meeting ID with a `.pdf` extension. Use `--offline` as well when every needed PDF is already local. Use `--meeting-id ID` to retry one meeting and `--force` only to replace successful enrichment records.

### Recover the highest-impact missing minutes with Chrome

The City document host may allow a normal browser while rejecting ordinary HTTP clients. The recovery job ranks `missing_meetings.csv` by unmatched motion count, opens each official PDF in a dedicated Chrome session, and validates the PDF structure, meeting date/type, extractable text, and expected vote markers before saving it. It can then run the existing enrichment job only for meetings with validated files.

Preview the top 20 meetings:

```bash
.venv/bin/python scripts/recover_missing_minutes.py --limit 20
```

Download, validate, and reprocess them:

```bash
caffeinate -i .venv/bin/python scripts/recover_missing_minutes.py \
  --limit 20 \
  --execute \
  --run-enrichment
```

The job is resumable: valid files are reused from `data/enrichment/browser_minutes`, and validation details are written to `data/enrichment/browser_minutes/validation_results.json`. Chrome is visible because headless requests are usually blocked. If Cloudflare requires a one-time interaction, pass a dedicated `--profile-dir` inside the project so its session can be reused; never point this option at a personal Chrome profile.

Important: running `prepare_data.py` rebuilds the title-only classification queue. Run `enrich_motions.py` again afterward; cached minutes make that rebuild quick.

## Classify enriched motions with Codex

The classification job is deliberately separate. It uses `codex exec` in ephemeral, read-only mode with a strict JSON Schema, validates every returned motion ID and label, saves each successful batch, and can resume after interruption. It ignores user-level Codex configuration and project rules for this isolated data-only call, which prevents unrelated MCP servers from starting; saved CLI authentication remains available.

After enrichment, start with a dry run:

```bash
python3 scripts/classify_with_codex.py --limit 25
```

Then run and review one pilot batch:

```bash
python3 scripts/classify_with_codex.py --limit 25 --execute
```

If the labels look useful, continue the remaining queue:

```bash
python3 scripts/classify_with_codex.py --execute
```

If a title-only pilot was classified before enrichment, refresh just those existing records first:

```bash
python3 scripts/classify_with_codex.py --reclassify-existing --execute
```

At the default batch size, the full 3,058-motion queue takes 123 Codex calls. Existing results are skipped. Use `--force` only when intentionally replacing all selected classifications; `--motion-id ID` can be repeated to target specific motions. The default Codex model comes from the local CLI configuration, or you can explicitly supply `--model MODEL`.

The taxonomy lives in `config/taxonomy.json`, the structured-output contract in `config/classification-batch.schema.json`, and the prompt in `prompts/classify-motions.md`. After every run, `web/data` is refreshed so the interface immediately picks up completed labels.

Official OpenAI documentation describes [`codex exec` non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), including schema-constrained output and resumable scripted runs.

## Human review is important

Some minutes may be unavailable, poorly extracted, or only partially captured. A motion can also remain ambiguous—especially for amendments, referrals, separated votes, and procedural votes. The classifier therefore:

- uses neutral topic and policy-action labels;
- records a confidence score;
- marks ambiguous records with `needs_review`;
- prefers the matched minutes excerpt over the agenda title;
- is instructed not to invent missing motion contents; and
- treats agenda titles as untrusted data, never as instructions.

Before publishing this as a voter resource, review low-confidence and `needs_review` classifications against the corresponding meeting minutes. The [City of Vancouver dataset](https://opendata.vancouver.ca/explore/dataset/council-voting-records/) says those minutes are the official record.

## Derive direct-amendment labels

Site builds run the deterministic stage rules automatically. To inspect or refresh the standalone derived file directly:

```bash
python3 scripts/derive_motion_stages.py
```

A vote is labelled as a direct amendment only when its agenda stage explicitly ends in wording such as `– Amendment`, or an exact vote-number minutes excerpt begins with an `AMENDMENT … MOVED by` block. Policy items that amend legislation—such as a `CD-1 Text Amendment`—are not treated as debate amendments merely because their subject contains the word “amendment.” Direct amendments remain in the motion explorer but are omitted from headline report-card totals.

## How report-card numbers work

- **Default scope** shows divided decisions: motions with at least one recorded In Favour vote and at least one recorded In Opposition vote. Choose **All decisions** to include one-sided and unanimous decisions. This decision filter is separate from the direct-amendment filter.
- **Headline votes** includes every published status for that member except records positively identified as direct amendments. The profile discloses both excluded and complete published counts.
- **Participation** is `(In Favour + In Opposition + Abstain) / all recorded statuses` for the selected scope.
- **Favour/opposition percentages** use only explicit In Favour and In Opposition positions as their denominator. The accompanying bar still shows other statuses separately.
- **Your match** compares only motions the visitor marks in “My ballot.” An In Favour record matches “I’d vote in favour,” and an In Opposition record matches “I’d vote in opposition.” A comparable position means one of those two explicit positions exists on the same motion.
- **Abstentions** count as participation in report-card participation rates, but as neutral in alignment: they are disclosed and excluded from the match denominator. Absences, conflicts, ineligible, no-vote and missing records are also disclosed separately from agreement or disagreement.
- **Find your match** starts with a fixed, manually selected set of 20 clear, consequential and issue-diverse questions, then offers the remaining 20 researched motions as an extension. Pool records have exact minutes matches, at least two votes on each side, no direct amendments, contextual scope and separate good-faith cases for and against. A comparison becomes available after 10 answers but remains hidden until the visitor reveals it or reaches the end of the starter set. Results default to the 11 currently sitting Council members, with an option to include former members.
- **My ballot** also defaults to currently sitting members and shares the same include-former-members setting with Find your match.
- Personal ballot choices are stored only in the visitor’s browser using `localStorage`.

Party labels are sourced separately from City election records and direct affiliation-change reporting. Because affiliation can change during a term, the interface shows the current or last recorded affiliation plus a short history and source on each profile. Party is descriptive metadata and is never inferred from voting behaviour.

These are descriptive measures, not grades. The interface does not infer ideology, intent, or whether a vote was “good.”

## Test

```bash
python3 -m unittest discover -s tests -v
node --check web/app.js
```

The tests cover date-window filtering, motion grouping, conservative amendment detection, classification validation, taxonomy/schema consistency, joins, report-card exclusions, party completeness, curated-pool criteria, and raw-source provenance.

## Source and limitations

The data comes from the City of Vancouver’s [Council voting records](https://opendata.vancouver.ca/explore/dataset/council-voting-records/) dataset and is subject to the [Open Government Licence – Vancouver](https://opendata.vancouver.ca/pages/licence/). The City notes that data can lag publication of meeting minutes and that the minutes are authoritative.

Minutes-backed text is available for 2,600 motions; 458 still rely on the shorter agenda description because no acceptable minutes match was found. Those records are the highest-priority enrichment gap. Even with exact minutes, amendments, referrals, separated votes, procedural motions, and extracted PDF boundaries can remain ambiguous, so confidence and `needs_review` should be used as review aids rather than guarantees of correctness.

The summaries and curated arguments are explanatory editorial material, not the official wording of a motion. Public-facing use should retain the source links, disclose the methodology, and periodically re-run human review as City records or the taxonomy change.
