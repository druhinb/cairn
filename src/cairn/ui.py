"""Terminal output: rich on a TTY, plain log-safe lines everywhere else.

Progress reaches the terminal as events once `attach()` subscribes the renderer.
The table printers below are called directly by the commands that show results.

launchd redirects stdout into a file, and a file full of cursor escapes is
unreadable, so off a TTY no Live/Progress is ever built. Everything untrusted is
escaped too; a title like "Engineer [New Grad]" is otherwise swallowed as rich markup.

Every renderer here has two branches. The TTY branch may use colour, tables, and
live regions; the plain branch must emit the same facts as flat text and nothing
else. Tests assert zero ESC bytes off a TTY, so keep that invariant when editing.
"""
import sys

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from cairn import events, settings

# isatty(), not console.is_terminal: rich honors FORCE_COLOR, which would let an
# inherited env var put a live Progress region into redirected output.
# pythonw, which Windows starts for the daily task and at login, has no stdout
IS_TTY = sys.stdout is not None and sys.stdout.isatty()

# force_terminal=False, not just no_color=True. no_color strips colour but keeps
# attributes, so a bold style still emits "\x1b[1m...\x1b[0m" — enough to corrupt
# a log. Declaring it a non-terminal suppresses every escape.
console = Console(force_terminal=IS_TTY, no_color=not IS_TTY,
                  highlight=False, soft_wrap=True)

ID_PREFIX = 8  # how much of a posting id to show; `applied` accepts any unique prefix


def _say(text, style=None):
    console.print(escape(str(text)), style=style)


def info(text):
    _say(text)


def warn(text):
    _say(f"WARN {text}", style="yellow")


def error(text):
    _say(f"ERROR {text}", style="bold red")


def rule(text):
    if IS_TTY:
        console.rule(f"[bold cyan]{escape(str(text))}", style="cyan")
    else:
        _say(f"== {text} ==")


def fit_style(fit):
    """Colour a 0-100 fit score by how worth your time it is."""
    if fit is None:
        return "dim"
    if fit >= 75:
        return "bold green"
    if fit >= settings.get().fit_threshold:
        return "green"
    if fit >= 40:
        return "yellow"
    return "dim red"


def tier_style(tier):
    """Colour company calibre against the configured floor."""
    if tier is None:
        return "dim"
    if tier >= 75:
        return "bold green"
    if tier >= settings.get().tier_floor:
        return "green"
    return "dim red"


def _short(job_id):
    return (job_id or "")[:ID_PREFIX]


def postings(jobs, extra=0):
    """List postings. A table on a TTY, one flat line each otherwise.

    Off a TTY the full id is printed so a log stays copy-pasteable; on a TTY a
    short prefix keeps the table readable and `applied` resolves prefixes.
    """
    if not IS_TTY:
        for job in jobs:
            fit = job.get("fit")
            score = f"fit {fit:>3}  " if fit is not None else ""
            flags = "R" if job.get("repeat") else " "
            info(f"  {flags} {score}[{job.get('category')}] {job.get('company_name')} — "
                 f"{job.get('title')}  {job.get('id')}")
        if extra > 0:
            info(f"  ... and {extra} more")
        return

    scored = any(j.get("fit") is not None for j in jobs)
    table = Table(box=None, pad_edge=False, show_header=True, header_style="dim",
                  expand=True)
    if scored:
        table.add_column("fit", justify="right", width=3)
        table.add_column("tier", justify="right", width=4)
    table.add_column("category", style="cyan", width=11, no_wrap=True, overflow="ellipsis")
    # ratio + expand lets `role` absorb the leftover width; without it a no_wrap
    # column claims its full content width and squeezes the fixed ones to ellipses.
    # One line per posting: a wrapped title turns a 25-row list into 50 rows of noise.
    table.add_column("role", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("id", style="dim", width=ID_PREFIX, no_wrap=True)
    for job in jobs:
        repeat = " [yellow]↩[/yellow]" if job.get("repeat") else ""
        role = (f"[bold]{escape(str(job.get('company_name')))}[/bold] — "
                f"{escape(str(job.get('title')))}{repeat}")
        row = [escape(str(job.get("category") or "")), role, _short(job.get("id"))]
        if scored:
            fit, tier = job.get("fit"), job.get("tier")
            row.insert(0, f"[{tier_style(tier)}]{'—' if tier is None else tier}[/]")
            row.insert(0, f"[{fit_style(fit)}]{'—' if fit is None else fit}[/]")
        table.add_row(*row)
    console.print(table)
    if extra > 0:
        console.print(f"[dim]  ... and {extra} more[/dim]")


def recent_applications(recent):
    """Recently logged applications: (job_id, entry) pairs."""
    if not recent:
        return
    if not IS_TTY:
        for job_id, entry in recent:
            info(f"  {(entry.get('applied_at') or '')[:10]}  {entry.get('company')} — "
                 f"{entry.get('title')}  {job_id}")
        return
    table = Table(box=None, pad_edge=False, show_header=True, header_style="dim",
                  title="recent applications", title_justify="left", title_style="bold")
    table.add_column("applied", style="cyan", width=10)
    table.add_column("role", no_wrap=True, overflow="ellipsis")
    table.add_column("id", style="dim", width=ID_PREFIX)
    for job_id, entry in recent:
        table.add_row((entry.get("applied_at") or "")[:10],
                      f"[bold]{escape(str(entry.get('company')))}[/bold] — "
                      f"{escape(str(entry.get('title')))}",
                      _short(job_id))
    console.print(table)


def _on_phase_end(data):
    info(f"[{data['name']}] {data['seconds']:.1f}s")


_RENDERERS = {
    "phase_start": lambda d: rule(d["name"]),
    "phase_end": _on_phase_end,
    "info": lambda d: info(d["text"]),
    "warn": lambda d: warn(d["text"]),
    "error": lambda d: error(d["text"]),
}


def render(event):
    """Show one event on the terminal. Kinds with no terminal form print nothing."""
    renderer = _RENDERERS.get(event.kind)
    if renderer is not None:
        renderer(event.data)


def attach():
    """Render every event from now on. Returns the unsubscribe callable."""
    return events.subscribe(render)


def summary(rows, title=None):
    """rows: (label, value) pairs. A table on a TTY, aligned plain lines otherwise."""
    if IS_TTY:
        table = Table(show_header=False, box=None, pad_edge=False,
                      title=title, title_justify="left", title_style="bold cyan")
        table.add_column(justify="right", style="dim")
        table.add_column(style="bold")
        for label, value in rows:
            table.add_row(escape(str(label)), escape(str(value)))
        console.print(table)
        return
    if title:
        _say(f"== {title} ==")
    width = max((len(str(label)) for label, _ in rows), default=0)
    for label, value in rows:
        _say(f"{str(label).rjust(width)}  {value}")
