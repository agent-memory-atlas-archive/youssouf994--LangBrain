"""Alias retrocompatibile: l'agente clima è un esempio di dominio, ora in `examples.smart_home.climate_agent`."""

import warnings

from examples.smart_home.climate_agent import *  # noqa: F401,F403
from examples.smart_home.climate_agent import AC_TARGET_VALUE, ClimateAgent  # noqa: F401
warnings.warn("app.agents.agent_climate è deprecato: usa examples.smart_home.climate_agent", DeprecationWarning, stacklevel=2)
