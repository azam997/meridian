"""SGE mit-plan healing-GCD Potential-Improvements card.

Now that the ceiling's healing tax is reconciled to the player's ACTUAL healing
(jobs/_core/heal_locks.reconcile_heal_budget), the beyond-plan over-heal card is
job-agnostic: the excess casts and the per-job filler potency both ride the
Scoring state, so a single shared implementation serves every healer. Kept as a
module so the registry (and tests) can import it by its historic path.
"""
from __future__ import annotations

from jobs._core.heal_locks import improvements_from_heal_gcds

__all__ = ["improvements_from_heal_gcds"]
