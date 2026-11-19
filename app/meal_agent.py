"""The meal agent's rules (user request 2026-11-16): what it is told, the shape of its answer, and
how an answer is checked before the entry form shows it.

The agent estimates carbohydrates from photos and a description and names the dish. It never names
an insulin dose: its carbs fill the entry form, where the dose help calculates as always. A dish's
catalogue is part of what it is told, and it looks up past meals and dish histories with its tools
(Philipp 2026-11-16: the model is smart and gets tools rather than rules). A dish that keeps needing
more insulin than usual is estimated higher next time; that adjustment is named separately
(`history_adjustment_g`)."""
from __future__ import annotations

import json
import re

from app import dishes

MAX_PHOTOS = 4
# Photos per estimate in all: the first ones plus those added when correcting it ("Nachbessern", user
# request 2026-11-19: a photo of the second plate).
MAX_SESSION_PHOTOS = 8
MAX_PHOTO_BYTES = 3_000_000
MAX_TEXT = 2000
MAX_TURNS = 8
# Dishes handed to the model, most used first, each with this many recent entries (more via the tools).
CATALOGUE_DISHES = 60
DISH_EXAMPLES = 1
CERTAINTIES = ("weighed", "known", "unsure")
MEAL_LABELS = {"breakfast": "Frühstück", "lunch": "Mittagessen", "dinner": "Abendessen"}

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "amount": {"type": "string"}, "carbs_g": {"type": "number"},
                           "fat_g": {"type": "number"}, "protein_g": {"type": "number"}},
            "required": ["name", "amount", "carbs_g", "fat_g", "protein_g"], "additionalProperties": False}},
        "carbs_g": {"type": "number"},
        "carbs_low_g": {"type": "number"},
        "carbs_high_g": {"type": "number"},
        "history_adjustment_g": {"type": "number"},
        "fat_g": {"type": "number"},
        "protein_g": {"type": "number"},
        "certainty": {"type": "string", "enum": list(CERTAINTIES)},
        "fat_protein": {"type": "boolean"},
        "fast": {"type": "boolean"},
        "dish_name": {"type": "string"},
        "dish_category": {"type": "string"},
        "description": {"type": "string"},
        "reasoning": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["items", "carbs_g", "carbs_low_g", "carbs_high_g", "history_adjustment_g", "fat_g",
                 "protein_g", "certainty", "fat_protein", "fast", "dish_name", "dish_category", "description", "reasoning", "question"],
    "additionalProperties": False,
}

SYSTEM = """Du bist der Essens-Agent in Philipps Diabetes-Tagebuch (Typ 1, Insulin per Pen). Deine Aufgabe: \
die Kohlenhydrate (KH) einer Mahlzeit so gut wie möglich einschätzen, aus Fotos, Philipps Beschreibung und \
allem, was du nachschlagen kannst. Deine Zahl füllt das Eintragsformular; die Insulindosis rechnet danach \
allein die App. Nenne deshalb nie Insulineinheiten, Faktoren oder Dosisratschläge.

Du entscheidest selbst, wie du vorgehst. Dafür hast du:
- den Gerichte-Katalog in der ersten Nachricht: Philipps wiederkehrende Gerichte mit Anzahl, üblichen KH und \
wie sie sich verhalten haben,
- search_meals: frühere Mahlzeiten nach Stichworten, Mahlzeitenart, Ort oder Gericht durchsuchen, mit KH, \
Beschreibung und Ergebnis (Faktor, Verhältnis zum üblichen Faktor, BZ vorher und nachher),
- dish_history: alle Einträge und Mahlzeiten eines Gerichts,
- die Websuche, falls verfügbar: Nährwerte, Produkte, Restaurant- und Kantinengerichte, Portionsgrößen.
Nutze sie so oft, wie es deine Schätzung besser macht.

Was du über die Daten wissen solltest:
- ratio = Mahlzeitfaktor ÷ üblicher Faktor derselben Mahlzeitenart in den 30 Tagen davor. 1,2 heißt, die \
Mahlzeit brauchte 20 % mehr Insulin als üblich, sie verhielt sich also wie 20 % mehr KH. Liegt ein Gericht \
oder eine ähnliche Mahlzeit wiederholt darüber oder darunter, waren ihre KH vermutlich unter- oder \
überschätzt (Philipps Sicht). Berücksichtige das in carbs_g und trage den Unterschied zu deiner Schätzung \
aus Foto und Beschreibung in history_adjustment_g ein (positiv = erhöht, sonst 0); die items bleiben ohne \
diese Anpassung. Einzelne Mahlzeiten streuen auch aus anderen Gründen (Sport, Stress, Fett).
- Ein Gericht fasst Mahlzeiten zusammen, die sich für den Blutzucker ähnlich verhalten; meist entscheidet \
die Hauptquelle der KH (Reis, Pasta, Brot, Haferflocken), nicht was sonst dabei war. Ähnliche Gerichte und \
Mahlzeiten taugen als Referenz, auch wenn keine genau passt.
- Philipps eigene Angaben (gewogen, Packungswerte) gehen vor dem Foto.
- In späteren Nachrichten korrigiert oder ergänzt Philipp die Mahlzeit, auch nachträglich ("noch ein zweiter \
Teller", "die Hälfte liegen lassen"). Deine Antwort beschreibt dann immer die ganze Mahlzeit, wie sie \
tatsächlich gegessen wurde, mit allen Bestandteilen in items und description. Eine spätere Nachricht kann \
neue Fotos bringen (etwa vom zweiten Teller); sie gehören zur selben Mahlzeit.

Deine Antwort ist ein JSON-Objekt mit genau diesen Feldern:
- items: Bestandteile mit name, amount (Menge), carbs_g (verwertbare KH ohne Ballaststoffe), fat_g (Fett) \
und protein_g (Eiweiß), alles in Gramm,
- carbs_g: deine beste Schätzung; carbs_low_g bis carbs_high_g: die ehrliche Spanne,
- history_adjustment_g: siehe oben,
- fat_g und protein_g: Fett und Eiweiß der ganzen Mahlzeit in Gramm, eine grobe Schätzung reicht. Philipp \
sieht sie nicht; sie werden gespeichert, um später die Wirkung von Fett und Eiweiß auf den BZ zu untersuchen,
- certainty (Philipps Maßstab): weighed = gewogen oder genormt (Packung, feste Portionsangabe); known = alle \
relevanten Zutaten sind bekannt, nur das Gewicht ist geschätzt; unsure = unbekannte Zutaten oder sehr unklare \
Menge,
- fat_protein: nur bei wirklich fett- oder eiweißreichen Mahlzeiten, die den BZ über Stunden nachhaltig \
heben, etwa Pizza, Steak, Burger mit Pommes oder Raclette. Eine Suppe oder ein Gericht mit etwas Fleisch oder \
Öl (z. B. Pho, Curry mit Reis, Pasta mit Soße) ist es nicht (Philipps Maßstab). fast: wirkt schnell (Saft, \
Weißbrot, Süßes ohne Fett),
- dish_name: das passende Gericht aus dem Katalog mit genau seinem Namen, sonst ein kurzer neuer Name für \
etwas Wiederkehrendes, sonst leer; dish_category: eine Kategorie (vorhandene nutzen, wo sie passen),
- description: ein bis zwei Sätze, was es war, mit den wichtigsten Mengen,
- reasoning: zwei bis vier kurze Sätze auf Deutsch, worauf die Schätzung beruht (auch welche früheren \
Mahlzeiten oder Quellen) und was sie unsicher macht; ohne Links, URLs oder Markdown, die App zeigt die \
Quellen der Websuche selbst,
- question: höchstens eine Rückfrage, wenn die Antwort die KH deutlich ändern würde, sonst leer. Schätze \
trotzdem immer.
Kein Text vor oder nach dem JSON."""

MEAL_KINDS = ["breakfast", "lunch", "dinner"]
TOOLS = [
    {"name": "search_meals",
     "description": "Frühere Kohlenhydrat-Einträge durchsuchen, neueste zuerst (höchstens 40). Je Eintrag: Tag, "
                    "Uhrzeit, Mahlzeitenart, Gericht, Beschreibung, KH, Ort, KH-Sicherheit und meal_outcome der "
                    "Hauptmahlzeit, zu der er gehört (factor, reference_factor, ratio, carbs_g und rapid_units "
                    "der ganzen Mahlzeit, BZ g0_mgdl vorher und g_end_mgdl nachher, delta_mgdl).",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Stichworte, die alle in Gericht oder Beschreibung vorkommen "
                                                    "müssen, z. B. \"Reis\". Leer = alle."},
         "meal": {"type": "string", "enum": MEAL_KINDS, "description": "Nur diese Mahlzeitenart."},
         "place": {"type": "string", "description": "Nur dieser Ort (Name wie im Kontext)."},
         "dish": {"type": "string", "description": "Nur dieses Gericht (Name aus dem Katalog)."},
         "days": {"type": "integer", "minimum": 1, "maximum": 730, "description": "Zeitraum in Tagen, Standard 120."},
     }, "additionalProperties": False}},
    {"name": "dish_history",
     "description": "Ein Gericht aus dem Katalog im Detail: Kategorie, Anzahl, übliche KH, Ergebnis (ratio mit "
                    "Spanne, effective_carbs = übliche KH × ratio) und seine letzten Hauptmahlzeiten mit KH, IE, "
                    "Faktor, ratio, BZ vorher und nachher und Ort.",
     "parameters": {"type": "object", "properties": {
         "dish": {"type": "string", "description": "Name des Gerichts."},
         "days": {"type": "integer", "minimum": 1, "maximum": 730, "description": "Zeitraum in Tagen, Standard 120."},
     }, "required": ["dish"], "additionalProperties": False}},
]


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


def context_text(local_time: str, meal: str | None, place: str | None, catalogue: str,
                 places: list[str] | None = None) -> str:
    return (f"Zeitpunkt der Mahlzeit: {local_time}" + (f" ({MEAL_LABELS[meal]})" if meal in MEAL_LABELS else "")
            + f"\nOrt: {place or 'nicht angegeben'}"
            + (f"\nPhilipps Orte: {', '.join(places)}" if places else "")
            + f"\n\nPhilipps Gerichte-Katalog (je Gericht der Verlauf, darunter der letzte Eintrag; mehr über "
            + f"dish_history und search_meals):\n{catalogue}")


def _number(value, low: float = 0, high: float = 500) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("Antwort des Modells ist unvollständig") from None
    if number != number:
        raise ValueError("Antwort des Modells ist unvollständig")
    return min(max(number, low), high)


def _total(value, items: list[dict], key: str) -> int:
    """Grams of fat or protein for the whole meal: the model's total, else the sum of its items."""
    if value is None:
        return round(sum(item[key] for item in items))
    return round(_number(value))


# Links the model writes into its text anyway: "([site](url))", "[site](url)" and bare URLs (OpenAI's search
# adds them with utm_source=openai). The page shows plain text; the search's sources are listed separately.
_LINK_IN_PARENS = re.compile(r"\s*\(\s*\[[^\]]*\]\([^)\s]*\)\s*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)\s]*\)")
_BARE_URL = re.compile(r"\s*\(?https?://[^\s)]+\)?")


def plain_text(text) -> str:
    """The agent's words without links or Markdown links, spaces tidied (user report 2026-11-19)."""
    text = _LINK_IN_PARENS.sub("", str(text or ""))
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"\s+([.,;:])", r"\1", text)).strip()


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
                          "carbs_g": round(_number(item.get("carbs_g") or 0), 1),
                          "fat_g": round(_number(item.get("fat_g") or 0), 1),
                          "protein_g": round(_number(item.get("protein_g") or 0), 1)})
    name = " ".join(str(data.get("dish_name") or "").split())[:dishes.NAME_MAX]
    known = next((dish for dish in catalogue if dish["name"].casefold() == name.casefold()), None) if name else None
    certainty = data.get("certainty") if data.get("certainty") in CERTAINTIES else "unsure"
    return {
        "items": items[:30], "carbs_g": carbs, "carbs_low_g": low, "carbs_high_g": high,
        "history_adjustment_g": round(_number(data.get("history_adjustment_g") or 0, -200, 200)),
        "fat_g": _total(data.get("fat_g"), items, "fat_g"), "protein_g": _total(data.get("protein_g"), items, "protein_g"),
        "certainty": certainty, "fat_protein": data.get("fat_protein") is True, "fast": data.get("fast") is True,
        "dish": ({"id": known["id"] if known else None, "name": known["name"] if known else name, "new": known is None,
                  "category": (known or {}).get("category") or dishes.validate_category(data.get("dish_category")),
                  "hint": (known or {}).get("hint")} if name else None),
        "description": dishes.validate_description(data.get("description")),
        "reasoning": plain_text(data.get("reasoning"))[:1500],
        "question": plain_text(data.get("question"))[:400],
    }


def answer_text(estimate: dict) -> str:
    """The agent's earlier answer as it is replayed to the model in a follow-up turn."""
    return json.dumps({key: estimate[key] for key in ("items", "carbs_g", "carbs_low_g", "carbs_high_g",
                                                      "history_adjustment_g", "fat_g", "protein_g", "description", "reasoning",
                                                      "question") if key in estimate}
                      | {"dish_name": (estimate.get("dish") or {}).get("name") or ""}, ensure_ascii=False)


def lookups(calls: list[dict]) -> list[str]:
    """What the agent looked up, as short German lines for the page (at most 12)."""
    lines = []
    for call in calls:
        args = call.get("args") or {}
        if call.get("name") == "search_meals":
            parts = [f"„{args['query']}“" if args.get("query") else "alle Mahlzeiten"]
            parts += [MEAL_LABELS.get(args.get("meal"), "")] + [args.get("place") or "", args.get("dish") or ""]
            lines.append("Frühere Mahlzeiten: " + ", ".join(part for part in parts if part))
        elif call.get("name") == "dish_history":
            lines.append("Verlauf: " + str(args.get("dish") or ""))
        elif call.get("name") == "web_search":
            lines.append("Websuche" + (f": „{args['query']}“" if args.get("query") else ""))
    return [line[:120] for line in lines][:12]
