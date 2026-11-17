"""Central configuration for the projection pipeline.

Every tunable number in the model lives here so it can be adjusted without
digging through the code.
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SIGMA_DATA_DIR", ROOT / "data"))
HISTORY_DIR = DATA_DIR / "history"
STATE_DIR = DATA_DIR / "state"
SLATES_DIR = DATA_DIR / "slates"

ET = ZoneInfo("America/New_York")

# ── DraftKings classic NBA scoring ──────────────────────────────────────────
DK_SCORING = {
    "pts": 1.0,
    "fg3m": 0.5,
    "reb": 1.25,
    "ast": 1.5,
    "stl": 2.0,
    "blk": 2.0,
    "tov": -0.5,
}
DK_DOUBLE_DOUBLE = 1.5
DK_TRIPLE_DOUBLE = 3.0
DK_SALARY_CAP = 50_000
DK_ROSTER_SLOTS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]
DK_SLOT_OK = {
    "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"}, "C": {"C"},
    "G": {"PG", "SG"}, "F": {"SF", "PF"}, "UTIL": {"PG", "SG", "SF", "PF", "C"},
}

# ── model parameters ────────────────────────────────────────────────────────
MODEL = {
    # minutes: exponential decay half-life measured in games
    "minutes_half_life": 4.0,
    "minutes_lookback": 15,
    # prior-season minutes count like this many current-season games
    "minutes_prior_games": 4.0,
    # fantasy-points-per-minute: slower decay, Bayesian shrink to prior
    "rate_half_life": 8.0,
    "rate_lookback": 25,
    "rate_prior_minutes": 250.0,
    # team minutes to fill (5 x 48, plus a little for overtime)
    "team_minutes": 241.0,
    "max_player_minutes": 40.0,
    # blowout: starters lose this fraction of minutes per point of spread beyond 6
    "blowout_per_point": 0.006,
    "blowout_free_points": 6.0,
    "back_to_back_mult": 0.97,
    # returning from an absence of N+ straight team games -> minutes restriction
    "return_absence_games": 5,
    "return_minutes_mult": 0.85,
    # injury report mentions reconditioning / a minutes restriction
    "restricted_minutes_mult": 0.75,
    # usage bump when rotation teammates are out
    "usage_bump_beta": 0.30,
    "usage_bump_cap": 0.20,
    # game environment from Vegas implied total (elasticity of DK FP to team points)
    "vegas_elasticity": 0.80,
    # opponent defense factor shrink (games of evidence needed for half weight)
    "opp_shrink_games": 10.0,
    "dvp_shrink_games": 20.0,
    "dvp_strength": 0.5,
    "home_edge": 0.01,
    # distribution
    "cv_prior": 0.32,
    "cv_shrink_games": 8.0,
    "cv_min": 0.18,
    "cv_max": 0.60,
    "boom_multiple": 6.0,   # boom = scores >= 6x salary/1000
    "bust_multiple": 4.0,   # bust = scores <  4x salary/1000
    # players with no NBA history: projection implied by salary
    "rookie_fp_per_k": 4.0,
    # how far a projection moves toward the sharp-book player props (0 = ignore, 1 = market only)
    "market_weight": 0.7,
    # optimal-lineup simulation used for opt% / leverage
    "opt_sims": 1500,
}

# injury statuses, ordered from most to least severe
STATUS_ORDER = ["OUT", "DOUBTFUL", "QUESTIONABLE", "PROBABLE", "AVAILABLE"]
# statuses treated as "will not play" when projecting
NOT_PLAYING = {"OUT", "DOUBTFUL"}
STATUS_SHORT = {
    "OUT": "O", "DOUBTFUL": "D", "QUESTIONABLE": "Q", "PROBABLE": "P",
    "AVAILABLE": "", "": "",
}

HTTP_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ── contest simulation (pipeline/sim.py) ────────────────────────────────────
SIM = {
    "lineups": 10_000,          # lineups built for every slate
    "sims": 1_000_000,          # simulated contests the lineups play
    "field_lineups": 10_000,    # simulated opponents drafted from projected ownership
    "field_min_salary": 48_500,
    "min_proj": 4.0,            # players projected under this are left out of the pool
    "generate_time_limit": 240,
    # chance a player actually plays, by injury status (OUT/DOUBTFUL are projected 0)
    "play_prob": {"QUESTIONABLE": 0.70, "PROBABLE": 0.97, "": 1.0},
    # used until the box-score history is large enough to measure them
    "default_corr": {"team": 0.02, "opp": 0.05},
}
