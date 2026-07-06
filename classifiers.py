import re
import time
from typing import Any

from config import (
    MAIN_MODEL,
    MAX_RETRIES,
    MAX_OUTPUT_TOKENS,
    FETCH_TIMEOUT_SECONDS,
    LOCATION_UNKNOWN,
    REMOTE_NOT_SPECIFIED,
)
from fetch_extract import fetch_job_page_text
from prompts import build_prompt
from rules import (
    get_primary_text_window,
    extract_remote_preferences,
    extract_remote_days,
)
from validators import (
    clean_description,
    extract_json_object,
    normalize_remote_preferences,
    normalize_remote_days,
    safe_str,
)


class RemoteLocationExtractor:
    def __init__(self, client, predefined_locations: list[str]):
        self.client = client
        self.predefined_locations = predefined_locations

    # --- model call (temperature=0, JSON mode, retry with backoff) ---
    def _call_model(self, prompt: str) -> str:
        last_error = None
        use_json_format = True
        use_temperature = True
        retry_delays = [2, 5, 10]

        for attempt in range(MAX_RETRIES + 1):
            try:
                kwargs = {
                    "model": MAIN_MODEL,
                    "input": prompt,
                    "max_output_tokens": MAX_OUTPUT_TOKENS,
                }
                if use_temperature:
                    kwargs["temperature"] = 0
                if use_json_format:
                    kwargs["text"] = {"format": {"type": "json_object"}}

                response = self.client.responses.create(**kwargs)
                text = (response.output_text or "").strip()

                if text and extract_json_object(text):
                    return text
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                if use_json_format and ("format" in message or "text" in message):
                    use_json_format = False
                elif use_temperature and "temperature" in message:
                    use_temperature = False

            if attempt < MAX_RETRIES:
                time.sleep(retry_delays[min(attempt, len(retry_delays) - 1)])

        raise RuntimeError(f"model_call_failed: {last_error}")

    # --- location selection (country-agnostic: keep the real worldwide location) ---
    def _choose_best_location(self, ai_location: Any, page_text: str) -> str:
        ai_location_s = safe_str(ai_location).strip()
        if ai_location_s and ai_location_s.lower() != "unknown":
            # Trust the location the model read from the page, worldwide. Only
            # tidy London variants to "London, UK". Never discard a location for
            # being outside the UK - this project is global.
            return self._collapse_london(ai_location_s)

        # Model gave nothing usable -> recover a labelled location from the page.
        recovered = self._recover_location_from_text(page_text)
        if recovered:
            return self._collapse_london(recovered)

        return LOCATION_UNKNOWN

    def _recover_location_from_text(self, *texts: str) -> str:
        labelled_patterns = [
            r"(?im)^\s*(?:job\s+)?location(?:\s+city)?\s*[:\-]\s*(.+)$",
            r"(?im)^\s*work location\s*[:\-]\s*(.+)$",
            r"(?im)^\s*office location\s*[:\-]\s*(.+)$",
            r"(?im)^\s*based in\s+(.+)$",
        ]

        for text in texts:
            text = clean_description(get_primary_text_window(text or ""))
            if not text:
                continue

            for pattern in labelled_patterns:
                for match in re.finditer(pattern, text):
                    value = (match.group(1) or "").strip()
                    value = re.split(
                        r"(?i)\b(hybrid|remote|onsite|on-site|salary|per annum|schedule|category|posted|employment type)\b",
                        value,
                        maxsplit=1,
                    )[0].strip(" ,:-")
                    if value and not self._is_broad_location(value):
                        return value

        return ""

    @staticmethod
    def _is_broad_location(value: str) -> bool:
        value_l = (value or "").strip().lower()
        return value_l in {
            "",
            "unknown",
            "global",
            "worldwide",
            "anywhere",
            "remote",
        }

    def _collapse_london(self, value: str) -> str:
        if not value:
            return value
        low = value.strip().lower()

        if low in {
            "london", "london, uk", "london, england", "london, england, uk",
            "greater london", "greater london, uk",
            "london metropolitan area, uk",
            "city of london", "city of london, uk",
        }:
            return "London, UK"

        if re.search(r",\s*london\b", low):
            return "London, UK"

        if low.startswith("london borough of"):
            return "London, UK"
        if re.match(r"(?:east|west|north|south|central)\s+london\b", low):
            return "London, UK"

        return value

    # --- main entry point: read ONLY from the live job URL ---
    def extract(self, job_url: str) -> dict:
        result = {
            "job_location": LOCATION_UNKNOWN,
            "remote_preferences": "",
            "remote_days": REMOTE_NOT_SPECIFIED,
            "notes": "",
        }

        if not job_url:
            result["notes"] = "missing_url"
            return result

        fetched = fetch_job_page_text(job_url, timeout=FETCH_TIMEOUT_SECONDS)
        if not (fetched.ok and fetched.text):
            reason = fetched.error or (fetched.status_code and f"status_{fetched.status_code}") or "unknown"
            if fetched.blocked:
                reason = f"blocked ({reason})"
            result["notes"] = f"url_fetch_failed: {reason}"
            return result

        page_text = clean_description(fetched.text)
        if not page_text:
            result["notes"] = "empty_page_text"
            return result

        payload = {}
        note = "ok"
        try:
            raw = self._call_model(build_prompt(page_text))
            payload = extract_json_object(raw) or {}
        except Exception as exc:
            note = f"model_failed: {exc}"

        # Location: trust the model's worldwide read, with a page-text fallback.
        job_location = self._choose_best_location(payload.get("job_location"), page_text)

        # Remote preferences: union the tuned regex extractor with the AI output.
        ai_remote = normalize_remote_preferences(payload.get("remote_preferences", []))
        det_remote = extract_remote_preferences(page_text)
        remote_set = set(det_remote) | set(ai_remote)
        remote_list = [x for x in ["onsite", "hybrid", "remote"] if x in remote_set]

        # Remote days: deterministic wins if it fires, else the AI value.
        det_days = extract_remote_days(page_text)
        ai_days = normalize_remote_days(payload.get("remote_days", ""))
        remote_days = det_days if det_days != "not specified" else ai_days

        result["job_location"] = job_location
        result["remote_preferences"] = ", ".join(remote_list)
        result["remote_days"] = remote_days
        result["notes"] = note
        return result
