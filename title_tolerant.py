"""Order-tolerant identity extraction for freeform card titles.

The canonical parser in `parse.py` reads the PSA store's machine-generated
grammar: YEAR MANUFACTURER #NUM PLAYER PSA GRADE. Other sellers write the same
facts in a different order - player first, grader at the end, a "Graded <year>"
prefix, "Card 200" instead of "#200".

This module runs ONLY where the canonical parse failed. A title the canonical
parser already understands is returned untouched, so no validated identity can
change. That is the whole safety argument: this is a fallback, never an
override.

It extracts each component independently and refuses to guess. A component that
cannot be read is left absent, and a title with two equally plausible readings
is rejected as ambiguous rather than resolved arbitrarily. Nothing here invents
a year, set, player, card number, grade or grader.
"""

import re

import card_vocab
import parse

# --- provenance ------------------------------------------------------------
CANONICAL = "canonical"          # parse.parse_title handled it
TOLERANT = "tolerant"            # this module supplied one or more fields
AMBIGUOUS = "ambiguous"          # two readings, no basis to choose

# --- graders ---------------------------------------------------------------
# Only graders whose numeric scale we model. "Beckett" written as a word is a
# real grader but its grade is often given as "GM 10" or "Auto 10", which is a
# different scale from a card grade - so a bare "Beckett" is NOT a grade.
GRADERS = ("PSA", "BGS", "SGC", "CGC")
_GRADER_GRADE = re.compile(
    rf"\b({'|'.join(GRADERS)})\s*(10|[1-9](?:\.5)?)\b(?!\s*/)", re.I)
_GRADER_ANY = re.compile(rf"\b({'|'.join(GRADERS)})\b", re.I)

# --- year ------------------------------------------------------------------
# A four-digit year, optionally a season span. Anchored to a word boundary so a
# serial like 1/2534 or a card number like 2025 inside "#2025" is not a year.
_YEAR = re.compile(r"(?<![\d#/-])((?:18|19|20)\d{2})(?:\s*-\s*(\d{2,4}))?(?![\d/])")

# --- card number -----------------------------------------------------------
# "#29", "#BCP99", "#JSA". Rejected when it is really a serial ("#1/1").
_HASH_NUM = re.compile(r"#\s*([A-Z]{0,5}-?\d+[A-Z]?)\b(?!\s*/)", re.I)
# "Card 200", "No. 29", "CRD 12" - the word form some sellers use instead.
_WORD_NUM = re.compile(r"\b(?:card|crd|no\.?)\s*#?\s*(\d+[A-Z]?)\b(?!\s*/)", re.I)

# --- serial / print run ----------------------------------------------------
_SERIAL = re.compile(r"(?<![\d/])(\d{1,4})\s*/\s*(\d{1,5})(?![\d/])")

# --- autograph -------------------------------------------------------------
# Autograph wording is an ATTRIBUTE, never part of the player name.
_AUTO_WORDS = re.compile(
    r"\b(auto|autos|autograph|autographed|signed|signature|sig)\b", re.I)

# Words that can never be part of a player's name.
_NOT_NAME = {
    "GRADED", "CARD", "CARDS", "TRADING", "ROOKIE", "RC", "HOF", "MINT", "GEM",
    "NM", "MT", "EX", "POP", "LOW", "HIGH", "RARE", "SP", "SSP", "VINTAGE",
    "BASEBALL", "FOOTBALL", "BASKETBALL", "HOCKEY", "SOCCER", "WRESTLING",
    "GOLF", "RACING", "BOXING", "UFC", "MMA", "NBA", "NFL", "MLB", "NHL",
    "AUTO", "SIGNED", "AUTOGRAPH", "AUTOGRAPHED", "SIG", "THE", "OF", "AND",
    "NEW", "LOT", "BOX", "PACK", "CASE", "SEALED", "RAW", "UNGRADED", "PSA",
    "BGS", "SGC", "CGC", "BECKETT", "EDITION", "SERIES", "SET", "INSERT",
    # PSA/BGS condition labels printed beside the grade. "PSA 6 EX-MT Mario
    # Lemieux" must not yield a player called "EX-MT MARIO LEMIEUX".
    "EXMT", "EX-MT", "NMMT", "NM-MT", "GEMMT", "GEM-MT", "VG", "VGEX", "VG-EX",
    "GD", "GOOD", "POOR", "PR", "FR", "AUTHENTIC", "ALTERED",
    # Set/product words that sit immediately before a name in some layouts.
    "USA", "DEBUT", "PROSPECTS", "CHROME", "DRAFT", "UPDATE", "CANVAS",
}

# Product-line words that follow a manufacturer: "Topps NOW", "Panini INSTANT",
# "Topps UPDATE". General title grammar across the hobby, not seller-specific.
# They matter here because our own surname vocabulary is built from parsed
# athlete names, and a handful of these leaked into it through earlier
# mis-parses - so "Topps Now Alex Ovechkin" would otherwise yield "NOW ALEX
# OVECHKIN". Listing them explicitly is cheaper than trusting a polluted set.
PRODUCT_LINE_WORDS = {
    "NOW", "INSTANT", "UPDATE", "DRAFT", "PROSPECTS", "TRADED", "RATED",
    "REFRACTOR", "HOLO", "FOIL", "SAPPHIRE", "UNIVERSITY", "NIL", "MERLIN",
}


def _clean(text):
    return parse.normalize(text or "")


def find_year(text):
    """(year, raw) or (None, None). The FIRST plausible year only.

    A second year later in a title is usually a season span or a set name, and
    choosing between them without a rule would be guessing.
    """
    m = _YEAR.search(text)
    if not m:
        return None, None
    year = int(m.group(1))
    if not 1860 <= year <= 2100:
        return None, None
    return year, m.group(0)


def find_grade(text):
    """(grader, grade, raw) from any position, or (None, None, None).

    Requires the grader and its number to be adjacent. "PSA 10" and "10 PSA"
    are not the same claim, and only the former is unambiguous.
    """
    hits = _GRADER_GRADE.findall(text)
    if not hits:
        return None, None, None
    graders = {g.upper() for g, _ in hits}
    grades = {v for _, v in hits}
    if len(graders) > 1 or len(grades) > 1:
        # "PSA 9 ... BGS 9.5" - two claims, no basis to pick one.
        return None, None, "AMBIGUOUS"
    m = _GRADER_GRADE.search(text)
    return hits[0][0].upper(), hits[0][1], m.group(0)


def find_card_number(text):
    """(number, raw) or (None, None); "AMBIGUOUS" when two forms disagree."""
    hashes = [h for h in _HASH_NUM.findall(text)]
    words = [w for w in _WORD_NUM.findall(text)]
    forms = {h.upper() for h in hashes} | {w.upper() for w in words}
    if not forms:
        return None, None
    if len(forms) > 1:
        return "AMBIGUOUS", None
    value = next(iter(forms))
    return value, (f"#{value}" if hashes else f"CARD {value}")


def find_serial(text):
    """(serial, print_run) or (None, None). Never read as a card number."""
    m = _SERIAL.search(text)
    if not m:
        return None, None
    num, den = int(m.group(1)), int(m.group(2))
    if num > den or den == 0:
        return None, None
    return num, den


def is_auto(text):
    return bool(_AUTO_WORDS.search(text))


def find_player(text, year_raw, grade_raw, surnames=None):
    """A player name only where the layout makes it unambiguous.

    Two supported layouts:
      player-first   "Cortez Kennedy Auto Signed 1990 Topps ..."  -> before year
      grader-first   "PSA 8 Shaquille O'Neal 1992 Hoops #1 ..."   -> between

    Autograph wording is stripped, never kept as part of the name. Anything
    that does not reduce to 2-4 plain name words is left absent rather than
    guessed at.
    """
    if not year_raw:
        return None
    head = text.split(year_raw)[0]
    if grade_raw and grade_raw in head:
        head = head.split(grade_raw)[-1]
    head = _AUTO_WORDS.sub(" ", head)
    head = re.sub(r"[^A-Z0-9'.\- ]", " ", head.upper())
    words = [w for w in head.split()
             if w and w not in _NOT_NAME and w not in PRODUCT_LINE_WORDS
             and not w.isdigit()]
    if not 2 <= len(words) <= 4:
        return None
    if any(len(w) == 1 and not w.endswith(".") for w in words):
        return None
    # Every word must be one the corpus has seen in an athlete's name. Without
    # this a stray set word ("Stars & Stripes USA Dylan Crews") rides along and
    # the query searches for a player who does not exist.
    if surnames and any(w.replace(".", "") not in surnames for w in words):
        return None
    return " ".join(words)


def find_player_before_number(text, surnames):
    """The "Graded <year> <set> <player> #NUM ..." layout.

    The card-number token is a hard boundary, so the player ends immediately
    before it. Where it BEGINS is the open question - set names are unbounded,
    so counting words back would be arbitrary.

    Instead the surname vocabulary built from our own 143k-row corpus decides:
    the run must END on a word we have independently seen used as a surname.
    If the last word before the number is not a known surname, this refuses to
    answer rather than guessing where the set ends and the name starts.
    """
    if not surnames:
        return None
    m = _HASH_NUM.search(text)
    if not m:
        return None
    head = _AUTO_WORDS.sub(" ", text[:m.start()])
    head = re.sub(r"[^A-Z0-9'.\- ]", " ", head.upper())
    words = [w for w in head.split()
             if w and w not in _NOT_NAME and not w.isdigit()
             and w not in PRODUCT_LINE_WORDS
             and not parse.MANUFACTURERS.get(w)
             and not parse.PRODUCT_MANUFACTURER.get(w)]
    if len(words) < 2 or words[-1] not in surnames:
        return None
    # Extend the run only through words the corpus has independently seen used
    # in an athlete's name. That is what stops "Topps NOW Alex Ovechkin" from
    # becoming "NOW ALEX OVECHKIN" - NOW is product vocabulary, not a name, so
    # the run stops there. No word-count heuristic is involved.
    run = [words[-1]]
    for w in reversed(words[:-1]):
        if len(run) >= 3 or w not in surnames or w in PRODUCT_LINE_WORDS:
            break
        run.insert(0, w)
    if len(run) < 2:
        return None
    return " ".join(run)


# Team names that CONTAIN a parallel colour word. Without these, "Chicago White
# Sox" reads as the WHITE parallel and "Boston Red Sox" as RED. General hobby
# grammar, not a seller quirk.
COLOUR_TEAM_PHRASES = (
    "RED SOX", "WHITE SOX", "BLUE JAYS", "GREEN BAY", "RED WINGS",
    "BLACK HAWKS", "BLACKHAWKS", "RED RAIDERS", "GOLDEN STATE",
    "GOLDEN KNIGHTS", "BLUE JACKETS", "SILVER KNIGHTS", "REDS", "REDSKINS",
    "BROWNS", "WHITECAPS", "BLUES", "ORANGE BOWL",
)


def find_parallel(text, athlete=None, grade_raw=None, year_raw=None):
    """(parallel, ambiguous) - only a parallel the title explicitly states.

    Extraction itself is delegated to parse._extract_parallel, the same
    authoritative routine the canonical parser and comp matching use, so the
    vocabulary cannot drift into a second list.

    What this adds is the SPAN. Run over a whole freeform title, that routine
    reads "Chicago White Sox" as the WHITE parallel and "Los Angeles Rams" as
    nothing at all only by luck. So the player, the team words, the grade, the
    year and the card number are removed first, and only what remains can name
    a parallel.

    Nothing is inferred from design, set, price, rarity or a serial number: a
    parallel is returned only when its own words are present.
    """
    span = text.upper()
    for phrase in COLOUR_TEAM_PHRASES:
        span = span.replace(phrase, " ")
    for chunk in (grade_raw, year_raw):
        if chunk:
            span = span.replace(chunk.upper(), " ")
    for word in (athlete or "").split():
        span = re.sub(rf"\b{re.escape(word)}\b", " ", span)
    for word in card_vocab.TEAM_CITY | card_vocab.LEAGUE_SPORT:
        span = re.sub(rf"\b{re.escape(word)}\b", " ", span)
    span = _HASH_NUM.sub(" ", span)
    span = _SERIAL.sub(" ", span)
    span = re.sub(rf"\b({'|'.join(GRADERS)})\s*\d+(?:\.5)?\b", " ", span)
    parallel, _rem = parse._extract_parallel(re.sub(r"\s+", " ", span))
    if not parallel:
        return None, False
    # Two distinct colours is two competing readings, not one compound
    # parallel. Refuse rather than pick.
    colours = [c for c in parse.PARALLEL_COLORS
               if re.search(rf"\b{c}\b", parallel)]
    if len(colours) > 1:
        return None, True
    return parallel, False


def find_manufacturer(text):
    """A known manufacturer anywhere in the title, or None if two disagree."""
    found = []
    for token in re.split(r"[^A-Z0-9'&-]+", text.upper()):
        canon = parse.MANUFACTURERS.get(token) or parse.PRODUCT_MANUFACTURER.get(token)
        if canon and canon not in found:
            found.append(canon)
    if not found:
        return None
    return found[0]                     # first named wins; stable and explainable


def extract(title, surnames=None):
    """Independent, order-free extraction. Returns fields + provenance.

    `ambiguity` lists every component that had more than one reading. A caller
    must treat a non-empty list as a refusal to parse, not as a partial answer.
    """
    text = _clean(title)
    out = {"year": None, "year_raw": None, "grader": None, "grade_value": None,
           "grade_raw": None, "card_number": None, "card_number_raw": None,
           "serial_num": None, "print_run": None, "is_auto": 0,
           "athlete": None, "manufacturer": None, "parallel": None,
           "provenance": TOLERANT, "ambiguity": []}

    year, year_raw = find_year(text)
    out["year"], out["year_raw"] = year, year_raw

    grader, grade, grade_raw = find_grade(text)
    if grade_raw == "AMBIGUOUS":
        out["ambiguity"].append("multiple graders or grades")
    else:
        out["grader"], out["grade_value"], out["grade_raw"] = grader, grade, grade_raw

    number, number_raw = find_card_number(text)
    if number == "AMBIGUOUS":
        out["ambiguity"].append("multiple card-number forms")
    else:
        out["card_number"], out["card_number_raw"] = number, number_raw

    out["serial_num"], out["print_run"] = find_serial(text)
    out["is_auto"] = 1 if is_auto(text) else 0
    out["manufacturer"] = find_manufacturer(text)
    out["athlete"] = find_player(text, year_raw, out["grade_raw"], surnames)
    if not out["athlete"]:
        out["athlete"] = find_player_before_number(text, surnames)
    out["parallel"], par_ambiguous = find_parallel(
        text, out["athlete"], out["grade_raw"], year_raw)
    if par_ambiguous:
        out["ambiguity"].append("two competing parallel readings")
    if out["ambiguity"]:
        out["provenance"] = AMBIGUOUS
    return out


REQUIRED = ("year", "grader", "grade_value", "card_number", "athlete")


def normalize_canonical(fields):
    """Give a canonical parse the same field shape as a tolerant one.

    parse.py never emits `grader`: PSA is implicit there, because GRADE_NUM_RE
    matches only a literal PSA token and rival graders are excluded outright.
    So a canonical NUMERIC grade did come from PSA - but a consumer reading
    `grader` off both dicts saw None for every canonical row, which made
    `is_complete` reject 399 perfectly good identities and reported
    sports-cards-forever as having zero graders.

    The value is derived, not invented: it is stated only where the canonical
    parser actually found a PSA grade.
    """
    out = dict(fields)
    if out.get("grader") is None and out.get("grade_type") == "NUMERIC" \
            and out.get("grade_value"):
        out["grader"] = "PSA"
    return out


def is_complete(fields):
    """Every component a sold-comp query needs, and no ambiguity."""
    return (not fields.get("ambiguity")
            and all(fields.get(k) for k in REQUIRED))


def parse_tolerant(title, surnames=None):
    """Canonical parse first; tolerant extraction only if it fell short.

    Returns (fields, provenance, ambiguity). A canonical success is returned
    verbatim, so an identity the validated pipeline already produces can never
    be altered here.
    """
    result = parse.parse_title(title)
    conf = result["conf"]
    if parse.RANK[parse.identity_confidence(conf)] >= parse.RANK[parse.MEDIUM]:
        return normalize_canonical(result["fields"]), CANONICAL, []
    got = extract(title, surnames)
    if got["ambiguity"]:
        return result["fields"], AMBIGUOUS, got["ambiguity"]
    return got, TOLERANT, []


# --------------------------------------------------------------------------
# Seller-year disagreement: a diagnostic, never a correction
# --------------------------------------------------------------------------
# Checkpoint candidate 13 was titled "Graded 2022 Bowman Jackson Holliday #BS6"
# but every comp calls the card 2023, so 47 of 50 were correctly rejected on
# year and the candidate ended with no evidence. Our parse of the title is
# right; the SELLER's year is wrong.
#
# This reports that shape. It never rewrites the candidate year, never accepts
# the rejected comps, and never feeds a valuation - a title is not outvoted by
# a search result.
YEAR_DISAGREEMENT = "candidate_year_disagreement"
MIN_DISAGREEING_COMPS = 5          # one or two rows prove nothing
MIN_DISAGREEMENT_SHARE = 0.80      # near-unanimous, not merely a majority


def year_disagreement(candidate_year, rejected_years, accepted_count=0):
    """Flag when otherwise-matching comps consistently name one other year.

    `rejected_years` are the years parsed from comps rejected ONLY on year.
    Returns None unless the evidence is both plentiful and near-unanimous.
    """
    if accepted_count or not candidate_year or not rejected_years:
        return None
    counts = {}
    for y in rejected_years:
        if y:
            counts[y] = counts.get(y, 0) + 1
    if not counts:
        return None
    total = sum(counts.values())
    year, n = max(counts.items(), key=lambda kv: kv[1])
    if n < MIN_DISAGREEING_COMPS or n / total < MIN_DISAGREEMENT_SHARE:
        return None
    if year == candidate_year:
        return None
    return {"flag": YEAR_DISAGREEMENT, "candidate_year": candidate_year,
            "observed_year": year, "supporting_comps": n,
            "year_distribution": dict(sorted(counts.items())),
            "action": "manual review only - the candidate year is not rewritten "
                      "and these comps remain rejected"}
