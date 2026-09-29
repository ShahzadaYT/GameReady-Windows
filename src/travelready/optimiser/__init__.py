"""Game-settings optimisation.

Turns a ROG Ally Life recommendation into a set of proposed changes, decides
which of them TravelReady is allowed to make, and applies only those the user
has explicitly approved.

The package is split so that the read-only half can be proved to write nothing:

``model``        value types — profiles, recommendations, proposed changes
``profiles``     the ROG Ally Life profile store (schema, load, validate, import)
``matcher``      installed game -> profile identification
``configio``     comment/order/encoding-preserving configuration editing
``inspector``    read-only discovery of a game's current settings
``safety``       safety classification and protected-target refusal
``diff``         recommended vs current comparison and plan building
``transaction``  the ONLY component permitted to write

Everything except :mod:`transaction` is read-only, and
``tests/test_readonly.py`` enforces that.
"""

from .model import (  # noqa: F401
    BLOCKED, CAUTION, MANUAL, RESEARCH_REQUIRED, SAFE, SAFETY_CLASSES,
    ChangePlan, GameProfile, ProposedChange, Recommendation, SettingValue,
)
