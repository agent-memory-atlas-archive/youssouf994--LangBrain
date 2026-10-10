"""Alias retrocompatibile: il modulo si chiama ora `app.tools.nervous_system` (vocabolario del corpo umano)."""

import warnings

from app.tools.nervous_system import *  # noqa: F401,F403
from app.tools.nervous_system import EventLog, NervousSystem  # noqa: F401
warnings.warn("app.tools.event_log è deprecato: usa app.tools.nervous_system", DeprecationWarning, stacklevel=2)
