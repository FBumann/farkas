"""YAML-based math definition layer for linopy.

Importing this package patches ``linopy.Model`` with ``.from_yaml()`` and
``.yaml``. Use ``from linopy import Model`` directly.
"""

from linopy_yaml._patch import apply_patches
from linopy_yaml.helpers import register
from linopy_yaml.schema import MathSchema

apply_patches()

__all__ = ["MathSchema", "register"]
__version__ = "0.0.2"
