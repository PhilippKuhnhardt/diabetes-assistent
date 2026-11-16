"""The meal agent's rules (user request 2026-11-16): what it is told, the shape of its answer, and
how an answer is checked before the entry form shows it.

The agent estimates carbohydrates from photos and a description and names the dish. It never names
an insulin dose: its carbs fill the entry form, where the dose help calculates as always. A dish's
history (app/dishes.py) is part of what it is told, so a dish that keeps needing more insulin than
usual is estimated higher next time; that adjustment is named separately (`history_adjustment_g`)."""
from __future__ import annotations

import json

from app import dishes

MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 3_000_000
MAX_TEXT = 2000
MAX_TURNS = 8
# Dishes handed to the model, most used first, each with this many recent entries.
CATALOGUE_DISHES = 60
DISH_EXAMPLES = 3
CERTAINTIES = ("weighed", "known", "unsure")
MEAL_LABELS = {"breakfast": "Frühstück", "lunch": "Mittagessen", "dinner": "Abendessen"}

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "amount": {"type": "string"}, "carbs_g": {"type": "number"}},
            "required": ["name", "amount", "carbs_g"], "additionalProperties": False}},
        "carbs_g": {"type": "number"},
        "carbs_low_g": {"type": "number"},
        "carbs_high_g": {"type": "number"},
        "history_adjustment_g": {"type": "number"},
        "certainty": {"type": "string", "enum": list(CERTAINTIES)},
        "fat_protein": {"type": "boolean"},
        "fast": {"type": "boolean"},
        "dish_name": {"type": "string"},
        "dish_category": {"type": "string"},
        "description": {"type": "string"},
        "reasoning": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["items", "carbs_g", "carbs_low_g", "carbs_high_g", "history_adjustment_g", "certainty",
                 "fat_protein", "fast", "dish_name", "dish_category", "description", "reasoning", "question"],
    "additionalProperties": False,
}

SYSTEM = """Du bist der Essens-Agent in Philipps Diabetes-Tagebuch (Typ 1, Insulin per Pen). Du schätzt die \
Kohlenhydrate (KH) einer Mahlzeit aus Fotos und Beschreibung und ordnest sie einem Gericht aus seinem \
Katalog zu. Deine KH-Schätzung füllt das Eintragsformular; die Insulindosis berechnet danach allein die App. \
Nenne deshalb nie Insulineinheiten, Faktoren oder Dosisratschläge.

So gehst du vor:
- Zerlege das Essen in Bestandteile mit Menge (g, ml oder Stück) und KH in Gramm. Zähle nur verwertbare KH \
(ohne Ballaststoffe), auch aus Getränken, Soßen und Beilagen. Schätze Portionen aus dem Foto an Teller, \
Besteck und Händen. Philipps Angaben (gewogene Mengen, Packungsangaben) gehen vor dem Foto.
- carbs_g ist deine beste Schätzung der Summe, carbs_low_g bis carbs_high_g die ehrliche Spanne, in der die \
Mahlzeit sehr wahrscheinlich liegt. Eine breite Spanne ist besser als falsche Genauigkeit.
- Gerichte: Passt die Mahlzeit zu einem Gericht im Katalog, nimm in dish_name genau dessen Namen, damit sein \
Verlauf zusammenbleibt. Nennt Philipp nur ein bekanntes Gericht ohne Mengen ("Porridge wie immer"), nimm \
dessen übliche KH und Zusammensetzung aus dem Katalog. Sonst schlage einen kurzen neuen Namen vor, der das \
wiederkehrende Gericht benennt (z. B. "Kantine Pasta Bolognese", nicht "Mittagessen vom Dienstag"), und eine \
Kategorie; nutze vorhandene Kategorien, wo sie passen. Ist es kein wiederkehrendes Gericht, bleibt dish_name leer.
- Verlauf: Steht bei einem Gericht, dass es wiederholt deutlich mehr oder weniger Insulin als üblich brauchte \
(ab 3 Mahlzeiten), dann waren seine KH bisher vermutlich unter- oder überschätzt. Bewege carbs_g dann in \
Richtung der KH, wie die es sich verhielt, trage den Unterschied zu deiner Schätzung aus Foto und Beschreibung \
in history_adjustment_g ein (positiv = erhöht, sonst 0) und nenne das in reasoning. Die Bestandteile (items) \
bleiben ohne diese Anpassung.
- certainty: weighed = Philipp hat gewogen oder Packungswerte genannt; known = bekanntes Gericht oder gut \
erkennbare Portion; unsure = grobe Schätzung.
- fat_protein = fett- oder eiweißreich (z. B. Pizza, Käse, Fleisch mit Soße, Frittiertes). fast = wirkt schnell \
(z. B. Saft, Weißbrot, Süßes ohne Fett).
- description: ein bis zwei Sätze, was es war, mit den wichtigsten Mengen, so dass man die Mahlzeit später \
wiedererkennt.
- reasoning: zwei bis vier kurze Sätze auf Deutsch, worauf die Schätzung beruht und was sie unsicher macht.
- question: höchstens eine Rückfrage, nur wenn die Antwort die KH um mehr als etwa 10 g ändern würde, sonst leer. \
Schätze trotzdem immer.
- Websuche (falls verfügbar): nutze sie für Nährwerte von Markenprodukten, Restaurant- oder Kantinengerichten \
und unbekannten Lebensmitteln, nicht für Alltägliches.

Antworte ausschließlich mit einem JSON-Objekt mit genau diesen Feldern: items (Liste aus name, amount, carbs_g), \
carbs_g, carbs_low_g, carbs_high_g, history_adjustment_g, certainty, fat_protein, fast, dish_name, \
dish_category, description, reasoning, question. Kein Text davor oder danach."""


def catalogue_text(catalogue: list[dict], examples: dict[int, list[dict]]) -> str:
    """The dish catalogue as the model reads it: name, category, use, outcome, and recent entries."""
    lines = []
    for dish in [dish for dish in catalogue if not dish.get("archived")][:CATALOGUE_DISHES]:
        line = f"- {dish['name']}" + (f" [{dish['category']}]" if dish.get("category") else "") + f": {dish['hint']}"
        for example in examples.get(dish["id"], [])[-DISH_EXAMPLES:]:
            carbs = f"{example['carbs_g']:g} g" if example.get("carbs_g") else "?"
            line += f"\n    · {example['day']}: {carbs}" + (f", {example['description']}" if example.get("description") else "")
        lines.append(line)
    return "\n".join(lines) if lines else "(noch keine Gerichte)"


def context_text(local_time: str, meal: str | None, place: str | None, catalogue: str) -> str:
    return (f"Zeitpunkt der Mahlzeit: {local_time}" + (f" ({MEAL_LABELS[meal]})" if meal in MEAL_LABELS else "")
            + f"\nOrt: {place or 'nicht angegeben'}\n\nPhilipps Gerichte-Katalog (Verlauf je Gericht, darunter "
            + f"die letzten Einträge):\n{catalogue}")


def _number(value, low: float = 0, high: float = 500) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("Antwort des Modells ist unvollständig") from None
    if number != number:
        raise ValueError("Antwort des Modells ist unvollständig")
    return min(max(number, low), high)


def check(data: dict, catalogue: list[dict]) -> dict:
    """An answer in the form the page shows: whole grams, a range that holds the estimate, a dish
    matched to the catalogue by name, and texts cut to size. Raises ValueError on a broken answer."""
    carbs = round(_number(data.get("carbs_g")))
    low = round(_number(data.get("carbs_low_g", carbs)))
    high = round(_number(data.get("carbs_high_g", carbs)))
    low, high = min(low, carbs), max(high, carbs)
    items = []
    for item in data.get("items") or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            items.append({"name": str(item["name"]).strip()[:80], "amount": str(item.get("amount") or "").strip()[:40],
                          "carbs_g": round(_number(item.get("carbs_g") or 0), 1)})
    name = " ".join(str(data.get("dish_name") or "").split())[:dishes.NAME_MAX]
    known = next((dish for dish in catalogue if dish["name"].casefold() == name.casefold()), None) if name else None
    certainty = data.get("certainty") if data.get("certainty") in CERTAINTIES else "unsure"
    return {
        "items": items[:30], "carbs_g": carbs, "carbs_low_g": low, "carbs_high_g": high,
        "history_adjustment_g": round(_number(data.get("history_adjustment_g") or 0, -200, 200)),
        "certainty": certainty, "fat_protein": data.get("fat_protein") is True, "fast": data.get("fast") is True,
        "dish": ({"id": known["id"] if known else None, "name": known["name"] if known else name, "new": known is None,
                  "category": (known or {}).get("category") or dishes.validate_category(data.get("dish_category")),
                  "hint": (known or {}).get("hint")} if name else None),
        "description": dishes.validate_description(data.get("description")),
        "reasoning": str(data.get("reasoning") or "").strip()[:1500],
        "question": str(data.get("question") or "").strip()[:400],
    }


def answer_text(estimate: dict) -> str:
    """The agent's earlier answer as it is replayed to the model in a follow-up turn."""
    return json.dumps({key: estimate[key] for key in ("items", "carbs_g", "carbs_low_g", "carbs_high_g",
                                                      "history_adjustment_g", "description", "reasoning", "question")}
                      | {"dish_name": (estimate.get("dish") or {}).get("name") or ""}, ensure_ascii=False)
