"""Calculated meal surcharge from the estimated fat and protein (user request 2026-11-19, transition phase).

The meal agent estimates a meal's fat and protein in grams. 100 kcal from them (one fat-protein unit, FPE:
9 kcal per gram fat, 4 per gram protein) raise glucose by a configured amount (setting fpe_surcharge_mgdl,
default 20 mg/dL, roughly the Warsaw rule with Philipp's own carb factor), capped at the static surcharge of
a fat/protein meal (80 + 120 mg/dL). The more FPE, the longer it acts: from the meal start until
3 h + 30 min per FPE, at most 8 h. Analysis: project files analysen/fett-eiweiss-2026-11-19.md (5 meals,
a first trend only).

For now Philipp picks per meal whether the dose uses this or the static surcharge (80, +120 with the
fat/protein mark); a meal saved with the calculated one carries [DAZ:<mg/dL>@<minutes>] in its note, so
"Wirkt noch", the second split part and the Automatik's factor history use the same surcharge.
With it the split is "carbs now, surcharge later": the second part is the surcharge's IE.
"""
from __future__ import annotations

from decimal import Decimal

PER_FPE_MGDL = Decimal(20)
MAX_MGDL = Decimal(200)
BASE_MINUTES, MINUTES_PER_FPE, MAX_MINUTES = Decimal(180), Decimal(30), Decimal(480)
# Static and calculated surcharge, as the entry form sends the choice.
MODES = ("static", "fpe")
# With the calculated surcharge a split is offered once the later part (the surcharge) reaches this.
SPLIT_LATER_MIN_UNITS = Decimal(2)


def units(fat_g, protein_g) -> Decimal | None:
    """FPE of a meal, None without an estimate."""
    if fat_g in (None, "") or protein_g in (None, ""):
        return None
    try:
        fat, protein = Decimal(str(fat_g)), Decimal(str(protein_g))
    except ArithmeticError:
        return None
    if not fat.is_finite() or not protein.is_finite() or fat < 0 or protein < 0 or fat + protein > 1000:
        return None
    return (9 * fat + 4 * protein) / 100


def surcharge_mgdl(fpe: Decimal, per_fpe: Decimal = PER_FPE_MGDL) -> Decimal:
    return min(MAX_MGDL, per_fpe * fpe)


def minutes(fpe: Decimal) -> int:
    """How long the surcharge acts, linearly from the meal start."""
    return int(min(MAX_MINUTES, BASE_MINUTES + MINUTES_PER_FPE * fpe))


def per_fpe_mgdl(settings: dict) -> Decimal:
    return Decimal(str(settings.get("fpe_surcharge_mgdl", PER_FPE_MGDL)))


def estimate(payload: dict, settings: dict) -> dict | None:
    """The calculated surcharge for the fat and protein the entry carries: FPE, mg/dL and minutes."""
    fpe = units(payload.get("fat_g"), payload.get("protein_g"))
    if fpe is None:
        return None
    return {"fpe": fpe, "mgdl": surcharge_mgdl(fpe, per_fpe_mgdl(settings)).quantize(Decimal(1)), "minutes": minutes(fpe)}
