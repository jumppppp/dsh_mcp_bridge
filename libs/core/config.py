"""Connection configuration and local metadata for the native DSH Web adapter.

Default configuration creates only V2's state directory, never a child Profile
or a copied provider credential environment.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .platform import APP_NAME, locations

# DeepSeek rejects a request when prompt plus reserved completion exceeds its
# window. DSH compacts at a fraction of the window without that reservation, so
# its 256k default completion budget let prompts fail below the threshold.
CONTEXT_WINDOW_TOKENS = 1_048_576
MAX_OUTPUT_TOKENS = 128_000


def context_limit_tokens(provider: str | None) -> int | None:
    if provider == "deepseek-official":
        return CONTEXT_WINDOW_TOKENS - MAX_OUTPUT_TOKENS
    return None


def project_root() -> Path:
    """The implementation root (``libs/``), where dsh_plugin.mjs lives."""
    return Path(__file__).resolve().parent.parent






def _node_executable() -> Path:
    """Node binary used to run the DSH JS entrypoint directly (never a shim)."""
    explicit = os.environ.get("DSH_NODE")
    if explicit and Path(explicit).is_file():
        return Path(explicit).resolve()
    found = shutil.which("node")
    if found:
        return Path(found).resolve()
    # Fall back to the interpreter idiom the installation record records.
    return Path(sys.executable).resolve()


def _package_entry(prefix: Path, package: str, command: str) -> Path | None:
    """Resolve ``node_modules/<package>/<bin>`` the way npm's shim does."""
    root = prefix / "node_modules" / package
    manifest = root / "package.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bin_field = data.get("bin")
    if isinstance(bin_field, str):
        entry = bin_field
    elif isinstance(bin_field, dict):
        entry = bin_field.get(command)
    else:
        return None
    if not entry:
        return None
    candidate = root / entry
    return candidate.resolve() if candidate.is_file() else None


def _js_entry(candidate: str | Path | None) -> str | None:
    """Return the absolute JS entrypoint when ``candidate`` names one."""
    if not candidate:
        return None
    path = Path(candidate)
    if not path.is_file():
        return None
    if path.suffix.lower() in (".js", ".mjs", ".cjs"):
        return str(path.resolve())
    # A bare executable is fine too (native DSH builds); the caller prepends node
    # only for JavaScript entrypoints.
    return str(path.resolve())



def resolve_dsh_cli() -> list[str]:
    """V2 固定 021：显式 DSH_CLI 覆盖，否则用桥接自带的 0.2.1 树。"""
    node = _node_executable()
    entry = _js_entry(os.environ.get("DSH_CLI"))
    if entry:
        return [str(node), entry] if entry.endswith((".js", ".mjs", ".cjs")) else [entry]
    from . import runtime021

    return ["node", str(runtime021.entry())]


def web_profile_patch() -> Path:
    """The USER's web profile patch (providers, agent-default-model).

    The 0.2.1 runtime mode repoints ``DSH_HOME`` at the bridge's own tree, so
    anything that reads user configuration must come here instead.
    """
    home = Path(os.environ.get("DSH_WEB_HOME", "R:/dsh-data"))
    profile = os.environ.get("DSH_WEB_PROFILE", "web")
    return home / "profiles" / profile / "cordis.patch.yml"






_DEFAULT_MODEL_CACHE: dict | None = None


def default_model(refresh: bool = False) -> dict:
    """Provider/model DSH itself uses by default (``agent-default-model``).

    The bridge must not hard-code the upstream project's defaults: on this
    machine DSH points at the ``local`` ai-gateway, so a bridge default of
    ``deepseek-official/deepseek-flash`` would silently bill the official API
    instead of the configured gateway.
    """
    global _DEFAULT_MODEL_CACHE
    if _DEFAULT_MODEL_CACHE is not None and not refresh:
        return _DEFAULT_MODEL_CACHE

    result = {"provider": "deepseek-official", "model": "deepseek-flash"}
    patch = web_profile_patch()
    try:
        lines = patch.read_text(encoding="utf-8").splitlines()
    except OSError:
        _DEFAULT_MODEL_CACHE = result
        return result

    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == "- id: agent-default-model":
            inside = True
            continue
        if inside:
            if stripped.startswith("- id:"):
                break
            if stripped.startswith("provider:"):
                result["provider"] = stripped.split(":", 1)[1].strip().strip("'\"") or result["provider"]
            elif stripped.startswith("model:"):
                result["model"] = stripped.split(":", 1)[1].strip().strip("'\"") or result["model"]
    _DEFAULT_MODEL_CACHE = result
    return result




@dataclass(frozen=True)
class RuntimeConfig:
    database: Path
    cli: list[str]      # [node.exe, dsh bin.js] — no shell scripts
    patch: Path
    legacy_patch: Path
    env: dict[str, str]
    mode: str = "021"    # V2 固定 021（写 v4）


def runtime_config(state: Path | None = None) -> RuntimeConfig:
    """Return Web-only configuration without generating Agent profiles."""
    from .platform import private_directory

    if state is None:
        override = os.environ.get("DSH_SUBAGENT_STATE", "").strip()
        if override:
            state = Path(override)

    state = state or locations().state
    private_directory(state)

    from . import runtime021 as _r021

    # Web-only startup must not create profiles or copy provider credentials.
    patch021 = state / "unused-web-adapter-patch"
    return RuntimeConfig(
        mode="021",
        database=state / "state.sqlite",
        cli=[],
        patch=patch021,
        legacy_patch=patch021,
        env=dict(os.environ),
    )

