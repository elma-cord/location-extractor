from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

INPUT_CSV = BASE_DIR / "jobs_input.csv"
OUTPUT_CSV = BASE_DIR / "jobs_output.csv"
PREDEFINED_LOCATIONS_CSV = BASE_DIR / "predefined_locations.csv"

# Extraction model. gpt-4.1 is chosen for accuracy on messy live job pages
# (nav/boilerplate/org-HQ noise). Switch to "gpt-4.1-mini" to cut cost ~5x if
# the accuracy is good enough for your needs.
MAIN_MODEL = "gpt-4.1"

OPENAI_TIMEOUT_SECONDS = 60
MAX_RETRIES = 2
MAX_OUTPUT_TOKENS = 400

# Tight fetch timeout: with 14k live fetches, a long timeout on hanging pages
# blows the GitHub Actions time budget. 10s drops dead/slow pages quickly.
FETCH_TIMEOUT_SECONDS = 10

LOCATION_UNKNOWN = "Unknown"
REMOTE_NOT_SPECIFIED = "not specified"
ALLOWED_REMOTE_PREFERENCES = ["onsite", "hybrid", "remote"]
