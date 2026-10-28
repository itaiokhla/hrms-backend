"""Roster service entry point (the G-14 brief's ``roster_service``): publish validation, publication and the generator.

The implementation follows the docs' module split (AS 14 WI-AS-5.3; IG-AS-33): periods, drafts and publication in
``shift_rosters``, the publish validator in ``roster_validation`` over the pure ``roster_rules``, rotations and the
90-day generator in ``shift_rotations``. This module re-exports their public operations under one name.
"""

from app.services.roster_validation import limits_for, period_states, placement_results  # noqa: F401
from app.services.roster_validation import run as validate  # noqa: F401
from app.services.shift_rosters import (  # noqa: F401
    DEFAULT_SCHEDULING_POLICY, REST_RULES, ShiftRosterProvider, create_period, direct_publish, discard_drafts,
    entries_many, grid, my_schedule, publish, put_drafts, reversion_entries, snapshot_hash,
)
from app.services.shift_rotations import (  # noqa: F401
    GENERATE_AHEAD_DAYS, activate_version, assign, create_pattern, generate_ahead, generate_drafts, templates,
)
