# Changelog

This file lists every notable change to this project. It follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- The starter watchlists are seven lists that each hold one kind of company:
  AI labs, AI apps and tools, Dev tools and infrastructure, Fintech, Consumer
  apps, Defense, space and hardware, and Quant and trading. Databricks, for
  one, moved from AI labs to Dev tools and infrastructure.
- Settings › Sources shows each starter list with one switch that follows or
  turns off all its companies, a count of how many are on, and its companies
  underneath. Companies you added yourself have their own group. This takes
  the place of the Add a starter list menu.

## [0.0.7] — 2026-10-01

- Runs look up company icons and read how many years each job asks for while
  they rank, so ranking starts minutes sooner. On a first run with 2,200 new
  postings, ranking used to wait about five minutes for those pages.
- Leaving Most years a job can ask for empty saves again, in setup and in
  Settings. It used to fail, and setup could stop halfway through saving.
- A new setting, Jobs to show, picks internships, new grad (full-time) jobs
  or both. Internships hides full-time jobs, and new grad hides internships.
  It takes the place of the Internships switch: if you had turned that off,
  Cairn now shows new grad jobs only.
- `cairn init --from-resume` asks which jobs to show. It suggests internships
  when you graduate more than a year from now, and new grad otherwise.
- Setup asks its questions one card at a time, starting with whether you want
  internships, new grad jobs or both. It skips the questions that don't apply,
  such as experience when you only want internships. The job title lists and
  degrees show only when you turn on Advanced options.
- Retake the questions, in Settings › Profile, asks the setup questions again
  without a new resume. It starts from your current settings and keeps your
  profile, changing only the lines the answers cover.

## [0.0.6] — 2026-10-01

- Grouped by date or company, Jobs loads each group whole before the next,
  so scrolling no longer adds rows to groups above or changes their counts.
  Each group's count is its full total from the start, and companies are
  listed alphabetically.

## [0.0.5] — 2026-09-26

- Cairn checks the careers pages of companies you follow every hour, even with
  the app closed, and sends a phone alert when a strong match appears. If you
  set up the daily run before this version, press Change time in Settings ›
  Schedule to turn the hourly check on.
- Each row shows when the job was posted, with how long ago Cairn found it
  underneath. Postings from a source's first read have no found age.
- Postings posted more than Recent days ago no longer show in Jobs, unless
  you saved, applied to or otherwise tracked them. A board editing its
  listings no longer makes old postings count as new.
- Recent days is 90 by default, up from 21, so new grad and intern roles
  posted in July and still open show up in the fall. Setup asks for it too,
  as Posted within.
- Setup's AI step has the AI limits, such as requests at once and postings
  per batch, filled in with Cairn's defaults. Change them there or later in
  Settings › Ranking.
- A role one company lists in several cities shows as one row. The posting's
  details list the other places, with a link to each.
- Postings with a fit under your fit threshold are hidden. Jobs shows how many
  beside the match count, and Show them brings them back.
- Setup and Settings ask how many years of full-time experience you have and
  the most a job can ask for. Full-time jobs asking for more stay out of your
  matches and are never scored.
- A new setting, Only jobs in the US, hides postings that list only places
  outside the US.
- Setup picks the title keywords and the titles and fields to skip from your
  resume. The Roles checkboxes add or take out a role's titles, the Jobs to
  skip checkboxes do the same for groups of skip words such as senior roles
  or hardware, and you can change the lists before you finish.
- Pick the ranking and summary models in one place, Settings › AI provider.
  With Claude Code, the models set there used to be ignored. Models set in
  Ranking before this version carry over.
- Search also looks at location, category and internship term, so Winter 2027
  lists the postings for that term.
- A role taken down and posted again carries a Reposted mark.
- Save your watchlist as a file to share it, or add the companies from a file
  someone shared, in Settings › Sources or with `cairn watchlist export` and
  `cairn watchlist import`.
- Five starter watchlists: Big tech, AI labs, Quant and trading, Fintech and
  dev tools, and Hot startups. Pick them during setup or add them in
  Settings › Sources.
- Setup has a step for phone alerts, with instructions for the ntfy app and a
  button that sends a test alert.
- Run setup again from the top of Settings › Profile.
- The Follow button on suggested companies works again.
- The first time Cairn opens, the sidebar no longer says Cairn stopped
  responding because the new database was locked.

## [0.0.4] — 2026-09-26

- Job lists show each internship's season, such as Summer 2027, in place of the
  source it came from. The posting's details still name the source.

## [0.0.3] — 2026-09-26

- Cairn runs on Windows 10 and 11. Install it with uv; the daily run uses Task
  Scheduler. The app download is still for Macs only.
- The first run ranks every posting that matches your preferences, where it used
  to rank at most 100 from the last two weeks. Max ranked per run, in Settings,
  applies to the runs after it.
- The profile that setup drafts from your resume has no TODO lines left to fill
  in. Your answers to the setup questions add your work authorization, the
  companies you rate highest and where you want to work.
- The tour starts on its own once your first run finishes. You no longer have to
  find it under the keyboard shortcuts.

## [0.0.2] — 2026-09-26

- `cairn ui` shows as Cairn, with the Cairn icon, in the Dock and the app
  switcher, where it used to show the Python version.
- On Python 3.11 releases before 3.11.10, Cairn no longer turns away your own
  browser when it connects over IPv6.

- Each release now includes the app for Apple silicon Macs as
  `Cairn-<version>.zip`.
- One spelling per city: NYC, New York City and New York, NY all count as New
  York, NY, and SF and LA work the same way.
- The Skills to close card on Today keeps its counts inside the card.
- On Python 3.11, an interview set in the hour the clocks skip in spring shows at
  the right time in the calendar export.
- Guides for contributing and for reporting security problems, and checks that
  run on every pull request.

## 0.0.1 — 2026-09-25

The first release.

- One list of new-grad and internship postings from the SimplifyJobs, vanshb03
  and speedyapply lists, Hacker News "Who is hiring?", Y Combinator, RemoteOK,
  USAJOBS, any careers page, and the job boards of companies you follow
  (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, BambooHR). The
  same job listed in several places shows once.
- Filters for role, location, degree, graduation date, internship term,
  sponsorship and pay, saved as your preferences.
- Each new posting gets a fit score and a company score with a short reason, from
  the AI provider you pick: Claude Code, the Anthropic API, Google Gemini, Groq,
  Mistral, OpenRouter, Ollama or any OpenAI-compatible service. Agree or Too high
  on a score tunes later ones. A monthly call cap stops ranking at a budget.
- On-demand summaries of a posting's pay, sponsorship and the requirements your
  profile meets or misses.
- Setup from your resume: Cairn drafts your profile, you set a few preferences,
  and the first search runs with live progress.
- Today, Jobs, Latest run, Saved, Applications, Insights, Runs and Settings
  views, with company icons, keyboard shortcuts, a command palette (⌘K), light,
  dark and contrast themes, and a compact density.
- Application tracking: statuses from saved to offer, notes, interview dates,
  a checklist and files per posting, follow-up reminders, a calendar export, and
  CSV export and import.
- A daily run on a schedule, a menu-bar item, start at login, an update check,
  push notifications through ntfy.sh, and backup and restore.
- A macOS app, the `cairn` command, and `cairn doctor`, which checks what the app
  needs and says how to fix it.
- All data stays in `~/.cairn` on your Mac. The app only answers the window it
  opened.

[0.0.7]: https://github.com/druhinb/cairn/releases/tag/v0.0.7
[0.0.6]: https://github.com/druhinb/cairn/releases/tag/v0.0.6
[0.0.5]: https://github.com/druhinb/cairn/releases/tag/v0.0.5
[0.0.4]: https://github.com/druhinb/cairn/releases/tag/v0.0.4
[0.0.3]: https://github.com/druhinb/cairn/releases/tag/v0.0.3
[0.0.2]: https://github.com/druhinb/cairn/releases/tag/v0.0.2
