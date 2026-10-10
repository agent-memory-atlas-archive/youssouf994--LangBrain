"""Alias retrocompatibile: il modulo si chiama ora `app.tools.effectors` (vocabolario del corpo umano)."""

import warnings

from app.tools.effectors import *  # noqa: F401,F403
from app.tools.effectors import (  # noqa: F401
    _TOOL_REGISTRY, IoTDeviceTool, SimulatedEffector, default_effectors, get_default_iot_tools, get_tool, registra_tool,
    tool_registrati, trova_tool,
)
warnings.warn("app.tools.sensor_tools è deprecato: usa app.tools.effectors", DeprecationWarning, stacklevel=2)
