"""Claude Code Telegram Bot.

A Telegram bot that provides remote access to Claude Code CLI, allowing developers
to interact with their projects from anywhere through a secure, terminal-like
interface within Telegram.
"""

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

# Python 3.11+ has tomllib built-in, otherwise use tomli
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

# Read version from pyproject.toml when running from source (always current).
# Fall back to installed package metadata for pip installs without source tree.
_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
try:
    if tomllib:
        with open(_pyproject, "rb") as _f:
            __version__: str = tomllib.load(_f)["project"]["version"]
    else:
        # Fallback if neither tomllib nor tomli is available
        raise ImportError("No TOML library available")
except Exception:
    try:
        __version__ = _pkg_version("claude-code-telegram")
    except PackageNotFoundError:
        __version__ = "0.0.0-dev"

__author__ = "Richard Atkinson"
__email__ = "richardatk01@gmail.com"
__license__ = "MIT"
__homepage__ = "https://github.com/richardatkinson/claude-code-telegram"
