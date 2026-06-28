"""partyboxd — PartyBox companion appliance daemon."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__: str = _pkg_version("partyboxd")
except PackageNotFoundError:
    __version__ = "dev"
