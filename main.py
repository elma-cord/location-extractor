import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI

from config import INPUT_CSV, OUTPUT_CSV, PREDEFINED_LOCATIONS_CSV, OPENAI_TIMEOUT_SECONDS
from classifiers import RemoteLocationExtractor

NEW_COLUMNS = ["job_location", "remote_preferences", "remote_days", "extraction_notes"]

# Column names we accept for the job link, in priority order.
URL_KEYS = ("job_url", "url", "link", "job_link", "joburl", "job link")


def read_input_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def load_predefined_locations(path):
    locations = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            value = (row[0] or "").strip()
            if value and value.lower() not in {"location", "locations", "name"}:
                locations.append(value)
    return locations


def _num_workers():
    raw = (os.getenv("WORKERS", "12") or "12").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 12
    return max(1, min(n, 24))


def _resolve_job_url(row):
    for key in URL_KEYS:
        if key in row and (row.get(key) or "").strip():
            return row[key].strip()
    # Fall back to any cell that looks like a URL (handles a single-column file).
    for value in row.values():
        v = str(value or "").strip()
        if v.startswith("http://") or v.startswith("https://"):
            return v
    return ""


def main():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Missing OPENAI_API_KEY", file=sys.stderr)
        sys.exit(1)

    rows, fieldnames = read_input_csv(INPUT_CSV)
    if not rows:
        print("jobs_input.csv is empty", file=sys.stderr)
        sys.exit(1)

    predefined_locations = load_predefined_locations(PREDEFINED_LOCATIONS_CSV)
    if not predefined_locations:
        print("predefined_locations.csv is empty or missing", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)
    extractor = RemoteLocationExtractor(client, predefined_locations)

    total = len(rows)
    output_rows = [None] * total

    def _process_row(idx, row):
        job_url = _resolve_job_url(row)
        result = extractor.extract(job_url)
        out = dict(row)
        out["job_location"] = result.get("job_location", "")
        out["remote_preferences"] = result.get("remote_preferences", "")
        out["remote_days"] = result.get("remote_days", "")
        out["extraction_notes"] = result.get("notes", "")
        return out

    workers = _num_workers()
    print(f"Processing {total} jobs with {workers} workers...", flush=True)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(_process_row, idx, row): idx
            for idx, row in enumerate(rows)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                output_rows[idx] = future.result()
            except Exception as exc:
                out = dict(rows[idx])
                out["job_location"] = ""
                out["remote_preferences"] = ""
                out["remote_days"] = ""
                out["extraction_notes"] = f"row_error: {exc}"
                output_rows[idx] = out
            done += 1
            if done % 100 == 0 or done == total:
                print(f"{done}/{total} done", flush=True)

    out_fieldnames = list(fieldnames)
    for col in NEW_COLUMNS:
        if col not in out_fieldnames:
            out_fieldnames.append(col)

    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"Wrote {total} rows to {OUTPUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
