# Contributing to Cairn

Thanks for helping. This page covers setting up, running the tests, and sending
a change. If any of it is unclear, open an issue and say which part.

## Set up

You need a Mac, [Git](https://git-scm.com), [uv](https://docs.astral.sh/uv/)
and [Node.js](https://nodejs.org). Node is only for the JavaScript tests.

Fork the repository on GitHub with the Fork button at the top of
<https://github.com/druhinb/cairn>, then clone your fork and install Cairn
with its test tools:

```sh
git clone https://github.com/<your-username>/cairn && cd cairn
uv sync --extra test
```

To try the app while you work, give it a folder of its own:

```sh
export CAIRN_HOME=$(mktemp -d)
.venv/bin/cairn init
.venv/bin/cairn serve --port 8766
```

`serve` prints a link. Open it in your browser, and press Ctrl-C in the
terminal to stop. `CAIRN_HOME` tells Cairn where to keep its data, so your
testing never touches your real jobs and notes in `~/.cairn`. Keep the same
`CAIRN_HOME` in each terminal you use. The screens are plain JavaScript files
under `src/cairn/server/static`; reload the page to see a change, with no build
step.

## Run the tests

```sh
.venv/bin/python -m unittest discover -s tests
node --test tests/js/*.mjs
```

Give `node --test` the files as shown. Newer versions of Node fail when you
pass it the folder. Every pull request runs the Python tests on Python 3.11, 3.13
and 3.14, and the JavaScript tests once, so run both before you push.

## What a good change looks like

- Each pull request does one thing. A bug fix and a rename go in separate pull
  requests.
- A bug fix comes with a test that fails without the fix and passes with it.
- New code matches the code around it: the same naming, error handling and
  amount of comments.
- Text that people see in the app uses plain words that someone who doesn't
  write code would follow.
- Ask in an issue before adding a new dependency.

## Commit messages

Write a short subject in lowercase that says what changed, as a command, with
no full stop. Most commits need nothing more than the subject. From `git log`:

```
store and match every spelling of a city as one place
release Cairn 0.0.1
```

Skip vague words like "improve" or "cleanup"; name the thing that changed.

## Issues

Report a bug or ask for a feature at
<https://github.com/druhinb/cairn/issues/new/choose> and pick the form that
fits. For a bug, say what you did, what you expected and what happened
instead, and include your Cairn version (the app shows it in Settings) and
your macOS version. If you found a security problem, follow
[SECURITY.md](SECURITY.md) and keep it out of public issues.

For a large change, open an issue first so you and the maintainer can agree on
the approach before you spend time on it.

## Pull requests

1. Make a branch in your fork, for example `git switch -c fix-city-names`.
2. Commit your change, push the branch, and open a pull request against `main`.
3. Fill in the checklist in the pull request description.

The tests must pass before a pull request can be merged. The maintainer reads
every pull request and may ask for changes.

Everyone who takes part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Licensing your contribution

Cairn is released under the [GNU Affero General Public License v3.0 or
later](LICENSE). By sending a contribution, you agree to license it under the same
terms. You keep the copyright in your work.
