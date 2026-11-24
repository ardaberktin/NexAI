# Contributing to Nex

Read [AGENTS.md](AGENTS.md), the [architecture](docs/architecture.md), and the [assistant contract](docs/assistant-contract.md) before changing assistant behavior. Set up the component you are changing using the [server guide](server/README.md) or the [Apple guide](apple/README.md).

Open an issue for a reproducible bug or a proposed change. Include component versions, the feature or entry point, and steps using synthetic data. For a pull request, explain the behavior change, note any client/server compatibility impact, and include the checks you ran. Private security issues follow the [security policy](SECURITY.md).

## Checks

- From `server/`, install `requirements-test.txt` in a Python 3.12 virtual environment and run `ruff check --isolated --target-version py312 .` and `python -m unittest discover -s tests -v`. Linux is required for the production network and sandbox checks.
- Compile changed Apple schemes with Xcode. Exercise pairing, microphone behavior, Watch relay, and device permissions on physical devices when changes affect those flows.
- For assistant changes, test the normal and fallback paths with paraphrases, context, and tool results. Verify authorization and status handling independently from generated wording.
- Use synthetic data and sanitized logs. Keep local archives, model weights, signed builds, and credentials out of commits.

See [release packaging](docs/releases.md) for the two source archives.
