"""One spelling per city, so one feed's "NYC" and another's "New York, NY" match."""

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
