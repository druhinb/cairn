"""Settings come from config.toml, and a file the user typed by hand can be wrong.

A bad key or type fails at load. Ignoring a misspelt `fit_treshold` would leave runs
on a threshold nobody chose.
"""
import dataclasses
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from helpers import temp_home

from cairn import cli
from cairn.core import events, paths, settings

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BAD_CONFIG = 'fit_treshold = 70\nlocation_allow = ["Nowhere"]\n'


def _bad_home():
    """A directory holding a config.toml that fails to load."""
    home = tempfile.TemporaryDirectory()
    with open(os.path.join(home.name, "config.toml"), "w", encoding="utf-8") as f:
        f.write(BAD_CONFIG)
    return home


def _configured():
    """A settings object differing from the defaults in every shape save() emits."""
    return dataclasses.replace(
        settings.defaults(),
        graduation_year=2027,
        degrees_held=["Bachelor's", "Associate's"],
        wanted_intern_terms=["fall 2026", "spring 2027"],
        fit_threshold=70,
        notify_macos=True,
        notify_ntfy_topic="a-topic",
        max_rank_per_run=200,
        watchlist=[{"kind": "lever", "location": "figma", "enabled": False}],
    )


class DefaultsTest(unittest.TestCase):
    def test_defaults_carry_no_personal_data(self):
        cfg = settings.defaults()
        self.assertEqual(cfg.notify_ntfy_topic, "")
        self.assertIsNone(cfg.graduation_year)
        self.assertEqual(cfg.degrees_held, [])
        self.assertEqual(cfg.wanted_intern_terms, [])

    def test_company_icons_are_on_unless_turned_off(self):
        self.assertTrue(settings.defaults().company_icons)
        self.assertFalse(settings.from_dict({"company_icons": False}).company_icons)
        with self.assertRaises(settings.SettingsError):
            settings.from_dict({"company_icons": "no"})

    def test_a_missing_file_is_every_default(self):
        home = self.enterContext(temp_home())
        self.assertFalse(paths.config_file().exists())
        self.assertEqual(settings.load(home / "config.toml"), settings.defaults())


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def test_save_then_load_returns_the_same_settings(self):
        original = _configured()
        settings.save(original)
        self.assertEqual(settings.load(), original)

    def test_only_changed_settings_are_written(self):
        settings.save(dataclasses.replace(settings.defaults(), fit_threshold=70))
        text = paths.config_file().read_text(encoding="utf-8")
        self.assertIn("fit_threshold = 70", text)
        self.assertNotIn("tier_floor", text)
        self.assertNotIn("sources", text)

    def test_characters_outside_the_bmp_and_del_survive(self):
        """JSON escapes these in ways TOML rejects; the file must still load."""
        original = dataclasses.replace(
            settings.defaults(), notify_ntfy_topic="jobs \U0001f4bc\x7f",
            location_allow=["Z\u00fcrich \U0001f30d"])
        settings.save(original)
        self.assertEqual(settings.load(), original)


class InvalidFileTest(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())

    def _write(self, text):
        paths.config_file().write_text(text, encoding="utf-8")

    def test_an_unknown_key_names_itself(self):
        self._write("fit_treshold = 70\n")
        with self.assertRaises(settings.SettingsError) as caught:
            settings.load()
        self.assertIn("fit_treshold", str(caught.exception))
        self.assertIn(str(paths.config_file()), str(caught.exception))

    def test_a_wrong_type_is_rejected(self):
        self._write('fit_threshold = "sixty"\n')
        with self.assertRaises(settings.SettingsError):
            settings.load()

    def test_a_number_out_of_range_names_the_key_and_the_range(self):
        for key, value, span in (("fit_threshold", 101, "between 0 and 100"),
                                 ("tier_floor", -1, "between 0 and 100"),
                                 ("recent_days", 0, "between 1 and 365"),
                                 ("rank_batch_size", 101, "between 1 and 100"),
                                 ("rank_retries", 6, "between 0 and 5"),
                                 ("model_concurrency", 0, "between 1 and 8"),
                                 ("model_concurrency", 9, "between 1 and 8"),
                                 ("max_summaries_per_run", 501, "between 0 and 500"),
                                 ("max_rank_per_run", 0, "at least 1")):
            with self.subTest(key), self.assertRaises(settings.SettingsError) as caught:
                settings.from_dict({key: value}, source="request")
            self.assertEqual(str(caught.exception),
                             f"request: '{key}' should be {span}, got {value}")

    def test_the_ends_of_each_range_are_accepted(self):
        cfg = settings.from_dict({"fit_threshold": 0, "tier_floor": 100, "recent_days": 365,
                                  "rank_batch_size": 1, "rank_retries": 5,
                                  "model_concurrency": 8,
                                  "max_summaries_per_run": 0, "max_rank_per_run": None})
        self.assertEqual((cfg.fit_threshold, cfg.recent_days, cfg.max_rank_per_run),
                         (0, 365, None))

    def test_a_value_out_of_range_in_the_file_fails_its_load(self):
        self._write("recent_days = 0\n")
        with self.assertRaises(settings.SettingsError) as caught:
            settings.load()
        self.assertIn("'recent_days' should be between 1 and 365", str(caught.exception))

    def test_a_list_of_the_wrong_element_type_is_rejected(self):
        self._write("degrees_held = [1, 2]\n")
        with self.assertRaises(settings.SettingsError):
            settings.load()

    def test_broken_toml_is_rejected(self):
        self._write("fit_threshold = \n")
        with self.assertRaises(settings.SettingsError):
            settings.load()


class RetiredKeyTest(unittest.TestCase):
    """A config.toml written by an older version still loads."""

    def setUp(self):
        self.enterContext(temp_home())
        self.warnings = []
        self.addCleanup(events.subscribe(
            lambda e: self.warnings.append(e.data["text"]) if e.kind == "warn" else None))

    def test_retired_keys_are_ignored_with_one_warning(self):
        paths.config_file().write_text('fit_threshold = 70\noutput_dir = "~/Desktop/a"\n'
                                       "max_tailor_per_run = 12\n", encoding="utf-8")
        cfg = settings.load()
        self.assertEqual(cfg.fit_threshold, 70)
        self.assertEqual(self.warnings, ["config.toml: ignoring retired setting(s): "
                                         "output_dir, max_tailor_per_run"])

    def test_a_usajobs_key_in_config_toml_is_retired_and_never_a_setting(self):
        paths.config_file().write_text('usajobs_key = "k3y"\nusajobs_email = "me@example.com"\n',
                                       encoding="utf-8")
        cfg = settings.load()
        self.assertEqual(cfg.usajobs_email, "me@example.com")
        self.assertFalse(hasattr(cfg, "usajobs_key"))
        self.assertEqual(self.warnings, ["config.toml: ignoring retired setting(s): usajobs_key"])
        with self.assertRaisesRegex(settings.SettingsError, "unknown setting 'usajobs_key'"):
            settings.from_dict({"usajobs_key": "k3y"})
        settings.save(cfg)
        self.assertNotIn("k3y", paths.config_file().read_text(encoding="utf-8"))

    def test_the_next_save_drops_them_from_disk(self):
        paths.config_file().write_text('fit_threshold = 70\noutput_dir = "~/Desktop/a"\n',
                                       encoding="utf-8")
        settings.save(settings.load())
        text = paths.config_file().read_text(encoding="utf-8")
        self.assertIn("fit_threshold = 70", text)
        self.assertNotIn("output_dir", text)
        self.warnings.clear()
        settings.load()
        self.assertEqual(self.warnings, [])


class ProcessSettingsTest(unittest.TestCase):
    def test_use_replaces_and_reset_reloads(self):
        self.enterContext(temp_home())
        settings.use(dataclasses.replace(settings.get(), fit_threshold=91))
        self.assertEqual(settings.get().fit_threshold, 91)
        settings.reset()
        self.assertEqual(settings.get(), settings.defaults())


class OverrideTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(temp_home())

    def test_an_override_ends_with_its_block(self):
        with settings.override(dataclasses.replace(settings.get(), fit_threshold=95)):
            self.assertEqual(settings.get().fit_threshold, 95)
            self.assertEqual(settings.base().fit_threshold, 60)
        self.assertEqual(settings.get().fit_threshold, 60)

    def test_an_override_in_one_thread_is_invisible_to_another(self):
        inside, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        overridden, elsewhere = [], []

        def hold():
            with settings.override(dataclasses.replace(settings.get(), fit_threshold=95)):
                overridden.append(settings.get().fit_threshold)
                inside.set()
                release.wait(timeout=10)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(inside.wait(5))
        other = threading.Thread(target=lambda: elsewhere.append(settings.get().fit_threshold))
        other.start()
        other.join(5)
        self.assertEqual(settings.get().fit_threshold, 60)
        release.set()
        holder.join(5)
        self.assertEqual((overridden, elsewhere), ([95], [60]))


class SuiteGuardTest(unittest.TestCase):
    """The suite never reads the developer's real config.toml.

    An earlier fixture loaded it to save and restore it, which installed the real
    settings for every later test; one bad key there failed 105 tests.
    """

    def test_a_bad_config_in_the_home_is_never_loaded(self):
        home = _bad_home()
        self.addCleanup(home.cleanup)
        with temp_home():
            os.environ["CAIRN_HOME"] = home.name
            self.assertEqual(settings.get(), settings.defaults())

    def test_tests_without_a_fixture_ignore_a_bad_real_config(self):
        """Run the fixture-free tests with HOME and CAIRN_HOME both bad."""
        home = _bad_home()
        self.addCleanup(home.cleanup)
        os.makedirs(os.path.join(home.name, ".cairn"))
        with open(os.path.join(home.name, ".cairn", "config.toml"), "w", encoding="utf-8") as f:
            f.write(BAD_CONFIG)
        env = {**os.environ, "HOME": home.name, "USERPROFILE": home.name, "CAIRN_HOME": home.name,
               "PYTHONPATH": os.path.join(REPO, "src")}
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests",
             "-k", "RelevanceTest", "-k", "MultiSourceTest", "-k", "ClaudeResultTest"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("OK", proc.stderr)


def _cli(home, *args):
    env = {**os.environ, "CAIRN_HOME": home, "PYTHONPATH": os.path.join(REPO, "src")}
    return subprocess.run([sys.executable, "-m", "cairn.cli", *args], cwd=REPO,
                          env=env, capture_output=True, text=True, timeout=60)


class BadConfigCliTest(unittest.TestCase):
    """A typo in config.toml is a one-line error, and never blocks help or init."""

    def setUp(self):
        self.home = _bad_home()
        self.addCleanup(self.home.cleanup)

    def test_a_command_reports_the_bad_key_and_exits_2(self):
        proc = _cli(self.home.name, "run", "--dry-run")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("fit_treshold", proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_help_still_works(self):
        proc = _cli(self.home.name, "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_init_still_works(self):
        proc = _cli(self.home.name, "init")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.home.name, "profile.md")))


class InitTest(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(temp_home())
        self.args = cli.parser.build_parser().parse_args(["init"])

    def _seeded(self):
        return sorted(p.name for p in self.home.rglob("*") if p.is_file())

    def test_init_seeds_the_home_directory(self):
        self.assertEqual(cli.init.cmd_init(self.args), 0)
        self.assertEqual(self._seeded(), ["config.toml", "profile.md"])
        self.assertEqual(settings.load(), settings.defaults())  # example is all comments

    def test_init_never_overwrites(self):
        cli.init.cmd_init(self.args)
        paths.profile_md().write_text("my own profile", encoding="utf-8")
        self.assertEqual(cli.init.cmd_init(self.args), 0)
        self.assertEqual(paths.profile_md().read_text(encoding="utf-8"), "my own profile")

    def test_init_restores_only_the_missing_files(self):
        cli.init.cmd_init(self.args)
        paths.profile_md().unlink()
        cli.init.cmd_init(self.args)
        self.assertTrue(paths.profile_md().exists())


if __name__ == "__main__":
    unittest.main()
