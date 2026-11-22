"""DSH MCP Bridge implementation package.

Layout
------
  libs/core/        bridge internals (platform, config, runtime, manager, projection)
  libs/transports/  MCP transports + DSH Web bridge (server, httpd, external)
  libs/ui/          control console, prompt generator, doctor

Entry points: ``main.py`` (unified) and ``python -m libs``.
"""

from .core.platform import APP_NAME, APP_VERSION, locations, temp_workspace
from .core.config import RuntimeConfig, resolve_dsh_cli, runtime_config, default_model
from .core.manager import Manager, PARENT_TITLE, settled_status, title_from_task

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "locations",
    "temp_workspace",
    "RuntimeConfig",
    "resolve_dsh_cli",
    "runtime_config",
    "default_model",
    "Manager",
    "PARENT_TITLE",
    "settled_status",
    "title_from_task",
]
