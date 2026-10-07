# Job-Search Copilot — "is this posting outside the US?"
#
# Used when searching nationwide (NATIONWIDE = True in discover_jobs.py) so that
# clearly overseas postings are dropped BEFORE AI scoring, which saves API credit.
#
# Checks run in this order (deliberately conservative: when unsure, KEEP the job):
#   1. Multi-location postings ("3 Locations (incl. ...)") -> keep. A hidden
#      location might be in the US, so the AI scorer judges those.
#   2. Strong US signal ("United States", "USA", "US-Remote")      -> keep.
#   3. Names a foreign COUNTRY ("India", "Canada", "Ireland")       -> drop.
#   4. Ends in a US state code (", TX", ", NY")                     -> keep.
#   5. Names a well-known foreign CITY ("Bengaluru", "Warsaw")      -> drop.
#   6. Anything else                                                -> keep.
# Why countries come before state codes: "Canada - Remote (ON, AB, BC, or NS)"
# contains ", OR" (Oregon!), so the country has to win.
# City names that also exist in the US (Paris, Athens, Kingston, Lima, Milan...)
# are left out on purpose, so "Paris, TX" is safe.
# ---------------------------------------------------------------------------

import re

US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
             "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()

_MULTI = re.compile(r"\d+\s+locations", re.I)
_STRONG_US = re.compile(r"united states|\busa\b|\bus\b|\bus-|puerto rico", re.I)
# Case-SENSITIVE on purpose: "Dublin, Co. Dublin" must not count as Colorado.
_STATE_CODE = re.compile(r",\s*(?:" + "|".join(US_STATES) + r")\b(?![.\-])")

_COUNTRIES = re.compile("|".join([
    "india", "canada", "canadian", r"\bcan,", "philippines", "poland", "hungary", "romania",
    "czech", "croatia", "ireland", "united kingdom", "england", "scotland", "great britain",
    "germany", "france", "italy", "spain", "portugal", "sweden", "switzerland", "netherlands",
    "belgium", "luxembourg", "ukraine", "slovakia", "australia", "new zealand", r"\bnzl\b",
    "china", "hong kong", "taiwan", "japan", "singapore", "malaysia", "indonesia", "thailand",
    "united arab emirates", r"\buae\b", "bahrain", "egypt", "south africa", "brazil",
    "argentina", "colombia", "peru", "uruguay", "costa rica", r"(?<!new )mexico",
]), re.I)

# Big foreign cities and ATS location codes actually seen in the feed.
_CITIES = re.compile("|".join([
    "bengaluru", "bangalore", "bangaluru", "gurugram", "gurgaon", "mumbai", "pune",
    "hyderabad", "chennai", "dlf cybercity", "udyog vihar", "shivaji", r"\bind07\b",
    "manila", "taguig", "mandaluyong", r"\bph-", "warsaw", "warszawa", "olsztyn", "lublin",
    "budapest", "prague", "bratislava", "bucharest", "cluj", "lviv", "zagreb", "brussels",
    "lisboa", "lisbon", "madrid", "boadilla", "frankfurt", "munich", "neuilly", "geneva",
    "geneve", "stockholm", "goteborg", "firenze", "amsterdam", r"(?<!new )london", "belfast",
    "glasgow", "bournemouth", "watford", "milton keynes", "redhill", "dublin", r"\bcork\b",
    "toronto", "mississauga", "montreal", "calgary", "vaughan", "halifax", "longueuil",
    r"\bca-(?:on|qc|ns|bc|ab)-", "tokyo", "minato", "chiyoda", "beijing", "shanghai",
    "kowloon", "taipei", "kuala lumpur", "selangor", "petaling jaya", "jakarta", "bangkok",
    "dubai", "johannesburg", "midrand", "sydney", "auckland", "sao paulo", "são paulo",
    "buenos aires", "bogota", "montevideo", "zonamerica", "cariari", "tijuana", "juarez",
    "aguascalientes", r"\bcdmx\b",
]), re.I)


def is_non_us(location):
    """True only when the location is clearly outside the United States."""
    loc = (location or "").strip()
    if not loc or _MULTI.search(loc) or _STRONG_US.search(loc):
        return False
    if _COUNTRIES.search(loc):
        return True
    if _STATE_CODE.search(loc):
        return False
    return bool(_CITIES.search(loc))
