"""Step 2: parse a PSA eBay listing title into a normalized card identity.

Vocabularies here were mined from the live store rather than recalled, so they
reflect how PSA actually writes titles. The shape is consistently:

    YEAR MANUFACTURER SET/PARALLEL [#CARDNO] ATHLETE [FLAGS] [SERIAL] PSA GRADE

Everything the parser cannot establish is left empty and flagged. Nothing is
guessed - a wrong identity is worse than a missing one, because it would let a
listing be compared against a different card.
"""

import hashlib
import re
import unicodedata

# Confidence ladder. Identity-critical fields must reach MEDIUM to be usable.
HIGH, MEDIUM, LOW, MISSING = "HIGH", "MEDIUM", "LOW", "MISSING"
RANK = {MISSING: 0, LOW: 1, MEDIUM: 2, HIGH: 3}

# eBay truncates titles at 80 characters; at the limit, trailing fields are lost.
TITLE_LIMIT = 78

RIVAL_GRADERS = ("BGS", "SGC", "CGC", "CSG", "BVG", "HGA", "TAG", "GMA", "ISA", "RCG")
# A rival grader only counts when a grade follows it. "TAG" alone appears inside
# set names ("MATERIAL-TAG", "TAG SWATCHES"), which would exclude PSA cards.
RIVAL_RE = re.compile(rf"\b({'|'.join(RIVAL_GRADERS)})\s*(\d{{1,2}}(?:\.\d)?)\b")

# Explicit manufacturer tokens (mined: first token after the year).
MANUFACTURERS = {
    "TOPPS": "TOPPS", "PANINI": "PANINI", "BOWMAN": "BOWMAN", "BOWMAN'S": "BOWMAN",
    "UD": "UPPER DECK", "UPPER": "UPPER DECK", "FLEER": "FLEER", "DONRUSS": "DONRUSS",
    "DONRUSS/LEAF": "DONRUSS", "SKYBOX": "SKYBOX", "LEAF": "LEAF", "SCORE": "SCORE",
    "O-PEE-CHEE": "O-PEE-CHEE", "PINNACLE": "PINNACLE", "PLAYOFF": "PLAYOFF",
    "SP": "UPPER DECK", "SPX": "UPPER DECK", "BBM": "BBM", "CALBEE": "CALBEE",
    "PACIFIC": "PACIFIC", "SAGE": "SAGE", "PRESS": "PRESS PASS", "WILD": "WILD CARD",
    "KELLOGG'S": "KELLOGG'S", "HOSTESS": "HOSTESS", "POST": "POST",
    "GOUDEY": "GOUDEY", "SPORTFLICS": "SPORTFLICS", "MYTHOS": "MYTHOS",
}

# Product lines whose manufacturer is implied when the title omits it
# ("1993 FINEST #199 ..." is a Topps product).
PRODUCT_MANUFACTURER = {
    "FINEST": "TOPPS", "STADIUM": "TOPPS", "CHROME": "TOPPS", "HERITAGE": "TOPPS",
    "ALLEN": "TOPPS", "GYPSY": "TOPPS", "BUNT": "TOPPS", "GALLERY": "TOPPS",
    "CONTENDERS": "PANINI", "PRIZM": "PANINI", "SELECT": "PANINI", "OPTIC": "PANINI",
    "MOSAIC": "PANINI", "CHRONICLES": "PANINI", "IMMACULATE": "PANINI",
    "OBSIDIAN": "PANINI", "SPECTRA": "PANINI", "REVOLUTION": "PANINI",
    "NATIONAL": "PANINI", "ABSOLUTE": "PANINI", "CERTIFIED": "PANINI",
    "ULTRA": "FLEER", "FLAIR": "FLEER", "METAL": "FLEER", "HOOPS": "HOOPS",
    "COLLECTOR'S": "UPPER DECK", "PRO": "PRO SET", "CLASSIC": "CLASSIC",
    "STAR": "STAR", "MUSEUM": "TOPPS", "INSTANT": "TOPPS",
}

# Parallel / finish vocabulary. Multi-word entries are matched first.
PARALLEL_PHRASES = [
    "CRACKED ICE", "STAINED GLASS", "GOLD FLASH", "RAINBOW FOIL", "TIE-DYE",
    "GREEN SCOPE", "FAST BREAK", "DIE-CUT", "SHOCK PRIZM", "NO HUDDLE",
    # Prizm Monopoly families - "Gold Money Shimmer" is not "Gold Shimmer".
    "MONEY SHIMMER", "MONEY BLAST",
    "PADPARADSCHA", "SUPERFRACTOR", "X-FRACTOR", "XFRACTOR", "REFRACTOR",
    "SNAKESKIN", "DRAGON SCALE", "GENESIS", "VELOCITY", "DISCO", "MOJO",
    "PULSAR", "SHIMMER", "SPARKLE", "SAPPHIRE", "SPECKLE", "ATOMIC", "NEGATIVE",
    "HYPER", "LASER", "SCOPE", "WAVE", "FOIL", "ICE", "FOTL", "TIFFANY",
    "GLOSSY", "CANVAS", "HOLO", "CAMO", "LOGOFRACTOR",
]
# PRIZM / CHROME / OPTIC are product lines, not parallels - "SILVER PRIZM" is a
# silver parallel of the Prizm set. Treating them as parallels emptied the set
# name, so they are deliberately absent from the vocabulary above.
PARALLEL_COLORS = [
    "GOLD", "SILVER", "BLACK", "BLUE", "RED", "GREEN", "ORANGE", "PURPLE",
    "PINK", "WHITE", "YELLOW", "BRONZE", "TEAL", "AQUA", "RAINBOW", "PLATINUM",
    "EMERALD", "RUBY", "AMETHYST", "ONYX", "COPPER", "MAGENTA", "CYAN", "NEON",
    "CHARCOAL", "LAVA", "TIDAL", "SEISMIC", "MARBLE",
]

INSERT_HINTS = [
    "AUTOS", "AUTOGRAPHS", "SIGNATURES", "SIGNATURE", "ROOKIES", "PROSPECTS",
    "PROSPECT", "UPDATE", "DRAFT", "PICKS", "STARS", "LEGENDS", "GREATS",
    "CHECKLIST", "REPRINTS", "VARIATIONS",
]

# Sport is only assigned from explicit evidence in the title - never inferred
# from a player or set name.
SPORT_TOKENS = [
    ("WNBA", "BASKETBALL"), ("NBA", "BASKETBALL"), ("BASKETBALL", "BASKETBALL"),
    ("NFL", "FOOTBALL"), ("FOOTBALL", "FOOTBALL"),
    ("MLB", "BASEBALL"), ("NPB", "BASEBALL"), ("BASEBALL", "BASEBALL"),
    ("NHL", "HOCKEY"), ("HOCKEY", "HOCKEY"),
    ("UFC", "MMA"), ("MMA", "MMA"), ("BOXING", "BOXING"),
    ("FIFA", "SOCCER"), ("UEFA", "SOCCER"), ("LA LIGA", "SOCCER"),
    ("PREMIER LEAGUE", "SOCCER"), ("BUNDESLIGA", "SOCCER"), ("MLS", "SOCCER"),
    ("SOCCER", "SOCCER"), ("PGA", "GOLF"), ("LIV GOLF", "GOLF"), ("GOLF", "GOLF"),
    ("NASCAR", "RACING"), ("FORMULA 1", "RACING"), ("FORMULA ONE", "RACING"),
    ("F1", "RACING"), ("WWE", "WRESTLING"), ("WRESTLING", "WRESTLING"),
    ("ATP", "TENNIS"), ("WTA", "TENNIS"), ("TENNIS", "TENNIS"),
    ("OLYMPIC", "OLYMPICS"), ("CRICKET", "CRICKET"),
]

# Tokens that terminate the athlete name.
STOP_TOKENS = {
    "ROOKIE", "RC", "AUTO", "AUTOGRAPH", "AUTOGRAPHED", "SIGNED", "SIG",
    "PATCH", "RELIC", "JERSEY", "MEMORABILIA", "SP", "SSP", "HOF", "PSA",
    "GEM", "MT", "MINT", "RPA", "PROSPECT", "RATED",
}

# Handles "2024", "2024-25", "2021-2022", "1880S" and years glued to the brand
# ("1996TOPPS FINEST ...").
YEAR_RE = re.compile(r"^((?:18|19|20)\d{2}(?:-(?:\d{4}|\d{2}))?S?)(?=\s|$|[A-Z])")
# PSA may be glued to the previous word ("ROOKIE RCPSA 9"), so no left boundary.
GRADE_NUM_RE = re.compile(r"PSA\s*(10|[1-9](?:\.5)?)(?![\d.])")
GRADE_AUTH_RE = re.compile(r"PSA\s*(AUTHENTIC(?:\s+ALTERED)?|AUTH)\b")

# PSA qualifiers: MC miscut, OC off-centre, ST stain, MK marked, PD print defect.
# A qualifier materially lowers value, so it is part of slab identity.
QUALIFIERS = ("MC", "OC", "ST", "MK", "PD")
QUALIFIER_RE = re.compile(
    rf"PSA\s*(?:10|[1-9](?:\.5)?)\s+({'|'.join(QUALIFIERS)})\b")

# A separate autograph grade: "PSA 9 AUTO 10", "PSA AUTHENTIC AUTO 9",
# "PSA 8 AUTO AUTHENTIC". Bare "AUTO" (no grade) only marks the card as signed,
# and "AUTO GOLD" / "AUTO REFRACTOR" are set names, not grades.
AUTO_GRADE_RE = re.compile(r"\bAUTO\s*(10|[1-9](?:\.5)?|AUTHENTIC|AUTH)\b")
SERIAL_RE = re.compile(r"(?<![\d/.])(\d{1,4})\s*/\s*(\d{1,5})(?![\d/])")
# PSA also writes the print run without a serial: "#/50" means one of 50, with
# the copy number undisclosed. Requires a slash immediately after the '#', so a
# card number like "#073/150" or "#F/X12" can never match.
PRINT_RUN_RE = re.compile(r"#\s*/\s*(\d{1,5})\b")
# A hash-prefixed serial stamp: "#429/500" is copy 429 of 500, NOT card number
# 429/500. Leading zeros are allowed ("#073/150" can be copy 73 of 150) but they
# are weak evidence, so a zero-padded stamp is only read as a serial when the
# title states a card number elsewhere. That keeps "#073/150 HYDREIGON", where
# the token IS the card number, intact.
SERIAL_STAMP_RE = re.compile(r"#\s*(\d{1,5})\s*/\s*(\d{1,5})\b")
OTHER_CARDNO_RE = re.compile(r"#\s*([A-Z0-9][A-Z0-9\-.\']*)")


def _serial_stamp(text):
    """The "#N/M" match that is a serial stamp rather than a card number."""
    for m in SERIAL_STAMP_RE.finditer(text):
        numerator, denominator = m.group(1), m.group(2)
        if int(numerator) > int(denominator):
            continue                       # "#69/30" cannot be copy 69 of 30
        if numerator.startswith("0"):
            # Zero-padded: only a stamp when a separate card number exists.
            others = [o for o in OTHER_CARDNO_RE.finditer(text)
                      if o.start() != m.start()]
            if not others:
                continue
        return m
    return None
CARDNO_RE = re.compile(r"#\s*([A-Z0-9][A-Z0-9\-./']*)")
ROOKIE_RE = re.compile(r"\b(ROOKIE|RC)\b")
AUTO_RE = re.compile(
    r"\b(AUTO|AUTOS|AUTOGRAPH|AUTOGRAPHS|AUTOGRAPHED|SIGNED|SIG)\b")
RELIC_RE = re.compile(r"\b(PATCH|RELIC|JERSEY|MEMORABILIA|GAME[- ]USED|RPA)\b")


def normalize(title):
    """Uppercase, strip accents, unify punctuation, collapse whitespace."""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("’", "'").replace("‘", "'").replace("–", "-")
    return re.sub(r"\s+", " ", t).strip().upper()


def rival_grader(text):
    m = RIVAL_RE.search(text)
    return m.group(1) if m else None


def canonical_brand(name):
    """The maker a manufacturer name or alias resolves to. None if unknown.

    "UD" and "Upper" are Upper Deck; "Ultra" and "Metal" are Fleer; "Hoops" is
    its own brand. Used to compare makers, never to compare product lines.
    """
    if not name:
        return None
    text = normalize(str(name))
    for token in text.split():
        if token in MANUFACTURERS:
            return MANUFACTURERS[token]
        if token in PRODUCT_MANUFACTURER:
            return PRODUCT_MANUFACTURER[token]
    return None


def brands_in(text):
    """Every maker a title names, whether by its own name or a product line.

    A title need not parse cleanly for its brand to be legible: "PSA 6 Michael
    Jordan 1989-90 NBA Hoops #21" has no year-first prefix, so the structured
    parser yields manufacturer=None, yet the title plainly says Hoops.
    """
    out = set()
    for token in re.split(r"[^A-Z0-9'-]+", normalize(text or "")):
        if not token:
            continue
        if token in MANUFACTURERS:
            out.add(MANUFACTURERS[token])
        elif token in PRODUCT_MANUFACTURER:
            out.add(PRODUCT_MANUFACTURER[token])
    return out


def _find_grade(text):
    """Return (grade_type, value, qualifier, auto_grade, raw, confidence).

    The card grade, the qualifier and the autograph grade are three different
    things. "PSA 9 AUTO 10" is a card graded 9 whose signature graded 10 - the
    10 must never be read as the card grade.
    """
    qual = QUALIFIER_RE.search(text)
    qualifier = qual.group(1) if qual else None

    auto = AUTO_GRADE_RE.search(text)
    auto_grade = None
    if auto:
        value = auto.group(1)
        auto_grade = "AUTHENTIC" if value in ("AUTHENTIC", "AUTH") else value

    matches = list(GRADE_NUM_RE.finditer(text))
    if matches:
        m = matches[-1]
        raw = text[m.start():(qual.end() if qual else m.end())]
        if auto:
            raw = text[m.start():max(m.end(), auto.end())]
        return "NUMERIC", m.group(1), qualifier, auto_grade, raw, HIGH

    m = GRADE_AUTH_RE.search(text)
    if m:
        value = "AUTHENTIC ALTERED" if "ALTERED" in m.group(1) else "AUTHENTIC"
        raw = text[m.start():(auto.end() if auto else m.end())]
        return "AUTHENTIC", value, qualifier, auto_grade, raw, HIGH

    # No card grade. An autograph grade alone (PSA/DNA) is not a card grade.
    return None, None, None, auto_grade, None, MISSING


GRADE_BEFORE_RE = re.compile(r"PSA\s*$")


def _serial_match(text):
    """First serial match that is really a serial.

    Skips a card number ("#69/30"), and skips a grade sitting above a year:
    "... #30 PSA 8 / 1991 Fleer ..." is two cards in one listing, not copy 8 of
    1991. A four-digit year only counts as a print run when the numerator is not
    a PSA grade.
    """
    for m in SERIAL_RE.finditer(text):
        if m.start() > 0 and text[m.start() - 1] == "#":
            continue
        serial, run = int(m.group(1)), int(m.group(2))
        if serial > run:
            continue
        looks_like_year = 1900 <= run <= 2099
        if looks_like_year and GRADE_BEFORE_RE.search(text[:m.start()]):
            continue                  # "PSA 8 / 1991" - a grade, then a year
        return m
    return None


def _find_serial(text):
    """Serial / print run, ignoring a match that is really a card number."""
    m = _serial_match(text)
    if m:
        return int(m.group(1)), int(m.group(2)), m.span()
    return None, None, None


def _find_sport(text):
    padded = f" {text} "
    for token, sport in SPORT_TOKENS:
        if f" {token} " in padded or f" {token}-" in padded:
            return sport, HIGH
    return None, MISSING


def _dedupe_tokens(text):
    """Collapse repeated tokens ("PRIZM DRAFT PICKS PRIZM" -> "PRIZM DRAFT PICKS").

    Pulling a colour out of "SILVER PRIZM" can leave the product name twice.
    """
    seen, kept = set(), []
    for tok in text.split():
        if tok not in seen:
            seen.add(tok)
            kept.append(tok)
    return " ".join(kept)


def _extract_parallel(blob):
    """Pull parallel terms out of the set span. Returns (parallel, remainder)."""
    found, remainder = [], blob
    for phrase in PARALLEL_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", remainder):
            found.append(phrase)
            remainder = re.sub(rf"\b{re.escape(phrase)}\b", " ", remainder)
    for color in PARALLEL_COLORS:
        if re.search(rf"\b{color}\b", remainder):
            found.append(color)
            remainder = re.sub(rf"\b{color}\b", " ", remainder)
    order = {p: i for i, p in enumerate(PARALLEL_COLORS + PARALLEL_PHRASES)}
    found.sort(key=lambda p: order.get(p, 999))
    return " ".join(found) or None, re.sub(r"\s+", " ", remainder).strip()


def parse_title(title):
    """Parse one title. Returns a dict of fields, confidences and issues."""
    raw = title or ""
    text = normalize(raw)
    issues = []
    out = {
        "sport": None, "year": None, "year_raw": None, "manufacturer": None,
        "set_name": None, "insert_name": None, "parallel": None, "athlete": None,
        "card_number": None, "is_rookie": 0, "is_auto": 0, "is_relic": 0,
        "serial_num": None, "print_run": None, "grade_type": None,
        "grade_value": None, "grade_qualifier": None, "auto_grade": None,
        "grade_raw": None, "cert_number": None,
        # A title ending in "PSA" lost its grade to eBay's 80-char cap even when
        # it sits below the length threshold.
        "truncation_risk": 1 if (len(raw) >= TITLE_LIMIT
                                 or text.endswith("PSA")) else 0,
    }
    conf = dict.fromkeys(
        ("sport", "year", "manufacturer", "set_name", "insert_name", "parallel",
         "athlete", "card_number", "grade"), MISSING
    )

    rival = rival_grader(text)
    if rival and not re.search(r"PSA", text):
        return {"excluded": rival, "fields": out, "conf": conf, "issues": issues,
                "normalized": text}

    # --- grade -----------------------------------------------------------
    gtype, gvalue, qualifier, auto_grade, graw, gconf = _find_grade(text)
    out["grade_type"], out["grade_value"], conf["grade"] = gtype, gvalue, gconf
    out["grade_qualifier"] = qualifier
    out["auto_grade"] = auto_grade
    out["grade_raw"] = graw
    if gtype is None:
        if re.search(r"PSA[\s/]*DNA", text):
            # PSA/DNA authenticates signatures; it is not a card grade, so the
            # slab has no grade to compare on.
            reason = "PSA/DNA autograph authentication, not a card grade"
        elif out["truncation_risk"]:
            reason = "grade absent - title truncated at eBay's 80-char limit"
        else:
            reason = "no PSA grade token found"
        issues.append(("grade", reason))

    # --- year ------------------------------------------------------------
    ym = YEAR_RE.match(text)
    if ym:
        out["year_raw"] = ym.group(1)
        out["year"] = int(ym.group(1)[:4])
        conf["year"] = HIGH
        body = text[ym.end():].strip()
    else:
        issues.append(("year", "no leading 4-digit year"))
        body = text

    # --- sport (explicit evidence only) ----------------------------------
    out["sport"], conf["sport"] = _find_sport(text)

    # --- serial / print run ----------------------------------------------
    serial, run, span = _find_serial(text)
    out["serial_num"], out["print_run"] = serial, run

    # A "#N/M" serial stamp is resolved before anything reads a card number,
    # so the numerator can never be mistaken for one.
    stamp = _serial_stamp(text)
    if stamp:
        out["serial_num"] = int(stamp.group(1))
        out["print_run"] = int(stamp.group(2))
        serial = out["serial_num"]
        run = out["print_run"]

    pr = PRINT_RUN_RE.search(text)
    print_run_conflict = False
    if pr:
        declared = int(pr.group(1))
        if run is not None and run != declared:
            # "12/50" and "#/25" in one title cannot both be right.
            issues.append(("print_run",
                           f"conflicting print runs: serial says /{run}, "
                           f"'#/' says /{declared}"))
            print_run_conflict = True
        out["print_run"] = declared
        if run is None:
            # "#/50" discloses the run but not which copy - do not invent one.
            out["serial_num"] = None

    # --- flags -----------------------------------------------------------
    out["is_rookie"] = 1 if ROOKIE_RE.search(text) else 0
    out["is_auto"] = 1 if AUTO_RE.search(text) else 0
    out["is_relic"] = 1 if RELIC_RE.search(text) else 0

    # --- manufacturer ----------------------------------------------------
    tokens = body.split()
    if tokens:
        head = tokens[0]
        if head in MANUFACTURERS:
            out["manufacturer"] = MANUFACTURERS[head]
            conf["manufacturer"] = HIGH
            if head in ("UPPER", "PRESS", "WILD") and len(tokens) > 1:
                tokens = tokens[2:]
            else:
                tokens = tokens[1:]
        elif head in PRODUCT_MANUFACTURER:
            out["manufacturer"] = PRODUCT_MANUFACTURER[head]
            conf["manufacturer"] = MEDIUM  # inferred from product line
        else:
            issues.append(("manufacturer", f"unrecognized leading token {head!r}"))
            conf["manufacturer"] = LOW
    body_after_mfr = " ".join(tokens)

    # Cut the grade and serial off the end before reading set / athlete, so
    # neither leaks into a name field.
    # Blank the serial stamp so CARDNO_RE cannot pick up "429/500".
    stamp_body = _serial_stamp(body_after_mfr)
    if stamp_body:
        body_after_mfr = (body_after_mfr[:stamp_body.start()]
                          + " " * (stamp_body.end() - stamp_body.start())
                          + body_after_mfr[stamp_body.end():])

    end = len(body_after_mfr)
    for m in (GRADE_NUM_RE.search(body_after_mfr),
              GRADE_AUTH_RE.search(body_after_mfr),
              _serial_match(body_after_mfr),
              PRINT_RUN_RE.search(body_after_mfr)):
        if m:
            end = min(end, m.start())
    body_core = body_after_mfr[:end].strip()

    # --- card number, then split set span from athlete span ---------------
    cm = CARDNO_RE.search(body_core)
    if cm:
        out["card_number"] = cm.group(1).rstrip(".-/")
        conf["card_number"] = HIGH
        set_blob = body_core[:cm.start()].strip()
        tail = body_core[cm.end():].strip()
        if "/" in out["card_number"]:
            issues.append(("card_number",
                           f"card number {out['card_number']!r} contains '/' - "
                           "may be a serial"))
            conf["card_number"] = LOW
    else:
        # Without a card number there is no reliable boundary between set name
        # and athlete name, so neither is guessed.
        issues.append(("card_number", "no '#' card number in title"))
        conf["card_number"] = MISSING
        set_blob, tail = body_core, None

    # --- parallel --------------------------------------------------------
    parallel, remainder = _extract_parallel(set_blob)
    remainder = _dedupe_tokens(remainder.strip(" -,"))
    out["parallel"] = parallel
    if parallel:
        conf["parallel"] = HIGH
    elif len(remainder.split()) >= 5:
        # Long unexplained set span with no known parallel term: a parallel we
        # do not recognize may be hiding in it.
        conf["parallel"] = LOW
        issues.append(("parallel",
                       f"no known parallel term in long set span {remainder!r}"))
    else:
        conf["parallel"] = MEDIUM  # most likely a base card

    # --- set / insert ----------------------------------------------------
    if remainder:
        out["set_name"] = remainder
        conf["set_name"] = MEDIUM if conf["manufacturer"] != LOW else LOW
        hits = [h for h in INSERT_HINTS if re.search(rf"\b{h}\b", remainder)]
        if hits:
            out["insert_name"] = " ".join(hits)
            conf["insert_name"] = LOW  # heuristic, not a curated set list
    elif out["manufacturer"]:
        # No set span at all is the hobby convention for the flagship base set
        # ("1975 TOPPS #370 TOM SEAVER" is 1975 Topps base). Not a guess.
        out["set_name"] = "BASE"
        conf["set_name"] = MEDIUM
    else:
        issues.append(("set_name", "empty set span and unknown manufacturer"))
        conf["set_name"] = MISSING

    # --- athlete ---------------------------------------------------------
    if tail is None:
        conf["athlete"] = MISSING
        issues.append(("athlete", "no card number to anchor the athlete name"))
    else:
        words, name = tail.split(), []
        for w in words:
            bare = w.strip(".,'")
            if bare in STOP_TOKENS or bare.startswith("PSA"):
                break
            if re.fullmatch(r"[\d/]+", bare):
                break
            name.append(w)
        if name:
            out["athlete"] = " ".join(name).strip("-,'. ")
            conf["athlete"] = MEDIUM
        else:
            issues.append(("athlete", "could not isolate an athlete name"))
            conf["athlete"] = MISSING

    if print_run_conflict:
        # Applied last: the card-number section above reassigns this field.
        conf["card_number"] = LOW

    return {"excluded": None, "fields": out, "conf": conf, "issues": issues,
            "normalized": text}


IDENTITY_FIELDS = ("year", "set_name", "parallel", "card_number", "grade")


def identity_confidence(conf):
    """Weakest link across the identity-critical fields."""
    worst = min(RANK[conf[f]] for f in IDENTITY_FIELDS)
    return {v: k for k, v in RANK.items()}[worst]


def parse_status(conf, issues):
    ident = identity_confidence(conf)
    if RANK[ident] >= RANK[MEDIUM] and not issues:
        return "ok"
    if RANK[ident] >= RANK[MEDIUM]:
        return "partial"
    return "failed"


def _norm_key_part(value):
    if value is None or value == "":
        return "-"
    return re.sub(r"[^A-Z0-9]+", "", str(value).upper()) or "-"


def make_keys(fields):
    """card_key ignores grade and serial; slab_key adds the grade.

    Serial number is deliberately excluded - 4/10 and 7/10 are the same card
    for valuation. Print run is included, so parallels of different scarcity
    never collide. Grade lives only in slab_key, which makes comparing across
    grades structurally impossible.
    """
    card_parts = [
        _norm_key_part(fields.get("sport")),
        _norm_key_part(fields.get("year")),
        _norm_key_part(fields.get("manufacturer")),
        _norm_key_part(fields.get("set_name")),
        _norm_key_part(fields.get("insert_name")),
        _norm_key_part(fields.get("parallel")),
        _norm_key_part(fields.get("card_number")),
        _norm_key_part(fields.get("athlete")),
        str(fields.get("is_auto") or 0),
        str(fields.get("is_relic") or 0),
        _norm_key_part(fields.get("print_run")),
    ]
    card_key = hashlib.sha1("|".join(card_parts).encode()).hexdigest()[:16]
    # Grade identity is the card grade, its qualifier, and any autograph grade.
    # PSA 4 and PSA 4 MC are different slabs; so are PSA 9 AUTO 9 and PSA 9 AUTO 10.
    grade = "|".join((
        fields.get("grade_type") or "-",
        _norm_key_part(fields.get("grade_value")),
        _norm_key_part(fields.get("grade_qualifier")),
        _norm_key_part(fields.get("auto_grade")),
    ))
    slab_key = hashlib.sha1(f"{card_key}|{grade}".encode()).hexdigest()[:16]
    return card_key, slab_key
