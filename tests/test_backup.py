"""Backup and restore of the home directory, through the module, the CLI, and the API."""
import contextlib
import io
import json
import os
import stat
import sys
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import temp_home, user_home

from cairn import backup, cli, insights, logos, paths, pipeline, settings, store
from cairn.server import system
from cairn.server.app import create_app

CONFIG = 'fit_threshold = 72\n'
PROFILE = "# Profile\n\nRust, Kafka.\n"


def _posting(posting_id):
    return {"id": posting_id, "company_name": "Acme", "title": "Software Engineer",
            "url": f"https://jobs.test/{posting_id}", "active": True, "is_visible": True,
            "date_posted": time.time()}


class BackupTestCase(unittest.TestCase):
    """A home at root/a with a config, profile, two postings, and one icon."""

    def setUp(self):
        self.root = self.enterContext(temp_home())
        self.out = self.root / "out"
        self.use_home("a")
        paths.config_file().write_text(CONFIG, encoding="utf-8")
        paths.profile_md().write_text(PROFILE, encoding="utf-8")
        store.upsert_postings([_posting("p1"), _posting("p2")], "feed")
        store.save_scores([{"id": "p1", "fit": 90, "tier": 70, "below_floor": False,
                            "fit_reason": "good"}])
        (paths.home() / "logos").mkdir()
        (paths.home() / "logos" / "acme.com.png").write_bytes(b"\x89PNG icon")

    def use_home(self, name):
        store.close()
        home = self.root / name
        home.mkdir(exist_ok=True)
        os.environ["CAIRN_HOME"] = str(home)
        self.addCleanup(store.close)
        return home

    def make_zip(self, entries, compression=zipfile.ZIP_STORED):
        path = self.root / "hand-made.zip"
        with zipfile.ZipFile(path, "w", compression) as bundle:
            for name, data in entries.items():
                bundle.writestr(name, data)
        return path

    def bomb(self):
        """A 208 KB zip whose pipeline.db unpacks to 200 MiB of zeros."""
        path = self.root / "bomb.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps({"schema": store.SCHEMA_VERSION}))
            with bundle.open("pipeline.db", "w", force_zip64=True) as db:
                zeros = bytes(2**20)
                for _ in range(200):
                    db.write(zeros)
        return path

    def assert_unchanged(self):
        self.assertEqual(paths.config_file().read_text(encoding="utf-8"), CONFIG)
        self.assertEqual(store.counts()["postings"], 2)
        self.assertEqual(list(self.root.glob("a.bak-*")), [])


class ExportRestoreTest(BackupTestCase):
    def test_export_writes_every_file_and_a_manifest(self):
        path = backup.export(self.out)
        self.assertRegex(path.name, r"^cairn-backup-\d{8}-\d{6}\.zip$")
        with zipfile.ZipFile(path) as bundle:
            self.assertEqual(sorted(bundle.namelist()), [
                "config.toml", "logos/acme.com.png", "manifest.json", "pipeline.db",
                "profile.md"])
            manifest = json.loads(bundle.read("manifest.json"))
        self.assertEqual(manifest["schema"], store.SCHEMA_VERSION)
        self.assertEqual(manifest["counts"]["postings"], 2)
        self.assertEqual(manifest["counts"]["scored"], 1)

    def test_restore_into_a_fresh_home_has_the_same_data(self):
        path = backup.export(self.out)
        before = store.counts()
        self.use_home("b")
        aside = backup.restore(path)
        self.assertEqual(store.counts(), before)
        self.assertEqual(paths.config_file().read_text(encoding="utf-8"), CONFIG)
        self.assertEqual(settings.get().fit_threshold, 72)
        self.assertEqual((paths.home() / "logos" / "acme.com.png").read_bytes(),
                         b"\x89PNG icon")
        self.assertEqual(list(aside.iterdir()), [])

    def test_restore_moves_the_current_files_aside(self):
        path = backup.export(self.out)
        store.upsert_postings([_posting("p3")], "feed")
        paths.config_file().write_text("fit_threshold = 50\n", encoding="utf-8")
        aside = backup.restore(path)
        self.assertEqual(aside.name.split(".bak-")[0], "a")
        self.assertEqual((aside / "config.toml").read_text(encoding="utf-8"),
                         "fit_threshold = 50\n")
        self.assertTrue((aside / "pipeline.db").exists())
        self.assertEqual(store.counts()["postings"], 2)
        self.assertTrue((paths.home() / "run.lock").exists())

    def test_refuses_a_newer_schema_and_changes_nothing(self):
        newer = self.make_zip({"manifest.json": json.dumps({"schema": store.SCHEMA_VERSION + 1}),
                               "pipeline.db": b""})
        with self.assertRaisesRegex(backup.BackupError, "Update Cairn"):
            backup.restore(newer)
        self.assertEqual(paths.config_file().read_text(encoding="utf-8"), CONFIG)
        self.assertEqual(store.counts()["postings"], 2)

    def test_refuses_what_is_no_backup(self):
        shapes = {
            "path outside the home": {"manifest.json": "{}", "pipeline.db": b"",
                                      "../evil": b""},
            "nested icon path": {"manifest.json": "{}", "pipeline.db": b"",
                                 "logos/../../evil": b""},
            "no manifest": {"pipeline.db": b""},
            "manifest without schema": {"manifest.json": "{}", "pipeline.db": b""},
            "unreadable database": {"manifest.json": json.dumps({"schema": 1}),
                                    "pipeline.db": b"garbage"},
        }
        for shape, entries in shapes.items():
            with self.subTest(shape):
                with self.assertRaises(backup.BackupError):
                    backup.restore(self.make_zip(entries))
                self.assertEqual(paths.config_file().read_text(encoding="utf-8"), CONFIG)
        not_zip = self.root / "notes.txt"
        not_zip.write_text("hello", encoding="utf-8")
        with self.assertRaisesRegex(backup.BackupError, "isn't a Cairn backup"):
            backup.restore(not_zip)

    def test_two_backups_in_one_second_both_keep_their_zip(self):
        with mock.patch.object(backup, "_stamp", return_value="20260925-120000"):
            first, second = backup.export(self.out), backup.export(self.out)
        self.assertEqual((first.name, second.name),
                         ("cairn-backup-20260925-120000.zip",
                          "cairn-backup-20260925-120000-2.zip"))
        self.assertEqual({p.name for p in self.out.iterdir()}, {first.name, second.name})

    def test_two_restores_in_one_instant_get_their_own_folder(self):
        path = backup.export(self.out)
        with mock.patch.object(backup, "_stamp", return_value="20260925-120000-000001"):
            first, second = backup.restore(path), backup.restore(path)
        self.assertEqual((first.name, second.name), ("a.bak-20260925-120000-000001",
                                                     "a.bak-20260925-120000-000001-2"))

    @unittest.skipIf(sys.platform == "win32", "Windows files have no Unix permission bits")
    def test_a_backup_zip_is_private_under_any_umask(self):
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        path = backup.export(self.out)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_later_restore_keeps_the_files_an_earlier_one_moved_aside(self):
        path = backup.export(self.out)
        paths.config_file().write_text("fit_threshold = 50\n", encoding="utf-8")
        first = backup.restore(path)
        second = backup.restore(path)
        self.assertEqual(sorted(self.root.glob("a.bak-*")), sorted([first, second]))
        self.assertEqual((first / "config.toml").read_text(encoding="utf-8"),
                         "fit_threshold = 50\n")

    def test_a_backup_that_changes_who_answers_prompts_needs_accepting(self):
        paths.config_file().write_text('llm_provider = "ollama"\n', encoding="utf-8")
        path = backup.export(self.out)
        paths.config_file().write_text(CONFIG, encoding="utf-8")
        settings.reset()
        with self.assertRaisesRegex(backup.ModelChange, "llm_provider 'claude-code' to "
                                                        "'ollama'") as caught:
            backup.restore(path)
        self.assertEqual(caught.exception.changes, [
            {"setting": "llm_provider", "current": "claude-code", "incoming": "ollama"}])
        self.assert_unchanged()
        backup.restore(path, accept_model_change=True)
        self.assertEqual(settings.base().llm_provider, "ollama")

    def test_a_restore_over_an_invalid_config_asks_to_confirm_the_provider(self):
        path = backup.export(self.out)
        paths.config_file().write_text("fit_threshold = \n", encoding="utf-8")
        settings.reset()
        with self.assertRaises(backup.ModelChange) as caught:
            backup.restore(path)
        self.assertEqual([(c["setting"], c["current"]) for c in caught.exception.changes],
                         [(name, None) for name in backup.MODEL_SETTINGS])
        backup.restore(path, accept_model_change=True)
        self.assertEqual(paths.config_file().read_text(encoding="utf-8"), CONFIG)

    @unittest.skipIf(sys.platform == "win32", "Windows files have no Unix permission bits")
    def test_restored_files_are_private(self):
        path = backup.export(self.out)
        self.use_home("b")
        backup.restore(path)
        for name in ("config.toml", "profile.md", "pipeline.db", "logos/acme.com.png"):
            with self.subTest(name):
                self.assertEqual(stat.S_IMODE((paths.home() / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((paths.home() / "logos").stat().st_mode), 0o700)

    def test_refuses_a_zip_bomb_and_changes_nothing(self):
        bomb = self.bomb()
        self.assertLess(bomb.stat().st_size, 300_000)
        with self.assertRaisesRegex(backup.BackupError, "pipeline.db expands to over 100 "
                                                        "times its size"):
            backup.restore(bomb)
        self.assert_unchanged()

    def test_refuses_what_unpacks_past_the_caps(self):
        text = self.make_zip({"manifest.json": "{}", "pipeline.db": b"", "config.toml": "x" * 64})
        with mock.patch.object(backup, "MAX_TEXT_ENTRY", 32), \
                self.assertRaisesRegex(backup.BackupError, "config.toml expands to over"):
            backup.restore(text)
        path = backup.export(self.out)
        with mock.patch.object(backup, "MAX_UNPACKED", 1000), \
                self.assertRaisesRegex(backup.BackupError, "It expands to over"):
            backup.restore(path)
        with mock.patch.object(backup, "MAX_ENTRY", 1000), \
                self.assertRaisesRegex(backup.BackupError, "pipeline.db expands to over"):
            backup.restore(path)
        with mock.patch.object(backup, "MAX_ZIP", 1000), \
                self.assertRaisesRegex(backup.BackupError, "too large to be a Cairn backup"):
            backup.restore(path)
        self.assert_unchanged()

    def test_refuses_a_file_without_the_zip_signature(self):
        disguised = self.root / "backup.zip"
        disguised.write_bytes(b"MZ" + backup.export(self.out).read_bytes()[2:])
        with self.assertRaisesRegex(backup.BackupError, "isn't a Cairn backup"):
            backup.restore(disguised)
        self.assert_unchanged()

    def test_refuses_a_config_this_version_cannot_load(self):
        path = backup.export(self.out)
        with zipfile.ZipFile(path) as bundle:
            entries = {name: bundle.read(name) for name in bundle.namelist()}
        entries["config.toml"] = "fit_threshold = \"high\"\n"
        with self.assertRaisesRegex(backup.BackupError, "settings don't work with this version"):
            backup.restore(self.make_zip(entries))
        self.assert_unchanged()

    def test_a_failed_swap_puts_the_previous_files_back(self):
        path = backup.export(self.out)
        store.upsert_postings([_posting("p3")], "feed")
        real_replace = os.replace

        def failing(src, dst):
            if ".restore-" in str(src) and Path(src).name == "pipeline.db":
                raise OSError(28, "No space left on device")
            real_replace(src, dst)

        with mock.patch.object(backup.os, "replace", failing), \
                self.assertRaisesRegex(backup.BackupError,
                                       "your data is back as it was"):
            backup.restore(path)
        self.assertEqual(paths.config_file().read_text(encoding="utf-8"), CONFIG)
        self.assertEqual(store.counts()["postings"], 3)
        self.assertTrue((paths.home() / "logos" / "acme.com.png").exists())
        self.assertEqual(list(self.root.glob("a.bak-*")), [])

    def test_refuses_while_a_run_holds_the_lock(self):
        path = backup.export(self.out)
        with pipeline.run_lock(), self.assertRaises(pipeline.RunInProgress):
            backup.restore(path)

    def test_export_settings_is_the_config_text(self):
        self.assertEqual(backup.export_settings(), CONFIG)
        paths.config_file().unlink()
        with self.assertRaises(FileNotFoundError):
            backup.export_settings()


class CommandTest(BackupTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        args = cli._parser().parse_args(list(argv))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = args.func(args)
        return code, out.getvalue() + err.getvalue()

    def test_backup_and_restore(self):
        code, output = self.run_cli("backup", "--to", str(self.out))
        self.assertEqual(code, 0)
        (path,) = self.out.iterdir()
        self.assertIn(str(path), output)
        self.use_home("b")
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False):
            code, output = self.run_cli("restore", str(path))
        self.assertEqual((code, output),
                         (2, "ERROR restore replaces your data. Pass --yes to confirm\n"))
        code, output = self.run_cli("restore", str(path), "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(store.counts()["postings"], 2)

    def test_restore_refuses_while_the_app_is_open(self):
        path = backup.export(self.out)
        with backup.serving():
            self.assertTrue(backup.app_running())
            code, output = self.run_cli("restore", str(path), "--yes")
        self.assertEqual(code, 3)
        self.assertIn("so quit it first", output)
        self.assertEqual(list(self.root.glob("a.bak-*")), [])
        self.assertFalse(backup.app_running())
        # Windows keeps the file; the lock alone says a server runs
        self.assertEqual(backup.server_marker().exists(), sys.platform == "win32")

    def test_the_app_server_holds_the_marker_while_it_runs(self):
        with TestClient(create_app(), base_url="http://127.0.0.1"):
            self.assertTrue(backup.app_running())
            self.assertEqual(backup.server_marker().read_text(encoding="utf-8"), str(os.getpid()))
        self.assertFalse(backup.app_running())

    def test_restore_asks_on_a_terminal(self):
        path = backup.export(self.out)
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n"):
            code, _ = self.run_cli("restore", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(list(self.root.glob("a.bak-*")), [])

    def test_settings_export_prints_the_config(self):
        self.assertEqual(self.run_cli("settings", "export"), (0, CONFIG))


class RouteTest(BackupTestCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(system.router)
        self.client = self.enterContext(TestClient(app))

    def test_backup_goes_to_downloads(self):
        with user_home(self.root / "user"):
            body = self.client.post("/api/backup").json()
        path = Path(body["path"])
        self.assertEqual(path.parent, self.root / "user" / "Downloads")
        self.assertEqual(body["bytes"], path.stat().st_size)

    def test_restore_upload(self):
        data = backup.export(self.out).read_bytes()
        self.use_home("b")
        response = self.client.post("/api/restore",
                                    files={"backup": ("b.zip", data, "application/zip")})
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["restart_required"])
        self.assertTrue(Path(response.json()["previous"]).is_dir())
        self.assertEqual(store.counts()["postings"], 2)

    def test_restore_over_an_invalid_config_asks_to_confirm(self):
        data = backup.export(self.out).read_bytes()
        paths.config_file().write_text("fit_threshold = \n", encoding="utf-8")
        settings.reset()
        response = self.client.post("/api/restore", files={"backup": ("b.zip", data)})
        self.assertEqual(response.status_code, 409)
        self.assertEqual({c["current"] for c in response.json()["model_changes"]}, {None})
        response = self.client.post("/api/restore?accept_model_change=true",
                                    files={"backup": ("b.zip", data)})
        self.assertEqual(response.status_code, 202)

    def test_restore_refusals(self):
        response = self.client.post("/api/restore",
                                    files={"backup": ("b.zip", b"not a zip", "application/zip")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("isn't a Cairn backup", response.json()["detail"])
        data = backup.export(self.out).read_bytes()
        with pipeline.run_lock():
            response = self.client.post("/api/restore", files={"backup": ("b.zip", data)})
        self.assertEqual(response.status_code, 409)
        response = self.client.post("/api/restore")
        self.assertEqual((response.status_code, response.json()["detail"]),
                         (400, "upload the backup zip as the form field 'backup'"))

    def test_restore_refuses_a_bomb_and_a_disguised_file(self):
        for data, message in [(self.bomb().read_bytes(), "too large to restore"),
                              (b"%PDF-1.7 not a zip", "isn't a Cairn backup")]:
            with self.subTest(message):
                response = self.client.post("/api/restore",
                                            files={"backup": ("b.zip", data, "application/zip")})
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["detail"])
        self.assert_unchanged()

    def test_restore_refuses_an_upload_over_the_cap(self):
        with mock.patch.object(backup, "MAX_ZIP", 1000):
            response = self.client.post(
                "/api/restore", files={"backup": ("b.zip", b"PK\x03\x04" + bytes(200_000))})
        self.assertEqual(response.status_code, 413)
        self.assert_unchanged()


    def test_settings_export(self):
        response = self.client.get("/api/settings/export")
        self.assertEqual(response.text, CONFIG)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        paths.config_file().unlink()
        self.assertEqual(self.client.get("/api/settings/export").status_code, 404)


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.maintenance = system.Maintenance()

    def test_requests_are_refused_while_a_restore_runs(self):
        self.assertTrue(self.maintenance.enter())
        self.maintenance.leave()
        with self.maintenance.restore(1):
            self.assertFalse(self.maintenance.enter())
        self.assertTrue(self.maintenance.enter())

    def test_a_restore_waits_for_a_background_job(self):
        self.maintenance.hold()
        threading.Timer(0.2, self.maintenance.leave).start()
        started = time.monotonic()
        with self.maintenance.restore(5):
            self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_a_restore_gives_up_on_work_that_does_not_finish(self):
        self.maintenance.hold()
        with self.assertRaises(system.HTTPException) as caught, self.maintenance.restore(0.1):
            pass
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(self.maintenance.restoring)

    def test_a_restore_waits_for_an_icon_fetch(self):
        with temp_home():
            with logos.icon_job(), self.assertRaises(system.HTTPException), \
                    self.maintenance.restore(0.3):
                pass
            with self.maintenance.restore(0.3):
                pass

    def test_a_restore_waits_for_a_link_check(self):
        with insights.LINK_LOCK, self.assertRaises(system.HTTPException), \
                self.maintenance.restore(0.3):
            pass

    def test_a_second_restore_is_refused(self):
        with self.maintenance.restore(1):
            with self.assertRaises(system.HTTPException) as caught, self.maintenance.restore(1):
                pass
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
