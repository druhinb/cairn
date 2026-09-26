# Tuning

Every setting below lives in `~/.cairn/config.toml` and has a default that
works. The file only needs the keys you change; `cairn init` writes one with
every key listed and commented out. The Settings view in the app edits the same
file.

An unknown key, a value of the wrong type, or a number outside the range given
below stops the run with the file and key named in the error. The Settings view
checks the same ranges before it saves.

## Which postings count as relevant

A run keeps a posting from a GitHub feed only when all of these hold:

- its category is in `allowed_categories`, or it has no category at all,
- its title matches an entry in `title_keywords`,
- no entry in `title_exclude` (wrong seniority: senior, staff, manager, phd) matches,
- no entry in `title_exclude_field` (wrong discipline: mechanical, firmware, field
  service, sales) matches.

Category and title are both required because each is unreliable alone. Category
alone admitted "Meteorologist" and "Survey Mapping Drafter", which upstream tags
`AI/ML/Data`; title alone admitted "Photonics Engineer" and "Field Service
Engineer". Together they cut a typical day's feed by about 44%. `engineer` in
`title_keywords` is broad on purpose, and `title_exclude_field` is what keeps that
breadth from dragging in hardware and non-software roles.

Some feeds (vanshb03 `New-Grad-2026`) carry no category. Their postings pass on the
title rule alone, with both exclude lists still applied, so the filter keeps those
feeds.

To widen the net, add to `title_keywords`. To drop a kind of junk you keep seeing,
add to `title_exclude_field`. `location_allow` narrows by location substring
(`["CA", "New York", "Remote"]`) and is empty, keeping every location, by default.

`recent_days` (21, 1 to 365) ignores postings not posted or updated in that
window.

### Internships

The internship feeds carry the season in a `terms` field, and most titles are a
bare "Software Engineer Intern". A title matching `intern_terms` (intern,
internship, co-op, coop) is an internship, and the filter keeps an internship only
when one of its terms equals, ignoring case, an entry in `wanted_intern_terms`.
That list is empty by default, so no internship passes until you list the terms
you can take, such as `["Fall 2026", "Spring 2027"]`. Update it as terms roll over.
`include_off_season_internships` is on by default; turning it off drops every
internship.

### Degrees and graduation year

The feed's `degrees` field lists the degrees a posting accepts, so a list that
omits yours excludes you: `["Master's", "PhD"]` is a graduate-only role. Set
`degrees_held` (for example `["Bachelor's"]`) to apply the check. The check treats
a posting with no degrees listed as unstated and keeps it; an empty `degrees_held`
turns the check off.

`graduation_year`, unset by default, flags any summarised posting whose stated
start year is earlier than yours. The year appears only in the description
("Graduate Software Engineer (2027 Start)"), and most postings say nothing; the
check never counts silence as a conflict.

## Fit, tier, and ordering

Claude ranks every relevant posting on two 0-100 axes:

- **fit**: does the role match you, judged against `profile.md`.
- **tier**: is the company a step up, judged against the anchors in the "Company
  tier" section of `profile.md`. Edit those anchors to move the goalposts.

Postings below `tier_floor` (55, 0 to 100) stay listed with a flag but never get a
summary or a place in the push. That keeps consultancies and non-tech enterprises
out of the summary budget while leaving them visible. Postings below
`fit_threshold` (60, 0 to 100) get no summary or push either.
`cairn run --fit N` changes the threshold for one run without touching
`config.toml`.

The list sorts by `fit + tier`, above-floor companies first, so a top firm at
fit 85 outranks an unremarkable company at fit 95.

Ranking is one call to `claude_model` (`sonnet`) per `rank_batch_size` (25, 1 to
100) postings. A batch whose reply does not parse gets `rank_retries` (1, 0 to 5)
extra attempts. After that its postings stay unseen and come back on the next run;
none of them shows up at fit 0.

Batches and summaries go out `model_concurrency` (4, 1 to 8) at a time. A batch of
25 takes about 40 seconds on `sonnet`, most of it spent writing the reasons, so four
at once cut a large first run to a quarter of the wait. Summaries run with thinking
off: on `haiku` that took one from about 70 seconds to about 4, with the same block.

`max_rank_per_run` is unset by default, so a run scores every new posting and
`max_summaries_per_run` then picks the real top. A cap on ranking takes the N
newest postings, because the feed arrives newest-first, and spends the budget on
whoever posted most recently while the best companies wait in the held-back pile.
The first run ignores the cap and ranks every match. Set an integer (1 or more) only
to bound the daily runs after it.

## Requirements summaries

The feeds carry no description text. With `fetch_descriptions` on, the default, a
run fetches the posting page for the top `max_summaries_per_run` (25, 0 to 500)
postings at or above `fit_threshold` and `tier_floor`, and `description_model`
(`haiku`) distills each to a TECH / DOMAIN / MUST / SIGNALS / TIMING block roughly 11 times smaller than
the page. Workday and Ashby serve their text through JSON APIs; everything else is
a plain HTML read, and about 95% of a typical feed succeeds. The run discards a
reply in any other shape, so a model's prose apology never lands as a requirement.
The app shows each stored summary and includes it in search; its "Summarise
requirements" button gets one for any other posting.

## Sources and watchlist

Postings come from four GitHub feeds by default: SimplifyJobs `New-Grad-Positions`
and `Summer2027-Internships`, and vanshb03 `New-Grad-2026` and
`Summer2027-Internships`. Each feed's postings carry its `owner/repo` name. A
`[[sources]]` table replaces all four defaults; `enabled = false` skips an entry
without deleting it. Any repo whose raw `listings.json` follows Simplify's schema
works as a source.

To follow a company directly, add its public job board to `watchlist`, which is
empty by default, in Settings › Sources or in `config.toml`:

```toml
[[watchlist]]
kind = "greenhouse"     # or "lever", "ashby", "smartrecruiters", "workable", "bamboohr", "workday"
location = "stripe"     # the slug in boards.greenhouse.io/stripe
company = "Stripe"      # optional; defaults to the slug, title-cased
```

For every kind but Workday, `location` is the slug in the board URL:
`jobs.smartrecruiters.com/ServiceNow`, `apply.workable.com/huggingface`, and the
subdomain of `rei.bamboohr.com`. A Workday board's location is
`<tenant>.<wdN>/<site>`, as in `nvidia.wd5/NVIDIAExternalCareerSite` for
`nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`. Each board gets 60 seconds
per run. The run reads Workday 20 jobs a page, up to 500 jobs, and Workable up to
20 pages, and a BambooHR board takes one extra request per job for its posting
date. The run drops whatever fails or misses the deadline, or leaves it undated,
with a warning. Workday gives dates as "Posted 3 Days Ago", so a Workday posting
keeps the date it had when a run first stored it.

Board postings get source names such as `greenhouse:stripe`. They carry no
category, so the title keywords, the exclude lists, and the degree and location
filters decide their relevance; a board names no degrees, which the degree filter
treats as unstated. A run keeps a posting listed by both a feed and a board once,
from whichever comes first, reading `sources` before `watchlist`. A board that
fails to load drops out of that run with a warning and never holds back the feeds.

TOML reads every key below a `[[...]]` header as part of that table, so these
tables must come after every plain key in the file.

## Feeds and pages

- **hn_hiring**: the title is the first header field that names a role, else
  "Engineer"; the locations are the fields that name a place or REMOTE, ONSITE or
  HYBRID. Comments without a "|" on the first line are skipped. Titles are loose,
  so `title_exclude` does most of the filtering.
- **github_readme**: the id comes from the apply URL. Dates like "2d" or "2mo"
  count back from the fetch and keep the first date stored; "Sep 20" means the
  latest Sep 20 up to tomorrow. Jobright's lists link to jobright.ai pages, so they
  never dedupe against Simplify; that list is not a default. speedyapply's FAANG+
  and Other sections read as Software.
- **yc_waas**: YC's pages list at most 50 jobs each, so the adapter also reads the
  city pages. Jobs asking for more than a year of experience are dropped.
- **remoteok**: a job passes when a tag is dev, engineer, software, data, backend,
  frontend, front end, full stack, ml or ai. Locations are "Remote" plus the job's
  own location.
- **usajobs**: series 2210, 1550, 0854 and 1515 are requested and checked again on
  each reply.
- **page**: an anchor counts when its text is 8–120 characters and names a role.
  Locations come from the text around the link. The fetch reads at most 512 KiB
  for up to 20 s, stops at 50,000 tags, keeps up to 300 role links, and names a
  posting by its link with only the id, jobid, job_id, gh_jid, req, requisition
  and posting query parameters. List, HN, YC, RemoteOK and USAJOBS replies are
  capped at 2 MiB.

## Company icons

Each company's website comes from the first of these that holds: the feed's
company link when it points at the company itself, an apply link on the
company's own domain, Clearbit's top autocomplete suggestion, the official
website of a Wikidata organisation, or a domain guessed from the name whose home
page title names the company. Clearbit and Wikidata count only when the
suggestion's name is the company's name (legal suffixes such as Inc aside) and
its domain holds a word of that name of three letters or more, or the name's
initials, so "GE Aerospace" never takes lexingtonmenus.com. The icon is the
site's `/favicon.ico`, else the largest icon its home page declares, else the
copy DuckDuckGo's or Google's favicon service holds; a favicon 16 px or narrower
gives way to a wider one from any source. A website with no icon anywhere gets
one more try through the later steps, so TikTok moves from lifeattiktok.com to
tiktok.com. A lookup that fails because the network is down leaves the company
pending; a company or icon that fails for a reason of its own waits 30 or 14
days before a retry.

Companies with the most active postings go first. A run spends up to 90 seconds
on 150 companies, 60 % of it on lookups and the rest on downloads, and picks up
where it left off next time. `cairn icons`, or Fetch icons now in
Settings, keeps going until nothing is left and runs alongside a daily run
without blocking it; `--reset-failures` retries the companies and icons that
failed before. Every request goes to a public address only; the
app refuses a link to an IP address, a private name such as `.local`, or a host
resolving to a private address, redirects included. Icons live in
`~/.cairn/logos`, and the browser only ever asks the local server for
them. Set `company_icons = false` to stop all of this; icons already downloaded
still show, and a company without one shows its initials.

## Feedback, links and groups

- Marking a score "agree" or "too high" with a reason (location, compensation,
  role, company, seniority, other) adds that posting to a calibration section in
  later ranking prompts: the 12 most recent verdicts, balanced between the two.
- A summary also records the stated salary (`SALARY:`), sponsorship
  (`SPONSORSHIP:`) and which requirements your profile meets or misses (`MET:`,
  `MISSING:`); the profile's skills and experience sections go into that one call.
  Summaries from before these lines still load. Re-summarise a posting to refresh
  them. Only pay stated in USD sorts and filters; other currencies read as
  unstated.
- Before ranking, a run checks the apply links of up to 60 new postings, and
  after icons up to 60 ranked ones not checked in three days (8 s each, 30 s in
  all, one check at a time). A 404 or 410, or a redirect to a Greenhouse, Lever,
  Ashby or Workday board's front page, marks the posting closed. A closed posting
  is not ranked or pushed, sorts last and stays visible. A redirect to a sign-in
  page settles nothing. Boards that render with JavaScript answer 200 for a dead
  job, so their links read open.
- Postings from different sources with the same company, title and places form
  one group; two whose apply links carry different requisition ids stay apart.
  Lists show one row per group, the member holding the application, else the one
  seen first, with the other sources under "also on"; ranking scores each group
  once and stores the score for every member.
- `monthly_call_cap` (unset by default) stops ranking and summaries once the last
  30 days hold that many model calls; the run log says so and the next run
  continues.

## Follow-ups and stages

- A posting in status applied with no later event for `follow_up_days` (14, 1 to
  90) days, or in interviewing with a stage three or more days past and nothing
  after it, needs a follow-up. Today lists them, and the push names the first
  two unless `notify_follow_ups` is off.
- A stage (screen, oa, onsite, final, other) carries a time and a note; adding
  one to a posting with no status sets it to interviewing. `GET /api/calendar.ics`
  exports the stages from the last 30 days to 90 days ahead (`?days=` changes
  the horizon) as one-hour events; open it to import them into Calendar. The
  app's port changes on each launch, so a subscription to the address does not
  hold.
- Moving a posting to applied adds four checklist items when its list is empty:
  resume version sent, referral asked, cover letter, thank-you note. Files are
  labels with a path on this Mac; nothing is copied.

## What counts as seen

- A run marks a posting seen only after it stores the posting's scores, and only if
  it ranked the posting. A crash anywhere earlier leaves the seen set untouched and
  the whole run retries next time.
- `cairn fetch` and `cairn run --dry-run` store the fetched feed but
  mark nothing seen and rank nothing.
- `cairn seed` marks the current backlog seen, so the first real run is not
  hundreds of old roles.

A run stores every posting from every source, relevant or not, so loosening a filter
needs no refetch.

## Notifications

[ntfy.sh](https://ntfy.sh) needs no account. Pushes are off while
`notify_ntfy_topic` is empty, the default. Pick a long, unguessable topic, set
`notify_ntfy_topic` to it, and subscribe the ntfy phone app to the same string; a
run then pushes a summary of company names and fit scores. Anyone who knows the
topic can read it, so treat it as a password. `notify_macos`, off by default,
also shows a local banner. A failed notification never fails a run.

## Claude

Every model call runs through the Claude Code CLI named by `claude_bin` (`claude`);
set an absolute path when it is not on `PATH`. `claude_model` (`sonnet`) ranks
postings and drafts the profile at setup, and `description_model` writes the
requirements summaries. Raising `fit_threshold` or `tier_floor`, or lowering
`max_summaries_per_run`, shrinks the number of summary calls.
