"""Jobs a request starts on a daemon thread, which a restore waits for."""
import threading
from concurrent.futures import Future

from fastapi import HTTPException

from cairn import store
from cairn.server import system

START_TIMEOUT = 10  # seconds a background job gets to take the run lock


def launch(work, on_failure=None):
    """Run work(started) on a daemon thread. Returns what it passes to started, if anything.

    Whatever work raises before calling started is raised here, which is how a held
    run lock reaches the request as a 409. A later failure goes to on_failure(error)
    and nowhere else.
    """
    ready = Future()

    def body():
        try:
            work(lambda value=None: ready.set_result(value))
        except Exception as e:  # noqa: BLE001 - reported to the request or on_failure
            if not ready.done():
                ready.set_exception(e)
            elif on_failure:
                on_failure(e)
        finally:
            store.close_thread()
            system.maintenance.leave()
            if not ready.done():
                ready.set_exception(RuntimeError("the job finished without starting"))

    # a restore waits for the job, which outlives the request that started it
    system.maintenance.hold()
    threading.Thread(target=body, daemon=True).start()
    try:
        return ready.result(timeout=START_TIMEOUT)
    except TimeoutError:
        raise HTTPException(500, "background job did not start") from None
