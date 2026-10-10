# vis-graphify

A Vis extension over the Blockether graphify fork. Only `extension.py` and `presentation.py` mention Vis.

- Keep graph logic in `src/vis_graphify/graph.py` as plain Python over `graph.json`. Do not import graphify there.
- Run graphify itself only as a child process through `graphify_command()`, so a slow build never blocks the host interpreter.
- Pin graphify to a tag of `Blockether/graphify` in `pyproject.toml`. Bump the pin and `uv.lock` together.
- Give every exported method a docstring and an Activity presentation with a capitalized English label. Test the success, failure and empty states.
- Keep `version` in `pyproject.toml` and `VERSION` in `extension.py` equal. A `vX.Y.Z` tag publishes the release.
- Format and lint with ruff. Run tests with `uv run --with pytest pytest tests -q`.
