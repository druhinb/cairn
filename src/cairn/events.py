"""In-process progress events: the pipeline emits, the terminal, log, and web UI listen.

Subscribers run synchronously on the emitting thread, so a slow subscriber slows the
run. One that raises is dropped and reported; its failure never reaches the run.
"""
import queue
import sys
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    kind: str
    at: float
    data: dict


_lock = threading.Lock()
_subscribers = []


def subscribe(fn):
    """Call fn(event) for every event from now on. Returns the unsubscribe callable."""
    with _lock:
        _subscribers.append(fn)

    def unsubscribe():
        with _lock:
            if fn in _subscribers:
                _subscribers.remove(fn)

    return unsubscribe


class DroppingQueue(queue.Queue):
    """A bounded queue that drops what does not fit and sets `dropped` when it does."""

    def __init__(self, maxsize):
        super().__init__(maxsize)
        self.dropped = False

    def offer(self, item):
        try:
            self.put_nowait(item)
        except queue.Full:
            self.dropped = True


# A consumer that stops reading, such as a stalled browser tab on the event stream,
# stays subscribed until its disconnect is noticed, and emit must never block on it
# or let its queue hold every event of a long run.
def queue_subscriber(maxsize=10000):
    """(queue, unsubscribe): every event is offered to a DroppingQueue."""
    events = DroppingQueue(maxsize)
    return events, subscribe(events.offer)


def emit(kind, **data):
    event = Event(kind, time.time(), data)
    with _lock:
        listeners = list(_subscribers)
    for fn in listeners:
        try:
            fn(event)
        except Exception as e:  # noqa: BLE001 - a broken listener must not fail the run
            _drop(fn, e)
    return event


def _drop(fn, error):
    with _lock:
        if fn in _subscribers:
            _subscribers.remove(fn)
    message = f"[events] dropped subscriber {fn!r}: {type(error).__name__}: {error}"
    # ui subscribes through this module, so a top-level import would be circular
    from cairn import ui  # noqa: PLC0415
    try:
        ui.warn(message)
    except Exception:  # noqa: BLE001 - the dropped subscriber may be the renderer itself
        print(f"WARN {message}", file=sys.stderr)
