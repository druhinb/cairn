# Cairn

A job feed for new-grad and early-career engineers that runs on your Mac. Each run
pulls postings from public GitHub feeds and from the job boards of companies you
follow, drops the ones outside your preferences, and ranks the rest for fit and
company tier. Cairn then summarises the requirements of the top matches,
and the run pushes the day's picks to your phone. The app tracks each application
from saved to offer.

It won't apply for you or scrape LinkedIn or Indeed. It needs no cloud service or
account, and everything stays in one folder on your Mac.

![Jobs](docs/screenshots/jobs.png)

![Applications](docs/screenshots/applications.png)

## Requirements

- macOS 13 or later. The window and the daily schedule are macOS-only.
- Python 3.11 or later. [uv](https://docs.astral.sh/uv/) installs both Python and
  the app; get it with `brew install uv`.
- [Claude Code](https://claude.com/claude-code), installed and logged in, with
  `claude` on your `PATH`, or an API key for another provider (see
  [AI providers](#ai-providers)).
- `pdftotext`, if you want setup to read a PDF resume. `brew install poppler`
  installs it.

## Install

```
uv tool install git+https://github.com/druhinb/cairn
```

or `pipx install git+https://github.com/druhinb/cairn`.

Then:

```
cairn init --from-resume resume.pdf   # draft profile.md and config.toml
cairn doctor                          # check Python, Claude Code, your files
cairn seed                            # mark today's backlog seen
cairn ui                              # open the app
```

`init --from-resume` makes one call to your AI provider, then asks a few questions (roles,
locations, work authorization, graduation date). `.txt` and `.md` resumes work too.
To do the same in the app, run `cairn ui` first and follow the setup wizard.
Plain `cairn init` writes example files for a made-up candidate to edit by
hand. `seed` makes the first run rank only postings that appear after it, and
`doctor` prints the fix for every failed check.

### Updating

When it opens, Cairn checks for a new version, at most once a day, and shows it
in the sidebar and the menu bar. To update, run `uv tool upgrade cairn-jobs`, or `pipx upgrade cairn-jobs`
if you installed with pipx. Your jobs, notes and settings stay as they are.

## The app

`cairn ui` opens a window; `cairn ui --no-window` opens the same app in
your browser. A sidebar holds the views and a run card, the middle pane lists
postings, and the right pane shows the selected one.

**Jobs** lists every posting that matches your preferences, ranked by fit plus
tier. Turn off *Matches my preferences* to see the whole feed. Filter chips narrow
by fit, tier, source, category, location, posting age, and status; *Hide passed* is
on by default. Search covers company, title, and requirements. Group by date or
company, or sort by newest, company, or recently updated. Scores show as
coloured meters and statuses as coloured pills. The detail pane shows both scores
with the ranker's reason, the requirements summary (or a *Summarise requirements*
button), a note that saves as you type, the status history, and the raw posting
fields.

**Latest run** shows the day's picks, the postings the newest run ranked.
**Saved** shows everything you saved.

**Applications** tracks each posting through saved → applied → interviewing →
offer, or rejected or withdrawn. The board view has a column per stage, with
rejected and withdrawn in one Closed column; drag a card, or focus it and press
`[` or `]`, to move it. The list view is a sortable table. Passing on a posting
hides it here and in Jobs; the Status filter still finds it.

**Runs** starts a run (with an optional limit, fit threshold, or dry run), shows
its phases, counters, and live log while it runs, and lists past runs; click one
to read its log.

**Settings** edits every setting in `config.toml` with inline validation, and
`profile.md` in an editor that highlights the `TODO:` lines left to fill. It also
manages the feeds and the watchlist, installs or removes the daily schedule, and
runs the doctor checks.

**Setup** opens on first launch. You upload a resume, check the profile Cairn
drafts from it, and answer a few questions. *Redo setup* in Settings opens it again.

**Command palette.** `⌘K` opens a palette of every action, grouped by view, with
recent actions first: go to a view, pin the current filters, switch theme, fetch
company icons, run now, open a Settings section, or search Jobs for what you
typed. The sidebar footer switches between Auto, Light, Dark and Contrast themes
(Auto follows your system's contrast setting) and between comfortable and compact
rows. A five-step tour shows the main controls on your first visit; *Take the
tour* in the palette or the shortcuts panel replays it. In Jobs, Shift+click or
`⇧J`/`⇧K` selects several postings, and `x`, `s` or `a` then acts on all of them
with one Undo. After you open a posting and come back within half an hour, the
detail pane asks whether you applied. Applications has *Export CSV* and *Import
CSV*: the import shows a preview first, matches rows to postings by id or by url,
and lists the rows it could not match.

| Key | Action |
|---|---|
| `j` / `k` (or `↓` / `↑`) | Next / previous posting |
| `Enter` / `o` | Open the posting |
| `s` | Save |
| `a` | Mark applied |
| `x` | Pass |
| `n` | Write a note |
| `/` | Search |
| `⌘K` | Command palette |
| `⇧J` / `⇧K` | Select the next / previous posting too |
| `Esc` | Close a popover or panel, clear the search or the selection |
| `1`–`8` | Today, Jobs, Latest run, Saved, Applications, Insights, Runs, Settings |
| `g` / `G` | Agree with the score / score too high |
| `[` / `]` | Move an application card a column left / right |
| `r` | Run now |
| `?` | Show every shortcut |

## Sources

Runs read four GitHub feeds by default:

| Feed | Carries |
|---|---|
| SimplifyJobs `New-Grad-Positions` | new-grad and early-career full-time roles |
| SimplifyJobs `Summer2027-Internships` | internships, with their terms |
| vanshb03 `New-Grad-2026` | new-grad roles, without categories |
| vanshb03 `Summer2027-Internships` | internships, one season each |

The watchlist adds a company's own public job board:

| Board | Add by | Board URL |
|---|---|---|
| Greenhouse | name or URL | `boards.greenhouse.io/stripe` |
| Lever | name or URL | `jobs.lever.co/palantir` |
| Ashby | name or URL | `jobs.ashbyhq.com/openai` |
| SmartRecruiters | name or URL | `jobs.smartrecruiters.com/ServiceNow` |
| Workable | name or URL | `apply.workable.com/huggingface` |
| BambooHR | name or URL | `rei.bamboohr.com/careers` |
| Workday | URL only | `nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite` |

Feeds and pages, added under `[[sources]]` in `config.toml` or in Settings › Sources:

| Kind | Location | Carries |
|---|---|---|
| `github_readme` | raw URL of a Markdown file | Every table with Company and Role, Job Title or Position columns; "↳" repeats the company above; a heading naming Software, Data or Quant sets the category |
| `hn_hiring` | `whoishiring` or a thread URL | Each top-level comment in Hacker News' monthly "Who is hiring?" thread; `whoishiring` follows the newest thread, plus last month's in the thread's first week |
| `yc_waas` | a `www.ycombinator.com/jobs` URL | YC startup jobs that take new grads or ask for at most a year, from the page and each city page it links |
| `remoteok` | `https://remoteok.com/api` | Remote jobs tagged for engineering or data |
| `usajobs` | a search keyword | Federal jobs in series 2210, 1550, 0854 and 1515; needs `usajobs_email` and the USAJOBS API key saved under API keys in Settings |
| `page` | a careers page URL, plus `company` | Each link whose text names a role; links that appear or disappear between runs become new or delisted postings. Pages that render with JavaScript show nothing |

Add a company in Settings › Sources by name or board URL. The app tries a name on
each board in the order above, and the first board that lists a job wins. Workday
needs the URL, because a name says nothing about its tenant, shard (`wd1`, `wd5`,
...) or site. In `config.toml` a Workday board's location takes the form
`<tenant>.<wdN>/<site>`:

```toml
[[watchlist]]
kind = "workday"
location = "nvidia.wd5/NVIDIAExternalCareerSite"
company = "NVIDIA"
```

## Daily schedule

```
cairn schedule install --hour 7    # --minute too; 7:00 by default
cairn schedule status
cairn schedule remove
```

`install` writes a launchd job, `~/Library/LaunchAgents/app.cairn.daily.plist`,
that runs `cairn run` once a day while you are logged in. Its output goes to
`~/.cairn/launchd.out` and `launchd.err`; each run's events also go to
`~/.cairn/run.log` and the Runs view.

## CLI reference

| Command | What it does |
|---|---|
| `init` | Write the example `config.toml` and `profile.md`; never overwrites |
| `init --from-resume FILE [--yes] [--overwrite]` | Draft `profile.md` from a `.pdf`, `.txt` or `.md` resume; `--yes` accepts the suggested answers, `--overwrite` replaces an edited profile |
| `init --apply-draft [--overwrite]` | Apply the draft a previous `--from-resume` saved |
| `run [--limit N] [--fit N] [--dry-run]` | Fetch, rank and summarise new postings; `--limit` takes the N newest, `--fit` changes the cutoff for this run, `--dry-run` changes nothing |
| `fetch [--limit N]` | List new postings without ranking them (25 by default) |
| `seed` | Mark the current backlog seen |
| `status` | Postings stored, applications logged, last run |
| `applied ID [--note TEXT]` | Log that you applied; any unambiguous id prefix works |
| `track ID [STATUS] [--note TEXT] [--clear]` | Set saved, applied, interviewing, offer, rejected, withdrawn or passed; `--clear` forgets the status, note and history |
| `serve [--host H] [--port N]` | Serve the web app without a window on 127.0.0.1, localhost or ::1 (any free port); open the URL it prints, whose token lets that browser in |
| `ui [--no-window] [--port N]` | Open the app in a window, or in the browser |
| `icons [--limit N] [--reset-failures]` | Look up company websites and download their icons until none are left, or N companies; `--reset-failures` retries the ones that failed before |
| `doctor` | Check this machine and print the fix for each failure |
| `schedule install [--hour H] [--minute M]` | Install the daily launchd run |
| `schedule status` / `schedule remove` | Show or remove it |
| `autostart install` / `status` / `remove` | Open the app at login |
| `llm` / `llm set PROVIDER [--model M] [--cheap-model M] [--base-url U] [--key-stdin]` / `llm test` | Show, choose or test the model provider; `set` asks for the key |
| `update-check [--force]` | Check GitHub for a newer release |
| `backup [--to DIR]` | Save config, profile, database and icons to one zip (default `~/Downloads`) |
| `restore ZIP [--yes]` | Replace your data with a backup's; the old files move to `~/.cairn.bak-<stamp>`, which Cairn never deletes |
| `settings export` | Print `config.toml` |

The API also serves `GET /api/calendar.ics`, a calendar of your interview stages.

## Where things live

```
~/.cairn/
  config.toml      settings; only the keys you changed
  profile.md       what you want: roles, locations, authorization, company tiers
  pipeline.db      postings, scores, summaries, applications, runs
  run.log          what each run did
  logos/           company icons
  launchd.out      output of the scheduled run
  launchd.err
```

Set `CAIRN_HOME` to use a different folder.

## AI providers

Cairn ranks postings with Claude Code by default. Without it, pick another
provider in Settings › AI provider or with `cairn llm set`: the Anthropic
API (pay as you go, $5 minimum) or a free tier from Google Gemini, Groq, Mistral
or OpenRouter. Ollama runs models on your Mac, offline and free. Each provider's
panel walks you through getting a key and has a Test button. Keys go in
`~/.cairn/secrets.toml`, readable only by you, or in
`CAIRN_<PROVIDER>_KEY`; they never go anywhere but the provider's own
endpoint. Free tiers limit requests, and the app paces its calls to stay under
them. Gemini's free tier uses prompts to improve Google's products, which
includes your profile text.

## Cost

The software is free. Model calls go through the provider you chose and count
against its subscription, credit or free tier:

- **Ranking**: one `sonnet` call per `rank_batch_size` (25) new postings. After
  `seed`, a normal day is a handful of calls.
- **Summaries**: one posting-page fetch and one `haiku` call each, for at most
  `max_summaries_per_run` (25) postings a run. A run never redoes a stored summary.
- **Onboarding**: one `sonnet` call per resume.

A typical day is a few ranking calls and up to 25 short summary calls. Raise
`fit_threshold`, lower `max_summaries_per_run`, or turn off `fetch_descriptions`
to spend less.

## Privacy

What leaves your Mac:

- requests to the GitHub feeds and the job-board APIs you follow,
- the posting pages of the roles it summarises,
- lookups for company icons. The app sends company names to Clearbit's
  autocomplete and to Wikidata to find each company's website, fetches the home
  page of that site (and of a guessed one, to confirm the guess) and its favicon,
  and sends the domain to DuckDuckGo's and Google's favicon services when the
  site has no icon or only a 16-pixel one. It contacts only public addresses and
  skips any on your own network. Turn off Fetch icons in Settings, or set
  `company_icons = false`, to stop these requests,
- if you set `notify_ntfy_topic`, a push to ntfy.sh with the top companies, titles,
  fit and tier scores, and posting links. Treat the topic as a password. Anyone who
  knows it can read your pushes, so use a long random one.

Your profile, resume text, and postings go only to the AI provider you picked in
setup: the local `claude` CLI, which sends them to Claude under your own account,
a hosted API under your own key, or Ollama running on your Mac. Your notes, statuses
and application history never leave the machine. The app's pages load nothing from the internet.

## Developing

```
git clone https://github.com/druhinb/cairn && cd cairn
uv venv && uv pip install -e ".[test]"
.venv/bin/python -m unittest discover -s tests
CAIRN_HOME=$(mktemp -d) .venv/bin/cairn init && .venv/bin/cairn serve --port 8766
```

The frontend is plain ES modules under `src/cairn/server/static`, and the
app serves them with no build step. Set `CAIRN_HOME` to keep test data
away from your own.

## Tuning and license

[docs/tuning.md](docs/tuning.md) explains every setting, from the relevance filters
to fit and tier, summaries, sources, icons, notifications and cost.

AGPL-3.0-or-later. See [LICENSE](LICENSE).
