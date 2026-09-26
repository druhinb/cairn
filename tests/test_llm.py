"""Model providers over HTTP: request shapes, errors, retries, pacing, keys, routes.

Every test is offline: the opener llm sends through is faked, except the redirect
test, which runs two HTTP servers on 127.0.0.1.
"""
import contextlib
import dataclasses
import datetime
import http.server
import io
import json
import os
import stat
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import temp_home

from cairn import claude, events, llm, paths, secrets, settings
from cairn.server import llm as llm_routes

KEY = "sk-test-0123456789abcdef"

def llm_home(case, **overrides):
    """Enter a temp home whose settings carry the llm fields. Returns the home."""
    case.addCleanup(settings.use, settings.defaults())
    case.enterContext(mock.patch.dict(os.environ))
    for name in [n for n in os.environ if n.startswith("CAIRN_") and n.endswith("_KEY")]:
        del os.environ[name]
    os.environ.pop("ANTHROPIC_API_KEY", None)
    return case.enterContext(temp_home(**overrides))


class Reply(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

def http_error(code, payload, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return urllib.error.HTTPError("https://api.test", code, "error", headers or {},
                                  io.BytesIO(body))


class FakeOpener:
    """Stands in for llm._opener(url): records each request, answers from a script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def body(self, i=-1):
        return json.loads(self.requests[i].data)

    def header(self, name, i=-1):
        return self.requests[i].get_header(name.capitalize())


def openai_reply(text):
    return Reply({"choices": [{"message": {"role": "assistant", "content": text}}]})


class LLMTestCase(unittest.TestCase):
    def setUp(self):
        self.home = llm_home(self)
        self.sleeps = []
        self.enterContext(mock.patch.object(llm.time, "sleep", self.sleeps.append))
        self.enterContext(mock.patch.object(llm, "_next_slot", {}))

    def opener(self, *answers):
        fake = FakeOpener(*answers)
        self.enterContext(mock.patch.object(llm, "_opener", lambda url: fake))
        return fake

    def target(self, provider, key=KEY, **fields):
        return dataclasses.replace(llm.target(provider=provider, key=key), **fields)


class RequestShapeTest(LLMTestCase):
    def test_anthropic_messages_request_and_text_blocks(self):
        fake = self.opener(Reply({"content": [{"type": "text", "text": "Hello "},
                                              {"type": "tool_use", "id": "x"},
                                              {"type": "text", "text": "there"}]}))
        text, err = llm.send(self.target("anthropic"), "hi")
        self.assertEqual((text, err), ("Hello there", None))
        request = fake.requests[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(fake.header("x-api-key"), KEY)
        self.assertEqual(fake.header("anthropic-version"), "2023-06-01")
        self.assertEqual(fake.header("content-type"), "application/json")
        self.assertIsNone(fake.header("authorization"))
        self.assertEqual(fake.body(), {"model": "claude-sonnet-5", "max_tokens": 4096,
                                       "messages": [{"role": "user", "content": "hi"}]})

    def test_openai_compatible_request_and_content(self):
        fake = self.opener(openai_reply("OK"))
        self.assertEqual(llm.send(self.target("groq"), "hi"), ("OK", None))
        self.assertEqual(fake.requests[0].full_url,
                         "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(fake.header("authorization"), f"Bearer {KEY}")
        self.assertTrue(fake.header("user-agent").startswith("cairn/"))
        self.assertEqual(fake.body(), {"model": "llama-3.3-70b-versatile", "temperature": 0,
                                       "messages": [{"role": "user", "content": "hi"}]})
        self.assertIsNone(fake.header("x-title"))

    def test_openrouter_names_the_app(self):
        fake = self.opener(openai_reply("OK"))
        llm.send(self.target("openrouter"), "hi")
        self.assertEqual(fake.header("x-title"), "Cairn")
        self.assertTrue(fake.header("http-referer"))

    def test_ollama_without_a_key_sends_no_authorization(self):
        fake = self.opener(openai_reply("OK"))
        self.assertEqual(llm.send(self.target("ollama", key=""), "hi"), ("OK", None))
        self.assertEqual(fake.requests[0].full_url, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertIsNone(fake.header("authorization"))

    def test_a_reply_without_content_is_an_error(self):
        self.opener(Reply({"choices": []}))
        text, err = llm.send(self.target("groq"), "hi")
        self.assertIsNone(text)
        self.assertEqual(err, "Groq sent an answer Cairn couldn't read. Try again.")

    def test_an_empty_reply_is_an_error(self):
        self.opener(openai_reply("  "))
        self.assertEqual(llm.send(self.target("groq"), "hi"),
                         (None, "Groq sent an empty answer. Try again."))

    def test_a_reply_over_the_cap_is_an_error(self):
        self.opener(Reply(b"x" * (llm.MAX_RESPONSE + 1)))
        text, err = llm.send(self.target("groq"), "hi")
        self.assertIsNone(text)
        self.assertEqual(err, "Cairn couldn't talk to Groq. Check your Groq setup in "
                              "Settings › AI provider and try again.")

    def test_custom_without_a_model_is_an_error_before_any_request(self):
        fake = self.opener()
        target = self.target("custom", base_url="https://llm.test/v1")
        text, err = llm.send(target, "hi")
        self.assertIsNone(text)
        self.assertIn("Custom has no model yet", err)
        self.assertEqual(fake.requests, [])


class ErrorTest(LLMTestCase):
    def test_401_reports_the_message_without_the_key(self):
        fake = self.opener(http_error(401, {"error": {
            "message": f"Incorrect API key provided: {KEY}", "type": "invalid_request_error"}}))
        text, err = llm.send(self.target("groq"), "hi")
        self.assertIsNone(text)
        self.assertEqual(err, "Groq didn't accept the key. Check it and paste it again.")
        self.assertNotIn(KEY, err)
        self.assertEqual(len(fake.requests), 1)

    def test_error_bodies_of_each_shape(self):
        self.opener(http_error(400, [{"error": {"code": 400, "message": "API key not valid"}}]),
                    http_error(401, {"detail": "Invalid API Key"}),
                    http_error(502, b"<html>bad gateway</html>"))
        self.assertEqual(llm.send(self.target("gemini"), "hi", retries=0)[1],
                         "Google Gemini turned down the request: API key not valid")
        self.assertEqual(llm.send(self.target("mistral"), "hi", retries=0)[1],
                         "Mistral didn't accept the key. Check it and paste it again.")
        self.assertEqual(llm.send(self.target("gemini"), "hi", retries=0)[1],
                         "Google Gemini is having trouble right now. Try again later.")

    def test_429_waits_for_retry_after_then_succeeds(self):
        seen = []
        self.addCleanup(events.subscribe(seen.append))
        fake = self.opener(http_error(429, {"error": {"message": "slow down"}},
                                      {"Retry-After": "7"}),
                           openai_reply("OK"))
        self.assertEqual(llm.send(self.target("groq"), "hi"), ("OK", None))
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(self.sleeps, [7.0])
        self.assertIn("[llm] Groq rate limit hit, waiting 7s",
                      [e.data.get("text") for e in seen if e.kind == "warn"])

    def test_retry_after_is_capped_and_5xx_retries_twice_with_backoff(self):
        self.opener(http_error(429, {}, {"Retry-After": "600"}),
                    http_error(503, {}), http_error(503, {"error": {"message": "down"}}))
        self.assertEqual(llm.send(self.target("groq"), "hi"), (None, "Groq is having trouble right now. Try again later."))
        self.assertEqual(self.sleeps, [30, 4])

    def test_an_unreachable_host_is_an_error(self):
        self.opener(urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))
        text, err = llm.send(self.target("ollama", key=""), "hi")
        self.assertIsNone(text)
        self.assertEqual(err, "Couldn't reach Ollama. Open it on this computer and try again.")

    def test_a_retry_after_a_429_takes_a_paced_slot(self):
        self.opener(http_error(429, {}, {"Retry-After": "1"}), openai_reply("OK"))
        with mock.patch.object(llm, "_pace") as pace:
            self.assertEqual(llm.send(self.target("groq"), "hi"), ("OK", None))
        pace.assert_called_once_with("groq")

    def test_a_wait_past_the_deadline_is_not_taken(self):
        fake = self.opener(http_error(429, {"error": {"message": "slow down"}},
                                      {"Retry-After": "30"}))
        self.assertEqual(llm.send(self.target("groq"), "hi", timeout=20),
                         (None, "Groq says Cairn asked too often. Wait a few minutes and try again."))
        self.assertEqual((len(fake.requests), self.sleeps), (1, []))

    def test_the_timeout_is_one_deadline_for_every_attempt(self):
        now = [1000.0]
        self.enterContext(mock.patch.object(llm.time, "monotonic", lambda: now[0]))
        self.enterContext(mock.patch.object(llm.time, "sleep",
                                            lambda s: now.__setitem__(0, now[0] + s)))
        fake = self.opener(http_error(503, {}, {"Retry-After": "8"}), openai_reply("OK"))
        self.assertEqual(llm.send(self.target("groq"), "hi", timeout=20), ("OK", None))
        self.assertEqual(fake.timeouts, [20, 12])

    def test_timeouts_over_120_are_honoured_up_to_300(self):
        fake = self.opener(openai_reply("OK"), openai_reply("OK"))
        llm.send(self.target("groq"), "hi", timeout=240)
        llm.send(self.target("groq"), "hi", timeout=900)
        self.assertTrue(239 < fake.timeouts[0] <= 240, fake.timeouts)
        self.assertTrue(299 < fake.timeouts[1] <= 300, fake.timeouts)

    def test_a_timeout_names_the_whole_budget(self):
        self.opener(urllib.error.URLError(TimeoutError("timed out")))
        self.assertEqual(llm.send(self.target("groq"), "hi", timeout=240),
                         (None, "Groq didn't answer within 240 seconds. Try again."))


class KeyLeakTest(LLMTestCase):
    def test_a_key_with_a_carriage_return_never_reaches_the_error(self):
        # http.client refuses the header and quotes it through repr(), so the raw key
        # never appears in its message; the escaped one does
        for key in ("sk-leak\rsecret", "sk-leak-secret\r", "sk-leak\r\nsecret"):
            with self.subTest(key=key):
                target = llm.Target("custom", "m", "http://127.0.0.1:9/v1", key)
                text, err = llm.send(target, "hi", timeout=5, retries=0)
                self.assertIsNone(text)
                self.assertIn("Check your Custom setup", err)
                self.assertNotIn("sk-leak", err)
                self.assertNotIn("secret", err)

    def test_redact_strips_every_quoted_form(self):
        key = "ab\rcd"
        for text in (f"x {key} y", f"x {key!r} y", "x b'Bearer ab\\rcd' y", "x abcd y"):
            with self.subTest(text=text):
                self.assertNotIn("ab", llm.redact(text, key))
                self.assertNotIn("cd", llm.redact(text, key))


class OpenerTest(unittest.TestCase):
    def proxies(self, url):
        # an opener keeps a ProxyHandler only when it has a proxy to use
        return {scheme: proxy for h in llm._opener(url).handlers
                if isinstance(h, urllib.request.ProxyHandler)
                for scheme, proxy in h.proxies.items()}

    def test_only_https_to_a_remote_host_goes_through_a_proxy(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.test:3128",
                                          "HTTP_PROXY": "http://proxy.test:3128"}):
            self.assertEqual(self.proxies("https://api.groq.com/openai/v1/chat/completions"),
                             {"https": "http://proxy.test:3128"})
            for local in ("http://127.0.0.1:11434/v1", "http://localhost:8080/v1",
                          "http://[::1]:11434/v1"):
                with self.subTest(local):
                    self.assertEqual(self.proxies(local), {})

    def test_no_proxy_in_the_environment_means_none(self):
        with mock.patch.dict(os.environ, clear=True):
            self.assertEqual(self.proxies("https://api.groq.com/openai/v1"), {})


class AnswerRecordTest(LLMTestCase):
    def test_a_successful_call_records_when_the_provider_answered(self):
        self.opener(openai_reply("OK"))
        self.assertIsNone(llm.last_answer("groq"))
        llm.send(self.target("groq"), "hi")
        answered = llm.last_answer("groq")
        self.assertLess(datetime.datetime.now(datetime.UTC) - answered,
                        datetime.timedelta(minutes=1))
        self.assertEqual(llm.check_file(), paths.home() / "llm_check.json")
        self.assertNotIn(KEY, llm.check_file().read_text(encoding="utf-8"))
        self.assertIsNone(llm.last_answer("mistral"))
        self.opener(http_error(401, {"error": {"message": "no"}}))
        llm.send(self.target("mistral"), "hi")
        self.assertIsNone(llm.last_answer("mistral"))
        self.assertFalse(secrets.secrets_file().exists())


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.hits.append((self.path, self.headers.get("Authorization")))
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(302 if self.server.redirect_to else 200)
        if self.server.redirect_to:
            self.send_header("Location", self.server.redirect_to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def local_server(redirect_to=None):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.hits, server.redirect_to = [], redirect_to
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class RedirectTest(LLMTestCase):
    def test_a_redirect_is_never_followed_so_the_key_stays_with_the_base_url(self):
        with local_server() as elsewhere:
            with local_server(f"http://127.0.0.1:{elsewhere.server_port}/steal") as api:
                target = self.target("custom", model="m",
                                     base_url=f"http://127.0.0.1:{api.server_port}/v1")
                text, err = llm.send(target, "hi")
        self.assertIsNone(text)
        self.assertIn("answered from a different address", err)
        self.assertEqual(api.hits, [("/v1/chat/completions", f"Bearer {KEY}")])
        self.assertEqual(elsewhere.hits, [])


class ProviderTableTest(unittest.TestCase):
    def test_every_provider_is_complete(self):
        fields = {"label", "kind", "base_url", "key_url", "models", "steps", "free",
                  "needs_key", "rpm", "notes"}
        for pid, spec in llm.PROVIDERS.items():
            with self.subTest(pid):
                self.assertEqual(set(spec), fields)
                self.assertIn(spec["kind"], ("cli", "anthropic", "openai"))
                self.assertEqual(set(spec["models"]), {"strong", "cheap"})
                if pid != "custom":
                    self.assertTrue(all(spec["models"].values()))
                    self.assertTrue(spec["base_url"] or spec["kind"] == "cli")
                if pid not in ("ollama", "custom"):
                    self.assertTrue(spec["key_url"].startswith("https://"))
                self.assertTrue(3 <= len(spec["steps"]) <= 5)
                self.assertTrue(spec["notes"].endswith("."))
                if spec["free"] and pid != "ollama":
                    self.assertGreater(spec["rpm"], 0)

    def test_every_hosted_base_url_is_https(self):
        for pid, spec in llm.PROVIDERS.items():
            if spec["base_url"] and pid != "ollama":
                self.assertTrue(spec["base_url"].startswith("https://"), pid)


class TargetTest(unittest.TestCase):
    def setUp(self):
        llm_home(self, llm_provider="ollama", llm_model="qwen3:14b",
                 llm_base_url="http://10.0.0.5:11434/v1/")

    def test_only_ollama_and_custom_take_a_base_url(self):
        self.assertEqual(llm.target(provider="groq", base_url="https://evil.example").base_url,
                         "https://api.groq.com/openai/v1")
        self.assertEqual(llm.target(provider="custom", base_url="https://llm.test/v1/").base_url,
                         "https://llm.test/v1")
        settings.use(dataclasses.replace(settings.get(), llm_provider="anthropic",
                                         llm_base_url="https://evil.example"))
        self.assertEqual(llm.target().base_url, "https://api.anthropic.com/v1")

    def test_settings_override_the_active_providers_defaults(self):
        strong, cheap = llm.target("strong"), llm.target("cheap")
        self.assertEqual((strong.model, cheap.model), ("qwen3:14b", "llama3.2:3b"))
        self.assertEqual(strong.base_url, "http://10.0.0.5:11434/v1")

    def test_another_provider_keeps_its_own_defaults(self):
        groq = llm.target(provider="groq")
        self.assertEqual(groq.model, "llama-3.3-70b-versatile")
        self.assertEqual(groq.base_url, "https://api.groq.com/openai/v1")


class PacingTest(LLMTestCase):
    def test_a_free_provider_spaces_calls_by_its_rpm(self):
        now = [1000.0]
        self.enterContext(mock.patch.object(llm.time, "monotonic", lambda: now[0]))
        self.enterContext(mock.patch.object(llm, "send", lambda t, p, timeout, retries: ("OK", None)))
        settings.use(dataclasses.replace(settings.get(), llm_provider="groq"))
        for _ in range(3):
            self.assertEqual(llm.complete("hi", tier="strong", timeout=5), ("OK", None))
        self.assertEqual([round(s, 6) for s in self.sleeps], [2.4, 4.8])
        now[0] += 60
        llm.complete("hi", tier="strong", timeout=5)
        self.assertEqual(len(self.sleeps), 2)

    def test_ollama_is_never_paced(self):
        self.enterContext(mock.patch.object(llm, "send", lambda t, p, timeout, retries: ("OK", None)))
        settings.use(dataclasses.replace(settings.get(), llm_provider="ollama"))
        for _ in range(3):
            llm.complete("hi", tier="strong", timeout=5)
        self.assertEqual(self.sleeps, [])


class ClaudeDispatchTest(LLMTestCase):
    def calls(self, provider, *models):
        seen = []
        self.enterContext(mock.patch.object(
            llm, "complete", lambda prompt, *, tier, timeout: seen.append(tier) or ("OK", None)))
        self.enterContext(mock.patch.object(claude, "run_cli",
                                            lambda *a, **k: seen.append("cli") or ("OK", None)))
        settings.use(dataclasses.replace(settings.get(), llm_provider=provider))
        for model in models:
            self.assertEqual(claude.run("hi", model=model), ("OK", None))
        return seen

    def test_an_explicit_tier_wins_over_the_model(self):
        seen = []
        self.enterContext(mock.patch.object(
            llm, "complete", lambda prompt, *, tier, timeout: seen.append(tier) or ("OK", None)))
        cfg = settings.get()
        settings.use(dataclasses.replace(cfg, llm_provider="groq",
                                         description_model=cfg.claude_model))
        claude.run("hi", model=cfg.claude_model, tier="cheap")
        claude.run("hi", model=cfg.claude_model)
        self.assertEqual(seen, ["cheap", "strong"])

    def test_claude_code_runs_the_cli(self):
        self.assertEqual(self.calls("claude-code", None, "haiku"), ["cli", "cli"])

    def test_other_providers_map_the_model_to_a_tier(self):
        cfg = settings.get()
        self.assertEqual(
            self.calls("groq", None, cfg.claude_model, cfg.description_model,
                       "claude-haiku-4-5", "opus"),
            ["strong", "strong", "cheap", "cheap", "strong"])


class SecretsTest(LLMTestCase):
    @unittest.skipIf(sys.platform == "win32", "Windows files have no Unix permission bits")
    def test_the_file_is_private_and_holds_one_keys_table(self):
        secrets.set_key("groq", f"  {KEY}\n")
        secrets.set_key("claude-code", "other")
        path = secrets.secrets_file()
        self.assertEqual(path, paths.home() / "secrets.toml")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(secrets.get_key("groq"), KEY)
        self.assertTrue(secrets.has_key("claude-code"))
        self.assertFalse(secrets.has_key("mistral"))
        secrets.delete_key("groq")
        self.assertIsNone(secrets.get_key("groq"))
        self.assertEqual(secrets.get_key("claude-code"), "other")
        self.assertNotIn(KEY, path.read_text(encoding="utf-8"))
        self.assertFalse((paths.home() / "config.toml").exists())

    @unittest.skipIf(sys.platform == "win32", "Windows files have no Unix permission bits")
    def test_a_loosened_file_is_made_private_on_the_next_write(self):
        secrets.set_key("groq", KEY)
        secrets.secrets_file().chmod(0o644)
        secrets.set_key("mistral", KEY)
        self.assertEqual(stat.S_IMODE(secrets.secrets_file().stat().st_mode), 0o600)

    def test_the_environment_wins_over_the_file(self):
        secrets.set_key("groq", "from-file")
        secrets.set_key("anthropic", "from-file")
        with mock.patch.dict(os.environ, {"CAIRN_GROQ_KEY": "from-env",
                                          "ANTHROPIC_API_KEY": "anthropic-env"}):
            self.assertEqual(secrets.get_key("groq"), "from-env")
            self.assertEqual(secrets.get_key("anthropic"), "anthropic-env")
        with mock.patch.dict(os.environ, {"CAIRN_CLAUDE_CODE_KEY": "cc"}):
            self.assertEqual(secrets.get_key("claude-code"), "cc")

    def test_a_bad_key_in_the_environment_is_ignored_with_one_warning(self):
        warnings = []
        self.addCleanup(events.subscribe(
            lambda e: e.kind == "warn" and warnings.append(e.data["text"])))
        self.enterContext(mock.patch.object(secrets, "_warned", set()))
        secrets.set_key("groq", "from-file")
        with mock.patch.dict(os.environ, {"CAIRN_GROQ_KEY": "sk-live\rhidden"}):
            self.assertIsNone(secrets.get_key("groq"))
            self.assertFalse(secrets.has_key("groq"))
        self.assertEqual(warnings, ["ignoring the groq key in $CAIRN_GROQ_KEY: the key "
                                    "should be printable ASCII with no spaces"])
        with mock.patch.dict(os.environ, {"CAIRN_GROQ_KEY": " sk-live-ok\r\n"}):
            self.assertEqual(secrets.get_key("groq"), "sk-live-ok")

    def test_a_bad_key_written_into_the_file_by_hand_is_ignored(self):
        self.enterContext(mock.patch.object(secrets, "_warned", set()))
        secrets.secrets_file().write_text('[keys]\ngroq = "two words"\n', encoding="utf-8")
        self.assertIsNone(secrets.get_key("groq"))

    def test_usajobs_is_a_key_name_and_unknown_names_are_refused(self):
        secrets.set_key("usajobs", KEY)
        self.assertEqual(secrets.get_key("usajobs"), KEY)
        with mock.patch.dict(os.environ, {"CAIRN_USAJOBS_KEY": "from-env"}):
            self.assertEqual(secrets.get_key("usajobs"), "from-env")
        for call in (secrets.get_key, secrets.has_key, secrets.delete_key,
                     lambda name: secrets.set_key(name, KEY)):
            with self.subTest(call), self.assertRaisesRegex(secrets.SecretsError, "no key named"):
                call("gpt")

    @unittest.skipIf(sys.platform == "win32", "Windows files have no Unix permission bits")
    def test_a_stale_staging_file_never_lends_its_mode(self):
        staging = secrets.secrets_file().with_name("secrets.toml.tmp")
        staging.write_text("left over", encoding="utf-8")
        staging.chmod(0o644)
        secrets.set_key("groq", KEY)
        self.assertEqual(stat.S_IMODE(secrets.secrets_file().stat().st_mode), 0o600)
        self.assertFalse(staging.exists())

    def test_a_key_with_spaces_or_nothing_is_refused(self):
        for bad in ("", "   ", "two words", "k\x00y", None):
            with self.subTest(bad), self.assertRaises(secrets.SecretsError):
                secrets.set_key("groq", bad)
        self.assertFalse(secrets.secrets_file().exists())

    def test_a_broken_file_is_an_error(self):
        secrets.secrets_file().write_text("[keys\n", encoding="utf-8")
        with self.assertRaises(secrets.SecretsError):
            secrets.get_key("groq")


class RouterTest(LLMTestCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(llm_routes.router)
        self.client = self.enterContext(TestClient(app))

    def call(self, method, url, status=200, **kwargs):
        response = self.client.request(method, url, **kwargs)
        self.assertEqual(response.status_code, status, response.text)
        self.assertNotIn(KEY, response.text)
        return response.json()

    def test_providers_list_every_provider_with_configured_and_active(self):
        secrets.set_key("groq", KEY)
        with mock.patch.object(llm_routes.shutil, "which", return_value=None):
            body = self.call("GET", "/api/llm/providers")
        self.assertEqual(body["active"], "claude-code")
        configured = {p["id"]: p["configured"] for p in body["providers"]}
        self.assertEqual(list(configured), list(llm.PROVIDERS))
        self.assertEqual(configured, {"claude-code": False, "anthropic": False,
                                      "gemini": False, "groq": True, "mistral": False,
                                      "openrouter": False, "ollama": True, "custom": False})

    def test_put_saves_settings_and_the_key_apart(self):
        body = self.call("PUT", "/api/llm", json={"provider": "groq", "model": "llama-x",
                                                  "key": KEY})
        self.assertEqual(body, {"provider": "groq", "model": "llama-x", "model_cheap": "",
                                "base_url": "", "has_key": True})
        config = paths.config_file().read_text(encoding="utf-8")
        self.assertIn('llm_provider = "groq"', config)
        self.assertNotIn(KEY, config)
        self.assertEqual(secrets.get_key("groq"), KEY)
        self.assertEqual(self.call("GET", "/api/llm"), body)

        self.call("PUT", "/api/llm", json={"provider": "groq", "key": ""})
        self.assertEqual(secrets.get_key("groq"), KEY)
        self.assertFalse(self.call("PUT", "/api/llm",
                                   json={"provider": "groq", "key": None})["has_key"])
        self.assertIsNone(secrets.get_key("groq"))

    def test_put_rejects_bad_input_and_saves_nothing(self):
        for payload, message in [
                ({"provider": "gpt"}, "provider: should be one of"),
                ({"provider": "ollama", "base_url": "ftp://x"}, "should start with http:// or https://"),
                ({"provider": "custom"}, "Custom needs an address"),
                ({"provider": "groq", "key": "two words"}, "no spaces"),
                ({"provider": "groq", "colour": "red"}, "unknown field(s): colour")]:
            with self.subTest(payload):
                response = self.client.put("/api/llm", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["detail"])
        self.assertFalse(paths.config_file().exists())
        self.assertFalse(secrets.secrets_file().exists())

    def test_test_call_uses_the_given_values_without_saving_them(self):
        fake = self.opener(openai_reply("OK"))
        body = self.call("POST", "/api/llm/test",
                         json={"provider": "mistral", "key": KEY, "model": "tiny"})
        self.assertTrue(body["ok"])
        self.assertEqual((body["reply"], body["error"]), ("OK", None))
        self.assertIsInstance(body["latency_ms"], int)
        self.assertEqual(fake.body()["model"], "tiny")
        self.assertEqual(fake.body()["messages"][0]["content"], llm.TEST_PROMPT)
        self.assertEqual(fake.header("authorization"), f"Bearer {KEY}")
        self.assertFalse(secrets.secrets_file().exists())
        self.assertEqual(llm.active(), "claude-code")

    def test_a_failed_test_call_hides_the_key(self):
        fake = self.opener(http_error(401, {"error": {"message": f"bad key {KEY}"}}))
        body = self.call("POST", "/api/llm/test", json={"provider": "groq", "key": KEY})
        self.assertFalse(body["ok"])
        self.assertIn("didn't accept the key", body["error"])
        self.assertEqual(len(fake.requests), 1)

    def test_test_call_for_claude_code_runs_the_cli(self):
        with mock.patch.object(claude, "run_cli", return_value=("OK", None)) as run_cli:
            body = self.call("POST", "/api/llm/test", json={})
        self.assertTrue(body["ok"])
        run_cli.assert_called_once_with(llm.TEST_PROMPT, None, llm_routes.TEST_TIMEOUT)

    def test_a_fixed_provider_refuses_a_base_url(self):
        secrets.set_key("anthropic", KEY)
        fake = self.opener()
        for method, url, body in [
                ("PUT", "/api/llm", {"provider": "anthropic", "base_url": "https://evil.example"}),
                ("POST", "/api/llm/test", {"provider": "anthropic",
                                           "base_url": "https://evil.example"})]:
            with self.subTest(url):
                response = self.client.request(method, url, json=body)
                self.assertEqual((response.status_code, response.json()["detail"]),
                                 (400, "base_url: only ollama and custom take one"))
        self.assertEqual(fake.requests, [])
        self.assertFalse(paths.config_file().exists())

    def test_the_stored_key_goes_only_to_the_saved_base_url(self):
        self.call("PUT", "/api/llm", json={"provider": "custom", "model": "m",
                                           "base_url": "https://llm.test/v1", "key": KEY})
        fake = self.opener(openai_reply("OK"), openai_reply("OK"), openai_reply("OK"),
                           openai_reply("OK"))
        self.call("POST", "/api/llm/test",
                  json={"provider": "custom", "base_url": "https://elsewhere.test/v1"})
        self.assertEqual(fake.requests[-1].full_url, "https://elsewhere.test/v1/chat/completions")
        self.assertIsNone(fake.header("authorization"))
        self.call("POST", "/api/llm/test",
                  json={"provider": "custom", "base_url": "https://elsewhere.test/v1",
                        "key": "sk-given"})
        self.assertEqual(fake.header("authorization"), "Bearer sk-given")
        self.call("POST", "/api/llm/test",
                  json={"provider": "custom", "base_url": "https://llm.test/v1/"})
        self.assertEqual(fake.header("authorization"), f"Bearer {KEY}")
        self.call("POST", "/api/llm/test", json={"provider": "custom"})
        self.assertEqual(fake.requests[-1].full_url, "https://llm.test/v1/chat/completions")
        self.assertEqual(fake.header("authorization"), f"Bearer {KEY}")

    def test_delete_removes_the_active_providers_key(self):
        self.call("PUT", "/api/llm", json={"provider": "gemini", "key": KEY})
        self.assertEqual(self.call("DELETE", "/api/llm/key"),
                         {"provider": "gemini", "has_key": False})
        self.assertFalse(secrets.has_key("gemini"))


if __name__ == "__main__":
    unittest.main()
