"""Alias retrocompatibile: il modulo si chiama ora `app.tools.motor_control` (vocabolario del corpo umano)."""

import warnings

from app.tools.motor_control import *  # noqa: F401,F403
from app.tools.motor_control import execute_tool_safely, force_execute_tool  # noqa: F401
warnings.warn("app.tools.tool_wrapper è deprecato: usa app.tools.motor_control", DeprecationWarning, stacklevel=2)
