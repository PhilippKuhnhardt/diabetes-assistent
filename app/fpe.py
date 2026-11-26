"""Calculated meal surcharge from the estimated fat and protein.

The meal agent estimates a meal's fat and protein in grams. 100 kcal from them (one fat-protein unit, FPE:
9 kcal per gram fat, 4 per gram protein) raise glucose by a configured amount (setting fpe_surcharge_mgdl,
default 20 mg/dL, roughly the Warsaw rule with a typical carb factor), capped at the static surcharge of
a fat/protein meal (80 + 120 mg/dL). The more FPE, the longer it acts: from the meal start until
3 h + 30 min per FPE, at most 8 h. Calibrated on 5 meals,
a first trend only.

For now the user picks per meal whether the dose uses this or the static surcharge (80, +120 with the
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
# Fat/protein levels for an entry without the agent's estimate (2026-11-26: the manual carb entry
# offers Nichts / Normal / Fett instead of the fat mark, computed as FPE). Normal (4 FPE = 80 mg/dL at
# 20 per FPE, over 5 h) and Fett (10 FPE = 200 mg/dL) give the former static surcharge without and with
# the fat mark; Nichts gives none.
LEVELS = {"none": Decimal(0), "normal": Decimal(4), "fat": Decimal(10)}
DEFAULT_LEVEL = "normal"
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


def estimate(values: dict | None, settings: dict) -> dict | None:
    """The calculated surcharge for fat_g and protein_g (an entry payload or the agent's estimate), else for the
    payload's fpe_level (LEVELS): FPE, mg/dL, minutes and the level (None for grams). None without either, or
    while the Automatik does not use the surcharge method, since only that one has a meal surcharge to replace.
    Every caller (dose help, entry form, meal card, agent session) reads it here."""
    if settings.get("auto_method") != "surcharge":
        return None
    values = values or {}
    fpe, level = units(values.get("fat_g"), values.get("protein_g")), None
    if fpe is None and values.get("fpe_level") in LEVELS:
        level = values["fpe_level"]
        fpe = LEVELS[level]
    if fpe is None:
        return None
    return {"fpe": fpe, "mgdl": surcharge_mgdl(fpe, per_fpe_mgdl(settings)).quantize(Decimal(1)), "minutes": minutes(fpe),
            "level": level}


def chosen(found: dict | None, payload: dict) -> bool:
    """Whether the dose and the saved entry use the calculated surcharge: always for a level, for the agent's
    grams when the form picked it (surcharge_mode "fpe")."""
    return found is not None and (found["level"] is not None or payload.get("surcharge_mode") == "fpe")


def level_of(surcharge: dict | None, fat_mark: bool) -> str:
    """The level a saved entry shows: from its stored surcharge's duration ([DAZ], unique per level), else
    from the former fat mark (fat) or the static surcharge (normal)."""
    if surcharge:
        for level, value in LEVELS.items():
            if int(surcharge["minutes"]) == minutes(value):
                return level
    return "fat" if fat_mark else DEFAULT_LEVEL


def plan(found: dict) -> dict:
    """What a meal saved with the calculated surcharge stores ([DAZ:<mgdl>@<minutes>], episodes.surcharge_tag)."""
    return {"mgdl": str(found["mgdl"]), "minutes": found["minutes"]}


def view(found: dict | None, values: dict) -> dict | None:
    """The calculated surcharge as the page shows it next to the static one."""
    return found and {"fpe": str(found["fpe"].quantize(Decimal("0.1"))), "mgdl": str(found["mgdl"]),
                      "minutes": found["minutes"], "fat_g": values.get("fat_g"), "protein_g": values.get("protein_g")}
