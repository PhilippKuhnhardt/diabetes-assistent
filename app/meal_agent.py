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
from app.i18n import pick, tr

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
MEAL_LABELS = {"breakfast": tr("Frühstück"), "lunch": tr("Mittagessen"), "dinner": tr("Abendessen")}

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

SYSTEM = pick(
"""Du bist der Essens-Agent in Philipps Diabetes-Tagebuch (Typ 1, Insulin per Pen). Deine Aufgabe: \
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
- Philipps eigene Angaben (gewogen, Packungswerte) gehen vor dem Foto. Halte sie so fest, dass sie beim \
nächsten Mal wieder auffindbar sind: Produkt und Marke im item-Namen, im amount Stückzahl, Gewicht je Stück und \
die Packungsangabe (z. B. "2 Stück à 23 g, 54 g KH/100 g"), auch in der description.
- Isst Philipp etwas, das er schon einmal angegeben hat (gleiches Produkt, "wie neulich", "die Waffel von \
eben"), suche es mit search_meals: jeder Treffer zeigt unter agent die damaligen items und Philipps eigene \
Worte (user_said), etwa Nährwerte je 100 g und Gewicht je Stück. Rechne damit statt neu zu schätzen. Ein \
Treffer zeigt carbs_g für den ganzen Eintrag, oft mit anderem Essen zusammen; nimm den Anteil des Produkts aus \
den items. Was schon in einem früheren Eintrag gezählt ist, zählt nicht noch einmal.
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
Kein Text vor oder nach dem JSON.""",
"""You are the meal agent in the user's diabetes diary (type 1, insulin by pen). Your task: \
estimate the carbohydrates (carbs) of a meal as well as you can, from photos, the user's description and \
everything you can look up. Your number fills the entry form; the app alone calculates the insulin dose \
afterwards. So never name insulin units, factors or dosing advice.

You decide how to proceed. For this you have:
- the dish catalogue in the first message: the user's recurring dishes with count, usual carbs and \
how they behaved,
- search_meals: search earlier meals by keywords, meal kind, place or dish, with carbs, \
description and outcome (factor, ratio to the usual factor, BG before and after),
- dish_history: all entries and meals of one dish,
- web search, if available: nutrition facts, products, restaurant and canteen dishes, portion sizes.
Use them as often as it makes your estimate better.

What you should know about the data:
- ratio = meal factor ÷ usual factor of the same meal kind in the 30 days before. 1.2 means the \
meal needed 20 % more insulin than usual, so it behaved like 20 % more carbs. If a dish \
or a similar meal is repeatedly above or below, its carbs were probably under- or \
overestimated (the user's view). Take this into account in carbs_g and enter the difference to your estimate \
from photo and description in history_adjustment_g (positive = raised, otherwise 0); the items stay without \
this adjustment. Single meals also scatter for other reasons (exercise, stress, fat).
- A dish groups meals that behave alike for blood glucose; usually the main source \
of carbs decides (rice, pasta, bread, oats), not what else came with it. Similar dishes and \
meals serve as a reference even if none matches exactly.
- The user's own figures (weighed, package values) take precedence over the photo. Record them so that they can be \
found again next time: product and brand in the item name, in amount the number of pieces, weight per piece and \
the package figure (e.g. "2 pieces of 23 g, 54 g carbs/100 g"), also in the description.
- If the user eats something they have stated before (same product, "like the other day", "the waffle from \
earlier"), look it up with search_meals: each hit shows under agent the items back then and the user's own \
words (user_said), such as nutrition per 100 g and weight per piece. Calculate with these instead of estimating anew. A \
hit shows carbs_g for the whole entry, often together with other food; take the product's share from \
the items. What is already counted in an earlier entry does not count again.
- In later messages the user corrects or adds to the meal, also afterwards ("a second \
plate", "left half of it"). Your answer then always describes the whole meal as it \
was actually eaten, with all components in items and description. A later message can \
bring new photos (say of the second plate); they belong to the same meal.

Your answer is a JSON object with exactly these fields:
- items: components with name, amount, carbs_g (available carbs without fibre), fat_g (fat) \
and protein_g (protein), all in grams,
- carbs_g: your best estimate; carbs_low_g to carbs_high_g: the honest range,
- history_adjustment_g: see above,
- fat_g and protein_g: fat and protein of the whole meal in grams, a rough estimate is enough. The user \
does not see them; they are stored to study the effect of fat and protein on BG later,
- certainty (the user's scale): weighed = weighed or standardised (package, fixed portion); known = all \
relevant ingredients are known, only the weight is estimated; unsure = unknown ingredients or a very unclear \
amount,
- fat_protein: only for really fat- or protein-rich meals that raise BG for hours, \
such as pizza, steak, burger with fries or raclette. A soup or a dish with some meat or \
oil (e.g. pho, curry with rice, pasta with sauce) is not (the user's scale). fast: acts fast (juice, \
white bread, sweets without fat),
- dish_name: the matching dish from the catalogue with exactly its name, otherwise a short new name for \
something recurring, otherwise empty; dish_category: a category (use existing ones where they fit),
- description: one or two sentences on what it was, with the main amounts,
- reasoning: two to four short sentences in English on what the estimate is based on (also which earlier \
meals or sources) and what makes it uncertain; without links, URLs or Markdown, the app shows the \
sources of the web search itself,
- question: at most one follow-up question if the answer would change the carbs considerably, otherwise empty. \
Estimate anyway, always.
No text before or after the JSON.""")

MEAL_KINDS = ["breakfast", "lunch", "dinner"]
TOOLS = [
    {"name": "search_meals",
     "description": pick(
         "Frühere Kohlenhydrat-Einträge durchsuchen, neueste zuerst (höchstens 40). Je Eintrag: Tag, "
         "Uhrzeit, Mahlzeitenart, Gericht, Beschreibung, KH, Ort, KH-Sicherheit, agent (die damaligen items "
         "mit Menge und KH und user_said, Philipps eigene Worte zur Einschätzung, etwa Nährwerte und "
         "Gewichte) und meal_outcome der Hauptmahlzeit, zu der er gehört (factor, reference_factor, ratio, carbs_g und rapid_units "
         "der ganzen Mahlzeit, BZ g0_mgdl vorher und g_end_mgdl nachher, delta_mgdl).",
         "Search earlier carb entries, newest first (at most 40). Per entry: day, time, meal kind, dish, "
         "description, carbs, place, carb certainty, agent (the items back then with amount and carbs, and "
         "user_said, the user's own words on the estimate, such as nutrition facts and weights) and meal_outcome "
         "of the main meal it belongs to (factor, reference_factor, ratio, carbs_g and rapid_units of the whole meal, "
         "BG g0_mgdl before and g_end_mgdl after, delta_mgdl)."),
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": pick(
             "Stichworte, die alle in Gericht, Beschreibung, items oder Philipps Worten vorkommen müssen, z. B. \"Reis\". Leer = alle.",
             "Keywords that must all appear in dish, description, items or the user's words, e.g. \"rice\". Empty = all.")},
         "meal": {"type": "string", "enum": MEAL_KINDS, "description": tr("Nur diese Mahlzeitenart.")},
         "place": {"type": "string", "description": tr("Nur dieser Ort (Name wie im Kontext).")},
         "dish": {"type": "string", "description": tr("Nur dieses Gericht (Name aus dem Katalog).")},
         "days": {"type": "integer", "minimum": 1, "maximum": 730, "description": tr("Zeitraum in Tagen, Standard 120.")},
     }, "additionalProperties": False}},
    {"name": "dish_history",
     "description": pick(
         "Ein Gericht aus dem Katalog im Detail: Kategorie, Anzahl, übliche KH, Ergebnis (ratio mit "
         "Spanne, effective_carbs = übliche KH × ratio) und seine letzten Hauptmahlzeiten mit KH, IE, "
         "Faktor, ratio, BZ vorher und nachher und Ort.",
         "One dish from the catalogue in detail: category, count, usual carbs, outcome (ratio with range, "
         "effective_carbs = usual carbs × ratio) and its latest main meals with carbs, units, factor, ratio, "
         "BG before and after, and place."),
     "parameters": {"type": "object", "properties": {
         "dish": {"type": "string", "description": tr("Name des Gerichts.")},
         "days": {"type": "integer", "minimum": 1, "maximum": 730, "description": tr("Zeitraum in Tagen, Standard 120.")},
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
    return "\n".join(lines) if lines else tr("(noch keine Gerichte)")


def context_text(local_time: str, meal: str | None, place: str | None, catalogue: str,
                 places: list[str] | None = None) -> str:
    return (tr("Zeitpunkt der Mahlzeit: {time}", time=local_time) + (f" ({MEAL_LABELS[meal]})" if meal in MEAL_LABELS else "")
            + "\n" + tr("Ort: {place}", place=place or tr("nicht angegeben"))
            + ("\n" + tr("Philipps Orte: {places}", places=", ".join(places)) if places else "")
            + "\n\n" + tr("Philipps Gerichte-Katalog (je Gericht der Verlauf, darunter der letzte Eintrag; mehr über "
                          "dish_history und search_meals):") + f"\n{catalogue}")


def _number(value, low: float = 0, high: float = 500) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(tr("Antwort des Modells ist unvollständig")) from None
    if number != number:
        raise ValueError(tr("Antwort des Modells ist unvollständig"))
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
    """What the agent looked up, as short lines for the page (at most 12)."""
    lines = []
    for call in calls:
        args = call.get("args") or {}
        if call.get("name") == "search_meals":
            parts = [tr("„{query}“", query=args["query"]) if args.get("query") else tr("alle Mahlzeiten")]
            parts += [MEAL_LABELS.get(args.get("meal"), "")] + [args.get("place") or "", args.get("dish") or ""]
            lines.append(tr("Frühere Mahlzeiten: {parts}", parts=", ".join(part for part in parts if part)))
        elif call.get("name") == "dish_history":
            lines.append(tr("Verlauf: {dish}", dish=str(args.get("dish") or "")))
        elif call.get("name") == "web_search":
            lines.append(tr("Websuche: „{query}“", query=args["query"]) if args.get("query") else tr("Websuche"))
    return [line[:120] for line in lines][:12]
