"""The run log: one timestamped plain-text line per event, appended to a file.

The file is reopened for every line so each write is on disk before the next event,
which is what lets a run record the byte range its lines occupy. That range runs
from the run_start line through the closing run_done or run_failed line. The run
lock keeps other runs out of it; events from another
thread in the same process (a server thread, say) can still land inside it.
"""
import datetime
from pathlib import Path

from cairn.core import events


def _summary_done(d):
    if d.get("err"):
        outcome = f"failed: {d['err']}"
    else:
        outcome = "stored" if d.get("keywords") else "nothing usable"
    return f"[summary] {d['id']}  {outcome}"


def _run_done(d):
    parts = [f"run {d['run_id']} {d['status']} in {d['elapsed']:.1f}s"]
    parts += [f"{k}={v}" for k, v in d["counts"].items()]
    return "  ".join(parts)


_LINES = {
    "run_start": lambda d: f"run {d['run_id']} started  " + "  ".join(
        f"{k}={v}" for k, v in d.items() if k != "run_id"),
    "phase_start": lambda d: f"== {d['name']} ==",
    "phase_end": lambda d: f"[{d['name']}] {d['seconds']:.1f}s",
    "info": lambda d: d["text"],
    "warn": lambda d: f"WARN {d['text']}",
    "error": lambda d: f"ERROR {d['text']}",
    "posting_ranked": lambda d: (
        f"[rank] fit {d['fit']} tier {d['tier']}{'  below floor' if d['below_floor'] else ''}"
        f"  {d['company']} — {d['title']}  {d['id']}"),
    "rank_progress": lambda d: f"[rank] ranked {d['done']} of {d['total']}",
    "summary_progress": lambda d: f"[jd] summarised {d['done']} of {d['total']}",
    "summary_start": lambda d: f"[summary] {d['id']}  starting",
    "summary_done": _summary_done,
    "run_done": _run_done,
    "run_failed": lambda d: f"run {d['run_id']} failed: {d['error']}",
}


def line(event):
    """The event as one log line, without the trailing newline."""
    render = _LINES.get(event.kind, lambda d: f"{event.kind} {d}")
    stamp = datetime.datetime.fromtimestamp(event.at).isoformat(sep=" ", timespec="seconds")
    return f"{stamp}  " + " ".join(str(render(event.data)).splitlines())


def attach(path):
    """Append every event to path from now on. Returns the unsubscribe callable."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(event):
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line(event) + "\n")

    return events.subscribe(write)
