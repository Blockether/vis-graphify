"""Domain plugins that ship with vis-graphify.

graphify loads each `*.py` file in this directory whose name does not start with `_`.
A plugin file defines `plugin`, `plugins` or `get_plugin()`; see the graphify guide
`docs/domain.md`. The host interpreter never imports these files: graphify runs them
in its own process.
"""
