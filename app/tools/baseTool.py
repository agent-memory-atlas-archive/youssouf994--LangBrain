"""Alias retrocompatibile: il modulo si chiama ora `app.tools.reflex` (vocabolario del corpo umano)."""

import warnings

from app.tools.reflex import *  # noqa: F401,F403
from app.tools.reflex import BaseTool, Reflex  # noqa: F401
warnings.warn("app.tools.baseTool è deprecato: usa app.tools.reflex", DeprecationWarning, stacklevel=2)
