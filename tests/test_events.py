"""The in-process event bus the pipeline reports progress through."""
import threading
import unittest
from unittest import mock

import helpers  # noqa: F401 - installs the suite settings guard

from cairn import events, ui


class EventsTest(unittest.TestCase):
    def listen(self, fn):
        self.addCleanup(events.subscribe(fn))

    def test_a_subscriber_receives_emitted_events(self):
        got = []
        self.listen(got.append)
        events.emit("info", text="hello")
        self.assertEqual([(e.kind, e.data) for e in got], [("info", {"text": "hello"})])
        self.assertGreater(got[0].at, 0)

    def test_unsubscribe_stops_delivery(self):
        got = []
        unsubscribe = events.subscribe(got.append)
        events.emit("info", text="one")
        unsubscribe()
        events.emit("info", text="two")
        self.assertEqual([e.data["text"] for e in got], ["one"])

    def test_queue_subscriber(self):
        queue, unsubscribe = events.queue_subscriber()
        self.addCleanup(unsubscribe)
        events.emit("phase_start", name="fetch")
        event = queue.get(timeout=1)
        self.assertEqual((event.kind, event.data), ("phase_start", {"name": "fetch"}))

    def test_a_full_queue_drops_and_says_so(self):
        queue, unsubscribe = events.queue_subscriber(maxsize=2)
        self.addCleanup(unsubscribe)
        for n in range(3):
            events.emit("info", text=str(n))
        self.assertTrue(queue.dropped)
        self.assertEqual([queue.get_nowait().data["text"] for _ in range(2)], ["0", "1"])
        self.assertTrue(queue.empty())

    def test_a_raising_subscriber_is_dropped_and_emit_carries_on(self):
        calls, got = [], []

        def broken(event):
            calls.append(event.kind)
            raise RuntimeError("renderer fell over")

        self.listen(broken)
        self.listen(got.append)
        with mock.patch.object(ui, "warn") as warn:
            events.emit("info", text="first")
            events.emit("info", text="second")
        self.assertEqual(calls, ["info"])
        self.assertEqual([e.data["text"] for e in got], ["first", "second"])
        warn.assert_called_once()
        self.assertIn("renderer fell over", warn.call_args.args[0])

    def test_emit_from_many_threads_delivers_every_event(self):
        got = []
        self.listen(got.append)

        def burst(n):
            for i in range(250):
                events.emit("info", text=f"{n}-{i}")

        threads = [threading.Thread(target=burst, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), 1000)
        self.assertEqual(len({e.data["text"] for e in got}), 1000)


if __name__ == "__main__":
    unittest.main()
