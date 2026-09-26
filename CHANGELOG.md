# Changelog

This file lists every notable change to this project. It follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Cairn runs on Windows 10 and 11. Install it with uv; the daily run uses Task
  Scheduler. The app download is still for Macs only.

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

[0.0.2]: https://github.com/druhinb/cairn/releases/tag/v0.0.2
