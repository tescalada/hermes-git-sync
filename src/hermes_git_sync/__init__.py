"""hermes-git-sync — two-way git sync of HERMES_HOME for Hermes Agent.

Entry point exposed via the `hermes_agent.plugins` group in pyproject.toml.
Hermes' plugin loader imports this package and calls `register(ctx)` once
at startup. `register` wires the session-boundary hooks that do the actual
sync work in `sync.py`.
"""

import logging

from . import sync

logger = logging.getLogger(__name__)

__version__ = "0.0.2"


def register(ctx) -> None:
    """Register session-boundary hooks with Hermes.

    `ctx` is the Hermes PluginContext, providing `register_hook(name, callback)`
    among other methods. We only need the hook surface for this plugin.

    Hermes catches and logs any exception raised here; the plugin will be
    disabled but the agent continues normally.
    """
    ctx.register_hook("on_session_start", sync.on_session_start)
    ctx.register_hook("on_session_end", sync.on_session_end)
    logger.info("hermes-git-sync v%s registered", __version__)
