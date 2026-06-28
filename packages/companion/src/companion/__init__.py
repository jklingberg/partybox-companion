"""companion — Full appliance package for partybox-companion."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__: str = _pkg_version("partybox-companion")
except PackageNotFoundError:
    __version__ = "dev"
