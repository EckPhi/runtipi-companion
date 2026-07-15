"""runtipi-companion: a companion CLI for Runtipi."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("runtipi-companion")
except PackageNotFoundError:
    # Running from a source checkout without an installed distribution
    # (e.g. straight off a git clone with no `pip install -e .` yet).
    __version__ = "0.0.0+unknown"
