"""Alias retrocompatibile: gli agenti medici sono un esempio di dominio, ora in `examples.medical_homeostasis`."""

import warnings

from examples.medical_homeostasis.medical_agents import *  # noqa: F401,F403
from examples.medical_homeostasis.medical_agents import CardiovascularOrganAgent, RespiratoryOrganAgent  # noqa: F401
warnings.warn("app.agents.medical_agents è deprecato: usa examples.medical_homeostasis.medical_agents", DeprecationWarning, stacklevel=2)
