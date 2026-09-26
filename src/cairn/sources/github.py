"""GitHub-hosted lists: listings.json feeds and README tables."""
import datetime
import html
import html.parser
import re
import time

from cairn.sources import web
from cairn.sources.common import _plain, _posting, _url_key

# a listings.json feed carries every posting as one JSON array; SimplifyJobs' alone runs ~17k rows
LISTINGS_MAX_BYTES = 64 * 1024 * 1024


def github_listings(url):
    rows = web.get_json(url, limit=LISTINGS_MAX_BYTES)
    # read as an empty feed, an error body such as {} would delist the whole source
    if not isinstance(rows, list):
        raise ValueError(f"expected a list of postings, got {type(rows).__name__}")
    # vanshb03's internship feed names a single season where Simplify lists terms
    return [row if "terms" in row
            else {**row, "terms": [row["season"]] if row.get("season") else []}
            for row in rows]


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_HTML_HREF = re.compile(r"""<a\s[^>]*?href=["']([^"']+)["']""", re.IGNORECASE)
_TABLE_RULE = re.compile(r"\|?(\s*:?-+:?\s*\|)*\s*:?-+:?\s*\|?")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
# a column's header text: the posting field it holds
_README_COLUMNS = {
    "company": "company", "role": "title", "job title": "title", "position": "title",
    "title": "title", "location": "location", "locations": "location", "date": "date",
    "date posted": "date", "posted": "date", "age": "date", "apply": "apply",
    "application": "apply", "application/link": "apply", "link": "apply", "posting": "apply",
}
# the first match wins; a heading that matches none sets no category. speedyapply
# files its software roles under "FAANG+" and "Other" beside "Quant".
_HEADING_CATEGORIES = (
    (re.compile(r"\bquant", re.IGNORECASE), "Quant"),
    (re.compile(r"\b(data|ai|ml|machine learning)\b", re.IGNORECASE), "AI/ML/Data"),
    (re.compile(r"\b(software|swe)\b|^faang\+?$|^other$", re.IGNORECASE), "Software"),
)
_AGE = re.compile(r"(\d+)\s*(mo|months?|w|wks?|weeks?|d|days?|h|hrs?|hours?)(?:\s+ago)?",
                  re.IGNORECASE)
_AGE_SECONDS = {"h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400}
_MONTH_DAY = re.compile(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2})(?:, (\d{4}))?")


def _cell_text(cell):
    text = _MD_LINK.sub(r"\1", _MD_IMAGE.sub("", cell))
    return _plain(text.replace("**", "").replace("__", "")).replace("\\|", "|")


def _cell_url(cell):
    """The first link in a table cell, Markdown or <a href>, or None."""
    cell = _MD_IMAGE.sub("", cell)
    found = [match for match in (_MD_LINK.search(cell), _HTML_HREF.search(cell)) if match]
    if not found:
        return None
    first = min(found, key=lambda match: match.start())
    return html.unescape(first[2] if first.re is _MD_LINK else first[1])


def _cells(line):
    return [cell.strip() for cell in _UNESCAPED_PIPE.split(line.strip().strip("|"))]


def _markdown_tables(text):
    """(the nearest heading above, lowercased header texts, body rows of raw cells)
    for each table in a Markdown document."""
    heading, lines = None, text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        elif (line.lstrip().startswith("|") and i + 1 < len(lines)
              and _TABLE_RULE.fullmatch(lines[i + 1].strip())):
            body = []
            for row in lines[i + 2:]:
                if not row.lstrip().startswith("|"):
                    break
                body.append(_cells(row))
            yield heading, [_cell_text(cell).lower() for cell in _cells(line)], body


def _heading_category(heading):
    for pattern, category in _HEADING_CATEGORIES:
        if heading and pattern.search(heading):
            return category
    return None


def _listed_at(text, now):
    """(unix time, whether it was counted back from now) for a list's date cell:
    "2d", "3 days ago", "1mo", "Sep 20, 2026", or "Sep 20", read as the latest
    Sep 20 no later than tomorrow. (None, False) for anything else."""
    text = text.strip()
    age = _AGE.fullmatch(text)
    if age:
        unit = age[2].lower()
        return now - int(age[1]) * _AGE_SECONDS["mo" if unit.startswith("mo") else unit[0]], True
    day = _MONTH_DAY.fullmatch(text)
    if day is None:
        return None, False
    year = int(day[3]) if day[3] else datetime.datetime.fromtimestamp(
        now, datetime.timezone.utc).year
    try:
        moment = datetime.datetime.strptime(f"{day[1]} {day[2]} {year}", "%b %d %Y")
        if not day[3] and moment.replace(tzinfo=datetime.timezone.utc).timestamp() > now + 86400:
            moment = moment.replace(year=year - 1)
    except ValueError:
        return None, False
    return moment.replace(tzinfo=datetime.timezone.utc).timestamp(), False


def _readme_rows(text, now):
    """The postings in every table whose header names a company and a role.

    A "↳" company cell repeats the company of the row above. The link comes from
    the apply column, else from the role cell; a row with no link is skipped.
    """
    rows = []
    for heading, header, body in _markdown_tables(text):
        company, columns = None, {}
        for i, name in enumerate(header):
            if name in _README_COLUMNS:
                columns.setdefault(_README_COLUMNS[name], i)
        if "company" not in columns or "title" not in columns:
            continue
        category = _heading_category(heading)
        for cells in body:
            def cell(field):
                i = columns.get(field)
                return cells[i] if i is not None and i < len(cells) else ""
            name = _cell_text(cell("company"))
            company = company if name == "↳" else name
            title = _cell_text(cell("title"))
            url = _cell_url(cell("apply")) or _cell_url(cell("title"))
            if not (company and title and url):
                continue
            posted, relative = _listed_at(_cell_text(cell("date")), now)
            place = _cell_text(cell("location"))
            row = _posting(f"github_readme:{_url_key(url)}", company, title, url,
                           [place] if place else [], posted, posted, category)
            rows.append({**row, "date_is_relative": True} if relative else row)
    return rows


def github_readme(url):
    return _readme_rows(web.read_text(url, time.monotonic() + web.FETCH_TIMEOUT), time.time())
