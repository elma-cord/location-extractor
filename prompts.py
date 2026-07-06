def build_prompt(source_text: str) -> str:
    """Prompt for extracting ONLY job_location, remote_preference, remote_days
    from a single job posting's live page text."""
    return f"""You are extracting structured data from ONE job posting's web page text.

Return ONLY a JSON object with exactly these three keys and nothing else:

1) "job_location"
   - The single real work location for THIS role, anywhere in the world, as plain text. Use the most specific form the posting supports: "City, Country" or "City, State/Region, Country" (e.g. "Manchester, UK", "Chicago, IL, USA", "Berlin, Germany", "Toronto, Canada").
   - If the role is anywhere in London (central London, a London borough, or a London suburb such as Shoreditch / Croydon / Wimbledon), output exactly "London, UK".
   - If only a country or region is given (or several cities within one country), output that country or region (e.g. "United States", "Germany", "United Kingdom").
   - For remote roles that are tied to a country or region (e.g. "remote, must be based in the US"), output that country or region (e.g. "United States").
   - Output "Unknown" ONLY when the role is genuinely remote-anywhere with no identifiable country or region.
   - Use ONLY the location of THIS job. IGNORE unrelated job cards, "similar jobs" / "related jobs" / "more jobs" sections, page footers, and the company's head-office address when it differs from where the role is actually based.

2) "remote_preference"
   - Return EXACTLY ONE value: "onsite", "hybrid", or "remote" — or "" (empty) if the posting gives no indication. Never return more than one.
   - "remote"  = fully remote / home-based / work-from-anywhere, with no requirement to regularly attend an office.
   - "hybrid"  = a mix of home and office working (some days in the office and some from home, or the posting simply says "hybrid").
   - "onsite"  = based full-time at an office or work site, with no home working.
   - A SPLIT WEEK IS HYBRID: e.g. "2 days in the office, 3 days from home" -> "hybrid" (never "onsite", never "remote").
   - Choose the single best fit. If the posting gives NO indication of remote/hybrid/office working, return "" (empty). Do not guess "onsite".

3) "remote_days"
   - The number of days per week the person works REMOTELY / from home, as a string "0"-"5", OR "not specified".
   - Convert from office days when needed (assume a 5-day week): fully office-based -> "0"; 1 day in the office -> "4"; 2 days in the office -> "3"; 3 days in the office -> "2"; 4 days in the office -> "1".
   - If it states WFH/remote days directly, use that number (e.g. "1 day from home" -> "1").
   - Fully remote, remote-first, or unclear -> "not specified".

Job page text:
<<<
{source_text}
>>>

Return only the JSON object.
"""
