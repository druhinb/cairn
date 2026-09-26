"""One spelling per city, so one feed's "NYC" and another's "New York, NY" match,
and the places outside the US."""
import re
import unicodedata

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
COUNTRY = {"us", "usa", "united states", "united states of america"}

# Looked up only as a whole place: SimplifyJobs writes Los Angeles as a bare "LA",
# while "Baton Rouge, LA" is in Louisiana.
_CITIES = {
    "New York, NY": ("nyc", "new york", "new york city", "new york city, ny",
                     "new york, new york", "manhattan, nyc", "manhattan, ny"),
    "San Francisco, CA": ("sf", "san francisco"),
    "South San Francisco, CA": ("south sf", "south san francisco"),
    "Los Angeles, CA": ("la", "los angeles"),
    "Washington, DC": ("dc", "washington dc", "washington d.c.", "washington, d.c.",
                       "washington d.c., dc"),
    "Seattle, WA": ("seattle",),
    "Chicago, IL": ("chicago",),
    "Boston, MA": ("boston",),
    "Austin, TX": ("austin",),
}
ALIASES = {alias: city for city, aliases in _CITIES.items() for alias in aliases}

_STATE_CODES = set(STATES.values())
_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "ON", "PE", "QC", "SK"}
_US_WORDS = re.compile(r"\b(?:usa|u\.s\.?|united states)\b")
# countries, regions and cities outside the US as feeds write them, accents dropped;
# Georgia, Jersey and Cambridge stay out, as places in the US share those names
_ABROAD = {
    "apac", "argentina", "australia", "austria", "belgium", "brazil", "bulgaria",
    "canada", "chile", "china", "colombia", "costa rica", "croatia", "czech republic",
    "czechia", "denmark", "egypt", "emea", "england", "estonia", "europe", "finland",
    "france", "germany", "greece", "hong kong", "hungary", "india", "indonesia", "ireland",
    "israel", "italy", "japan", "kenya", "korea", "latam", "latvia", "lithuania",
    "luxembourg", "malaysia", "mexico", "morocco", "netherlands", "new zealand", "nigeria",
    "norway", "pakistan", "peru", "philippines", "poland", "portugal", "qatar", "romania",
    "saudi arabia", "scotland", "serbia", "singapore", "slovakia", "south africa", "spain",
    "sweden", "switzerland", "taiwan", "thailand", "turkey", "uae", "uk", "ukraine",
    "united arab emirates", "united kingdom", "uruguay", "vietnam", "wales",
    "amsterdam", "bangalore", "barcelona", "beijing", "belgrade", "bengaluru", "berlin",
    "bristol", "cdmx", "chennai", "copenhagen", "doha", "dubai", "dublin", "edinburgh",
    "gurgaon", "gurugram", "hyderabad", "london", "madrid", "manchester", "melbourne",
    "milan", "montreal", "mumbai", "munich", "noida", "ottawa", "paris", "pune", "riyadh",
    "sao paulo", "seoul", "shanghai", "stockholm", "sydney", "tel aviv", "tokyo", "toronto",
    "vancouver", "warsaw", "zurich",
}
_LONGEST_NAME = max(len(name.split()) for name in _ABROAD)


def canonical(place):
    """place with a known city's other spellings, a spelled-out US state, and a
    trailing US country all written one way; any other place as given, less
    doubled spaces."""
    if not isinstance(place, str):
        return place
    place = " ".join(place.split())
    parts = [part.strip() for part in place.split(",")]
    if len(parts) == 3 and parts[2].casefold() in COUNTRY:
        parts = parts[:2]
    if len(parts) == 2 and parts[1].casefold() in STATES:
        parts[1] = STATES[parts[1].casefold()]
    place = ", ".join(parts)
    return ALIASES.get(place.casefold(), place)


def term(text):
    """A location preference or search as the lowercase text it finds in a posting's
    canonical places."""
    return canonical(text).lower()


def _folded(place):
    decomposed = unicodedata.normalize("NFKD", place.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _names_us(place):
    parts = [part.strip() for part in place.split(",")]
    return (any(part in _STATE_CODES or part.casefold() in STATES for part in parts[1:])
            or bool(_US_WORDS.search(place.casefold())))


def _names_abroad(place):
    parts = [part.strip() for part in place.split(",")]
    if any(part in _PROVINCES for part in parts[1:]):
        return True
    words = re.findall(r"[a-z]+", _folded(place))
    return any(" ".join(words[i:i + n]) in _ABROAD
               for n in range(1, _LONGEST_NAME + 1) for i in range(len(words) - n + 1))


def abroad(locations):
    """True when a posting lists places and each names somewhere outside the US.

    A place naming a US state or the US counts as in it, so "Dublin, CA" stays; one
    naming nowhere Cairn knows, such as "Remote" or "Seattle Office", keeps the
    posting too.
    """
    return bool(locations) and all(
        not _names_us(canonical(place)) and _names_abroad(canonical(place))
        for place in locations)
