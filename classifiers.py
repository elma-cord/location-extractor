import os
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
    extract_remote_days,
)
from validators import (
    clean_description,
    extract_json_object,
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
            return self._collapse_london(ai_location_s)

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

    # --- single remote preference ---
    @staticmethod
    def _normalize_single_remote(value: Any) -> str:
        allowed = ("onsite", "hybrid", "remote")
        if isinstance(value, list):
            items = [str(v).strip().lower() for v in value]
        elif isinstance(value, str):
            items = [x.strip().lower() for x in value.split(",")]
        else:
            items = []
        items = [x for x in items if x in allowed]
        if not items:
            return ""
        # Defensive: if the model returned more than one, a mix means hybrid.
        if len(items) > 1:
            return "hybrid"
        return items[0]

    @staticmethod
    def _single_remote(ai_pref: str, remote_days: str) -> str:
        # A stated split week (1-4 remote days) is hybrid by definition.
        if remote_days in ("1", "2", "3", "4"):
            return "hybrid"
        if ai_pref in ("onsite", "hybrid", "remote"):
            return ai_pref
        if remote_days == "5":
            return "remote"
        # No clear signal -> leave empty (do not assume onsite).
        return ""

    # --- run the model on one block of page text and compute the 3 fields ---
    def _extract_fields(self, page_text: str) -> dict:
        payload = {}
        note = "ok"
        try:
            raw = self._call_model(build_prompt(page_text))
            payload = extract_json_object(raw) or {}
        except Exception as exc:
            note = f"model_failed: {exc}"

        job_location = self._choose_best_location(payload.get("job_location"), page_text)

        det_days = extract_remote_days(page_text)
        ai_days = normalize_remote_days(payload.get("remote_days", ""))
        remote_days = det_days if det_days != "not specified" else ai_days

        ai_pref = self._normalize_single_remote(
            payload.get("remote_preference", payload.get("remote_preferences"))
        )
        remote_preference = self._single_remote(ai_pref, remote_days)

        return {
            "job_location": job_location,
            "remote_preferences": remote_preference,
            "remote_days": remote_days,
            "note": note,
        }

    # --- main entry point: static fetch first, headless render only on a miss ---
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

        render_enabled = os.getenv("ENABLE_RENDER", "1").strip() == "1"

        fetched = fetch_job_page_text(job_url, timeout=FETCH_TIMEOUT_SECONDS)
        static_text = clean_description(fetched.text) if (fetched.ok and fetched.text) else ""

        fields = None
        note = ""
        if static_text:
            fields = self._extract_fields(static_text)
            note = fields["note"]
        else:
            reason = fetched.error or (fetched.status_code and f"status_{fetched.status_code}") or "unknown"
            if fetched.blocked:
                reason = f"blocked ({reason})"
            note = f"static_failed: {reason}"

        need_render = render_enabled and (
            fields is None or fields["job_location"] == LOCATION_UNKNOWN
        )
        if need_render:
            rendered = fetch_job_page_text(job_url, timeout=FETCH_TIMEOUT_SECONDS, render=True)
            rendered_text = clean_description(rendered.text) if (rendered.ok and rendered.text) else ""
            if rendered_text:
                rendered_fields = self._extract_fields(rendered_text)
                if fields is None or rendered_fields["job_location"] != LOCATION_UNKNOWN:
                    fields = rendered_fields
                    note = f"{rendered_fields['note']} (rendered)"
            else:
                r_reason = rendered.error or (rendered.status_code and f"status_{rendered.status_code}") or "unknown"
                note = f"render_failed: {r_reason}" if fields is None else f"{note}; render_failed: {r_reason}"

        if fields is None:
            result["notes"] = note or "no_content"
            return result

        result["job_location"] = fields["job_location"]
        result["remote_preferences"] = fields["remote_preferences"]
        result["remote_days"] = fields["remote_days"]
        result["notes"] = note
        return result
