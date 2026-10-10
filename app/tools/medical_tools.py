"""Alias retrocompatibile: i tool medici sono un esempio di dominio, ora in `examples.medical_homeostasis.medical_tools`."""

import warnings

from examples.medical_homeostasis.medical_tools import *  # noqa: F401,F403
from examples.medical_homeostasis.medical_tools import (  # noqa: F401
    HeartRateRegulatorTool, LungVentilatorTool, deterministic_biometric_normalizer,
)
warnings.warn("app.tools.medical_tools è deprecato: usa examples.medical_homeostasis.medical_tools", DeprecationWarning, stacklevel=2)
