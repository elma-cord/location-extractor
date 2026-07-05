# Remote Location Extractor

Reads a list of job links and, for **each link**, opens the live job page and
extracts three fields:

- `job_location`
- `remote_preferences` (onsite / hybrid / remote — can be a combination)
- `remote_days` (number of days per week worked remotely, `0`–`5`, or `not specified`)

It reuses the tuned location-normalization and remote-parsing logic from
`job-data-extractor`, but reads **only from the job link** (never a description)
and outputs only these three fields.

## Setup (one time)

1. Create a new GitHub repo and upload every file in this folder, keeping the
   folder structure (including `.github/workflows/extractor.yml`).
2. Copy **`predefined_locations.csv`** from your `job-data-extractor` repo into
   this repo (it's the allowed-locations list the matcher needs).
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret** → name `OPENAI_API_KEY`, value = your OpenAI key.
   (No Gemini key is needed — this project does not use Gemini.)

## Input file

Upload your list of links as **`jobs_input.csv`**. It needs a column with the
job URL named one of: `job_url`, `url`, `link`, or `job_link`. Any other columns
you include are carried through to the output unchanged.

## Run it

Repo → **Actions → Remote location extractor → Run workflow**.

When it finishes, download the **`results`** artifact — that's `jobs_output.csv`,
your original columns plus `job_location`, `remote_preferences`, `remote_days`,
and `extraction_notes` (which says `ok`, or why a row was skipped, e.g.
`url_fetch_failed`).

## Notes

- **Model:** `gpt-4.1` (set in `config.py`). Change `MAIN_MODEL` to
  `gpt-4.1-mini` to cut cost ~5x if the accuracy is good enough for you.
- **Cost:** roughly £70–110 for ~14k jobs on `gpt-4.1` (far less on mini).
- **`extraction_notes = url_fetch_failed`** means the page was dead, blocked, or
  JavaScript-rendered (no readable content). No model can fix that — it's a
  property of the link, not the extractor.
- The run splits into batches of 2,000 (see `BATCH_SIZE` in the workflow) so
  progress is saved per batch; `WORKERS` (default 12) controls parallelism.
