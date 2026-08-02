"""Step 3A: Tier B identity verification via eBay getItem aspects.

Fetches authoritative item specifics for a SHORTLIST only, stores them beside
the Tier A parsed values (never over them), and decides whether each candidate's
identity is confirmed well enough to be worth an external comp later.

No sold-comp source is contacted here and no valuation is produced.

Endpoint : GET https://api.ebay.com/buy/browse/v1/item/{item_id}
Cost     : 1 request per item. Quota is 5,000 Browse calls/day (application
           level); actual remaining is read from the Analytics API first.
Caching  : an item already fetched with HTTP 200 is never refetched unless
           --refresh is passed, so reruns cost nothing.

Usage:
  python enrich.py --plan              # show shortlist + quota, fetch nothing
  python enrich.py --pilot             # 20 candidates (10 HIGH, 10 unresolved)
  python enrich.py --limit 300         # full shortlist, only after approval
"""

import argparse
import collections
import datetime as dt
import json
import re
import sys

import card_vocab
import db
import ebay_api
import parse
import preview_anomalies as P

HARD_CAP = 300          # never exceed the approved shortlist size
PILOT_SIZE = 20

# The exact aspect names observed in the pilot. eBay supplied these 13 and
# nothing else, so nothing beyond this list may be claimed.
ASPECT_MAP = {
    "grader": ["PROFESSIONAL GRADER"],
    "grade": ["GRADE"],
    "sport": ["SPORT"],
    "set_name": ["SET"],
    "card_number": ["CARD NUMBER"],
    "season": ["SEASON"],
    "parallel": ["PARALLEL/VARIETY"],
    "player": ["PLAYER/ATHLETE"],
    "card_name": ["CARD NAME"],
    "vintage": ["VINTAGE"],
    "graded": ["GRADED"],
    "type": ["TYPE"],
    "country": ["COUNTRY OF ORIGIN"],
}

# getItem did not return these in the pilot. Tier A remains the only source, and
# a Tier A value must never be erased or downgraded because getItem is silent.
NOT_PROVIDED = ("cert_number", "brand", "features", "autographed",
                "auto_grade", "serial_num", "print_run")
NOT_PROVIDED_MARK = "not_provided_by_getItem_in_pilot"

# getItem is authoritative for these; everything else stays with Tier A.
TIER_B_AUTHORITATIVE = ("grader", "grade", "sport", "set_name", "card_number",
                        "season", "parallel")

# Disagreement severity. Only the material class quarantines.
BENIGN = "BENIGN_FORMATTING"
CANONICAL = "CANONICAL_MATCH"
MATERIAL = "MATERIAL_IDENTITY_DISAGREEMENT"
MISSING_B = "MISSING_TIER_B_FIELD"
UNRESOLVED = "UNRESOLVED"

# Explicit product equivalences. Only pairs curated here may compare equal after
# the year/manufacturer prefix is stripped.
PRODUCT_SYNONYMS = {
    "BOWMANS BEST": "BEST",
    "TOPPS CHROME": "CHROME",
    "BOWMAN CHROME": "CHROME",
}

# Token-level abbreviations PSA and eBay use for the same word. Curated, not
# inferred - each pair is a spelling of one concept, never two different ones.
TOKEN_SYNONYMS = {
    "AUTOS": "AUTOGRAPHS", "AUTO": "AUTOGRAPHS", "AUTOGRAPH": "AUTOGRAPHS",
    "PRSPCT": "PROSPECTS", "PROSPECT": "PROSPECTS",
    "REF": "REFRACTOR", "REFRACTORS": "REFRACTOR",
    "SIGS": "SIGNATURES", "SIGNATURE": "SIGNATURES",
    "VARIATION": "VARIATIONS", "VAR": "VARIATIONS", "ED": "EDITION",
}

# Whole-phrase equivalences, applied to the canonical set only. Phrase-level so
# a bare "RC" is never rewritten to "ROOKIE" outside this exact context.
PHRASE_ALIASES = {
    "RC SIGNATURES": "ROOKIE SIGNATURES",
}

# Tokens that may be dropped ONLY to reconcile two otherwise-identical sets, and
# only when every other identity field already agrees. "UEFA CHAMPIONS LEAGUE"
# and "CHAMPIONS LEAGUE" name one competition; the token adds no distinction.
OPTIONAL_SET_TOKENS = {"UEFA"}

# eBay writes sports longhand; Tier A derives them from league tokens.
SPORT_ALIASES = {
    "MIXED MARTIAL ARTS (MMA)": "MMA", "MIXED MARTIAL ARTS": "MMA",
    "AUTO RACING": "RACING", "MOTORSPORTS": "RACING", "FORMULA 1": "RACING",
    "AMERICAN FOOTBALL": "FOOTBALL", "ICE HOCKEY": "HOCKEY",
    "ASSOCIATION FOOTBALL": "SOCCER", "FOOTBALL (SOCCER)": "SOCCER",
}


def canonical_sport(value):
    if not value:
        return None
    v = re.sub(r"\s+", " ", str(value).strip().upper())
    return SPORT_ALIASES.get(v, v)

# "BASE" is our sentinel for "no set span", not a real token.
BASE = "BASE"


def _syn(token):
    return TOKEN_SYNONYMS.get(token, token)


def _mfr_tokens(manufacturer):
    """Every token that may legitimately prefix a set name for this maker."""
    if not manufacturer:
        return set()
    canon = manufacturer.upper()
    toks = set(canon.split())
    for alias, target in parse.MANUFACTURERS.items():
        if target.upper() == canon:
            toks.update(alias.upper().split())
    return toks


def canonical_set(raw, tier_a_year=None, manufacturer=None, season=None):
    """Strip a leading season and manufacturer so set names can be compared.

    "2018 PANINI PRIZM" -> "PRIZM" when the year and maker already agree with
    the separate Tier A fields. Only LEADING tokens are removed, so a set whose
    own name contains a maker word ("DONRUSS OPTIC") survives intact.
    """
    if raw is None:
        return None
    text = re.sub(r"[^\w\s/&'-]", " ", str(raw).upper())
    # Collapse a season span to its opening year BEFORE hyphens become spaces,
    # otherwise "2023-24 TOPPS" strips only "2023" and leaves a stray "24".
    text = re.sub(r"\b((?:18|19|20)\d{2})\s*-\s*(?:\d{4}|\d{2})\b", r"\1", text)
    text = text.replace("'", "").replace("-", " ")
    tokens = [t for t in text.split() if t]

    years = {str(tier_a_year), str(season)} - {"None"}
    mfr = _mfr_tokens(manufacturer)
    while tokens:
        head = tokens[0]
        if head in mfr or re.fullmatch(r"(18|19|20)\d{2}(/\d{2})?", head) and (
                not years or head[:4] in {y[:4] for y in years}):
            tokens.pop(0)
            continue
        break
    canon = " ".join(_syn(t) for t in tokens)
    canon = PRODUCT_SYNONYMS.get(canon, canon)
    for phrase, replacement in PHRASE_ALIASES.items():
        canon = re.sub(rf"\b{re.escape(phrase)}\b", replacement, canon)
    # An empty set span is the flagship base set, which is what Tier A calls BASE.
    return canon or BASE


def canonical_parallel(raw, set_canon=None, manufacturer=None):
    """Normalized token multiset for a parallel, order-insensitive.

    Hyphenation and token order carry no hobby meaning ("GREEN LASER HOLO" is
    "HOLO GREEN LASER"), but the tokens themselves do - so this compares
    multisets and never reorders for display. Product words already carried by
    the set field are dropped, so "SILVER PRIZM" matches "SILVER".
    """
    if raw is None:
        return None
    text = re.sub(r"[^\w\s-]", " ", str(raw).upper()).replace("-", " ")
    drop = set((set_canon or "").split()) | _mfr_tokens(manufacturer)
    tokens = [_syn(t) for t in text.split() if t and _syn(t) not in drop]
    return tuple(sorted(tokens))


def identity_tokens(set_canon, parallel_raw, manufacturer):
    """Combined set+parallel tokens.

    Tier A and Tier B legitimately draw the set/parallel boundary in different
    places - eBay calls ALL-STAR a Parallel/Variety where the title reads it as
    part of the set name. Both describe the same card, and both fields sit in
    the identity key, so the union is what must agree. A genuinely different
    parallel still produces a different union, so this loses no safety.
    """
    tokens = [t for t in (set_canon or "").split() if t != BASE]
    if parallel_raw:
        text = re.sub(r"[^\w\s-]", " ", str(parallel_raw).upper()).replace("-", " ")
        tokens += [t for t in text.split() if t]
    drop = _mfr_tokens(manufacturer)
    # A repeated token carries no extra identity ("PRIZM WNBA" + "WNBA LOGO
    # PRIZM" is one card), so compare distinct tokens. Distinct tokens only -
    # an unmatched token is still fatal.
    return tuple(sorted({_syn(t) for t in tokens if _syn(t) not in drop}))


def name_tokens(*values):
    """Tokens belonging to a person or card title, never to a parallel."""
    out = set()
    for v in values:
        if not v:
            continue
        text = re.sub(r"[^\w\s/-]", " ", str(v).upper()).replace("-", " ")
        out.update(t for t in text.replace("/", " ").split() if t)
    return out


# Surnames observed in our own athlete column, loaded once. Used only to tell a
# multi-player subject apart from a real parallel - never to delete a parallel
# that carries a recognized hobby term.
SURNAMES = set()

PARALLEL_VOCAB = {t.upper() for t in
                  parse.PARALLEL_PHRASES + parse.PARALLEL_COLORS
                  for t in t.split()}


def load_surnames(conn):
    global SURNAMES
    SURNAMES = {r[0] for r in conn.execute(
        "SELECT DISTINCT upper(athlete) FROM cards WHERE athlete IS NOT NULL")}
    SURNAMES = {tok for name in SURNAMES for tok in name.split() if len(tok) > 2}
    return SURNAMES


def is_name_only_parallel(parallel_raw, player, card_name):
    """True when Tier B's Parallel/Variety is really the subject, not a parallel.

    eBay files "JORDAN/WILKINS/MALONE" as Parallel/Variety on a Scoring Leaders
    card. Three requirements keep this narrow: no recognized parallel term may
    appear, every token must be a surname seen in our own data, and the value
    must either be slash-joined or already be the stated player - so a genuine
    parallel like "WNBA LOGO PRIZM" can never be erased.
    """
    if not parallel_raw:
        return False
    text = re.sub(r"[^\w\s/-]", " ", str(parallel_raw).upper()).replace("-", " ")
    toks = {t for t in text.replace("/", " ").split() if t}
    if not toks or toks & PARALLEL_VOCAB:
        return False
    names = name_tokens(player, card_name)
    if toks <= names and names:
        return True
    return bool(SURNAMES) and toks <= SURNAMES and "/" in str(parallel_raw)


def sets_reconcile(a_canon, b_canon):
    """Equal outright, or equal once an optional qualifier token is dropped."""
    if a_canon == b_canon:
        return True, None
    a_t = [t for t in (a_canon or "").split()]
    b_t = [t for t in (b_canon or "").split()]
    a_s = [t for t in a_t if t not in OPTIONAL_SET_TOKENS]
    b_s = [t for t in b_t if t not in OPTIONAL_SET_TOKENS]
    if a_s == b_s and a_t != b_t:
        dropped = sorted(set(a_t) ^ set(b_t))
        return True, f"optional token {dropped} ignored"
    return False, None


def norm(value):
    if value is None or value == "":
        return None
    return re.sub(r"[^A-Z0-9]+", "", str(value).upper()) or None


def extract(raw):
    """Pull canonical fields out of a getItem payload."""
    aspects = {}
    for a in raw.get("localizedAspects") or []:
        name = (a.get("name") or "").strip()
        if name:
            aspects[name] = (a.get("value") or "").strip()
    upper = {k.upper(): v for k, v in aspects.items()}

    out = {"aspects": aspects}
    for field, aliases in ASPECT_MAP.items():
        out[field] = next((upper[a] for a in aliases if a in upper), None)
    # getItem supplies none of these; carry them as explicitly absent so no
    # downstream code mistakes a missing key for a missing value.
    for field in NOT_PROVIDED:
        out[field] = None
    out["year"] = out.get("season")

    ship = (raw.get("shippingOptions") or [{}])[0].get("shippingCost") or {}
    try:
        out["shipping_cost"] = float(ship.get("value"))
    except (TypeError, ValueError):
        out["shipping_cost"] = None
    out["condition"] = raw.get("condition")
    out["buying_options"] = ",".join(raw.get("buyingOptions") or [])
    out["epid"] = raw.get("epid")
    return out


def grade_parts(text):
    """Split a Tier B grade string into (numeric-or-AUTHENTIC, qualifier)."""
    if not text:
        return None, None
    t = str(text).upper()
    m = re.search(r"\b(10|[1-9](?:\.5)?)\b", t)
    q = re.search(r"\b(MC|OC|ST|MK|PD)\b", t)
    if m:
        return m.group(1), (q.group(1) if q else None)
    if "AUTH" in t:
        return "AUTHENTIC", (q.group(1) if q else None)
    return None, (q.group(1) if q else None)


def compare(a, b):
    """Tier A row vs Tier B dict -> (findings, resolved, verdict, reasons).

    Formatting differences are classified, not quarantined. Only a genuine
    identity conflict quarantines. Tier A values survive Tier B silence.
    """
    findings, resolved, reasons = [], {}, []

    # A Parallel/Variety that only restates the player is not a parallel.
    b_par_raw = b.get("parallel")
    name_only = is_name_only_parallel(b_par_raw, b.get("player"), b.get("card_name"))
    b_par_eff = None if name_only else b_par_raw
    if name_only:
        reasons.append(f"Tier B Parallel/Variety {b_par_raw!r} is the player "
                       "name, not a parallel - ignored for identity")

    def record(field, av, bv, severity, note=None, canon=None):
        findings.append({"field": field, "tier_a": av, "tier_b": bv,
                         "severity": severity, "note": note, "canonical": canon})

    def authoritative(field, av, bv):
        if bv not in (None, ""):
            resolved[field] = {"value": bv, "source": "tier_b"}
        else:
            resolved[field] = {"value": av, "source": "tier_a (tier_b silent)"}

    # --- set: compare canonically -----------------------------------------
    a_set, b_set = a["set_name"], b.get("set_name")
    a_canon = canonical_set(a_set, a["year"], a["manufacturer"], b.get("season"))
    b_canon = canonical_set(b_set, a["year"], a["manufacturer"], b.get("season"))
    authoritative("set_name", a_set, b_set)
    resolved["set_canonical"] = {"value": b_canon or a_canon, "source": "canonical"}

    # Same card, different set/parallel split?
    a_ident = identity_tokens(a_canon, a["parallel"], a["manufacturer"])
    b_ident = identity_tokens(b_canon, b_par_eff, a["manufacturer"])
    boundary_shift = a_canon != b_canon and a_ident == b_ident
    resolved["identity_tokens"] = {"value": " ".join(b_ident), "source": "canonical"}

    # An optional set token may only be ignored when everything else lines up.
    b_grade_pre, _q = grade_parts(b.get("grade"))
    # Card number, grade, year and sport must agree; the remaining set tokens
    # are checked by sets_reconcile itself. Parallel is deliberately NOT part of
    # this gate - a Tier B parallel discovery is a re-key, not a set conflict.
    a_sport, b_sport = canonical_sport(a["sport"]), canonical_sport(b.get("sport"))
    other_fields_agree = (
        norm(a["card_number"]) == norm(b.get("card_number"))
        and (b_grade_pre is None or norm(a["grade_value"]) == norm(b_grade_pre))
        and (b.get("season") is None or str(a["year"] or "")[:4] ==
             str(b["season"])[:4])
        and (a_sport is None or b_sport is None or a_sport == b_sport))
    reconciled, recon_note = sets_reconcile(a_canon, b_canon)
    if not reconciled and name_only:
        # Symmetric to the parallel guard: subject names must not create a fake
        # SET either. "1993 HOOPS JORDAN/WILKINS/MALONE" is the Hoops base set;
        # the surnames are who is pictured.
        drop = {t for t in re.sub(r"[^\w\s/-]", " ", str(b_par_raw).upper())
                .replace("-", " ").replace("/", " ").split() if t}
        stripped = " ".join(
            t for t in (a_canon or "").replace("/", " ").split()
            if t not in drop) or BASE
        if stripped == b_canon:
            reconciled = True
            recon_note = (f"Tier A set carried subject names {sorted(drop)}; "
                          "Tier B set is authoritative")

    if b_set is None:
        record("set_name", a_set, None, MISSING_B)
    elif a_canon == b_canon:
        record("set_name", a_set, b_set,
               BENIGN if norm(a_set) == norm(b_set) else CANONICAL,
               canon=b_canon)
    elif reconciled and other_fields_agree:
        record("set_name", a_set, b_set, CANONICAL, recon_note, canon=b_canon)
    elif boundary_shift:
        record("set_name", a_set, b_set, CANONICAL,
               "set/parallel boundary differs; combined identity matches",
               canon=b_canon)
    else:
        record("set_name", a_set, b_set, MATERIAL,
               f"canonical {a_canon!r} != {b_canon!r}", canon=b_canon)

    # --- parallel ----------------------------------------------------------
    a_par, b_par = a["parallel"], b_par_eff
    a_pc = canonical_parallel(a_par, b_canon or a_canon, a["manufacturer"])
    b_pc = canonical_parallel(b_par, b_canon or a_canon, a["manufacturer"])
    if b_par is None and a_par is None:
        # Silence is NOT proof of a base card.
        parallel_state = "tier_a_unresolved"
        resolved["parallel"] = {"value": None, "source": "unresolved"}
        record("parallel", None, None, UNRESOLVED,
               "neither tier states a parallel; absence is not confirmation of base")
    elif b_par is None:
        parallel_state = "tier_b_missing"
        resolved["parallel"] = {"value": a_par,
                                "source": "tier_a (unconfirmed)"}
        record("parallel", a_par, None, MISSING_B,
               "Tier A parallel retained but unconfirmed by getItem")
    elif a_par is None:
        parallel_state = "canonical_match"
        resolved["parallel"] = {"value": b_par, "source": "tier_b (resolved)"}
        record("parallel", None, b_par, CANONICAL, "Tier B resolved the parallel")
    elif a_pc == b_pc:
        parallel_state = "exact" if norm(a_par) == norm(b_par) else "canonical_match"
        resolved["parallel"] = {"value": b_par, "source": "tier_b"}
        record("parallel", a_par, b_par,
               BENIGN if parallel_state == "exact" else CANONICAL,
               canon=" ".join(b_pc))
    elif boundary_shift:
        parallel_state = "canonical_match"
        resolved["parallel"] = {"value": b_par, "source": "tier_b"}
        record("parallel", a_par, b_par, CANONICAL,
               "set/parallel boundary differs; combined identity matches",
               canon=" ".join(b_pc))
    else:
        parallel_state = "material_disagreement"
        resolved["parallel"] = {"value": b_par, "source": "tier_b"}
        record("parallel", a_par, b_par, MATERIAL,
               f"tokens {a_pc} != {b_pc}", canon=" ".join(b_pc))

    # --- card number, grade, sport, season --------------------------------
    b_grade, _ = grade_parts(b.get("grade"))
    for field, av, bv in (("card_number", a["card_number"], b.get("card_number")),
                          ("grade", a["grade_value"], b_grade),
                          ("sport", canonical_sport(a["sport"]), canonical_sport(b.get("sport")))):
        authoritative(field, av, bv)
        if bv in (None, ""):
            record(field, av, bv, MISSING_B)
        elif norm(av) == norm(bv):
            record(field, av, bv, BENIGN)
        elif av is None:
            record(field, av, bv, CANONICAL, "Tier B supplied a missing value")
        else:
            record(field, av, bv, MATERIAL)

    # --- fields getItem never supplies: Tier A stands, undowngraded --------
    for field, av in (("grade_qualifier", a["grade_qualifier"]),
                      ("auto_grade", a["auto_grade"]),
                      ("print_run", a["print_run"]),
                      ("serial_num", a["serial_num"])):
        resolved[field] = {"value": av, "source": f"tier_a ({NOT_PROVIDED_MARK})"}

    # --- grader ------------------------------------------------------------
    grader = (b.get("grader") or "").upper()
    grader_bad = bool(grader) and "PSA" not in grader and \
        "PROFESSIONAL SPORTS" not in grader

    # --- verdict -----------------------------------------------------------
    material = [f for f in findings if f["severity"] == MATERIAL]
    if grader_bad:
        verdict = "quarantined"
        reasons.append(f"Tier B grader is {b.get('grader')!r}, not PSA")
    elif material:
        verdict = "quarantined"
        reasons += [f"material disagreement on {f['field']}" for f in material]
    elif parallel_state in ("tier_a_unresolved", "tier_b_missing"):
        ok, why = base_compatible(
            a, b, a["title"] if "title" in a.keys() else None)
        if ok and parallel_state == "tier_a_unresolved":
            verdict = "verified"
            reasons.append(f"confirmed base: {why}")
        else:
            verdict = "held_for_parallel_resolution"
            reasons.append(f"parallel {parallel_state}"
                           + ("" if ok else f"; {why}"))
    elif any(f["severity"] == MISSING_B for f in findings):
        verdict = "verified_with_missing_fields"
        reasons.append("some Tier B fields absent")
    else:
        verdict = "verified"

    for field in NOT_PROVIDED:
        reasons.append(f"{field}: {NOT_PROVIDED_MARK}")
    return findings, resolved, verdict, reasons


def score_all(conn):
    """Re-score every cached Tier B row from stored aspects. No API calls."""
    conn.execute("UPDATE tierb SET original_verdict = verdict "
                 "WHERE original_verdict IS NULL")
    conn.commit()
    rows = conn.execute("""
        SELECT t.item_id, t.original_verdict, t.aspects_json, t.fetched_at,
               c.*, l.title, l.price, l.shipping_cost
        FROM tierb t JOIN cards c USING (item_id) JOIN listings l USING (item_id)
        WHERE t.http_status = 200
    """).fetchall()
    out = []
    for r in rows:
        aspects = json.loads(r["aspects_json"] or "{}")
        upper = {k.upper(): v for k, v in aspects.items()}
        b = {"aspects": aspects}
        for field, aliases in ASPECT_MAP.items():
            b[field] = next((upper[a] for a in aliases if a in upper), None)
        for field in NOT_PROVIDED:
            b[field] = None
        findings, resolved, verdict, reasons = compare(r, b)
        conn.execute("UPDATE tierb SET verdict=?, disagreements=?, resolved_json=? "
                     "WHERE item_id=?",
                     (verdict, json.dumps(findings), json.dumps(resolved),
                      r["item_id"]))
        out.append({"row": r, "b": b, "findings": findings, "verdict": verdict,
                    "original": r["original_verdict"], "aspects": aspects})
    conn.commit()
    return out


def shortlist_stats(conn, ids):
    """Counts the pre-flight print-out must show before any request goes out."""
    groups, _ = P.eligible_groups(conn)
    strong = {c["item_id"]: c for c in P.build_candidates(groups)}
    weak = {c["item_id"]: c for c in P.build_candidates(groups, exact_size=P.WEAK_GROUP)}
    rows = {r["item_id"]: r for r in conn.execute(
        "SELECT * FROM cards WHERE item_id IN (%s)" % ",".join("?" * len(ids)),
        list(ids))}
    s = collections.Counter()
    for i in ids:
        c = strong.get(i) or weak.get(i)
        r = rows.get(i)
        if not c or not r:
            continue
        s["high" if c["parallel_conf"] == parse.HIGH else "unresolved"] += 1
        s["size2" if c["size"] == 2 else "size3plus"] += 1
        s["numbered"] += r["print_run"] is not None
        s["autograph"] += r["auto_grade"] is not None
        s["qualifier"] += r["grade_qualifier"] is not None
    return s


def select_shortlist(conn, total, weak_target=10):
    """Build the shortlist, always seeded with everything already cached.

    Seeding with the cache guarantees new requests = total - cached, so the
    number printed in the pre-flight is the number actually spent.
    """
    groups, _ = P.eligible_groups(conn)
    strong = P.build_candidates(groups)
    weak = P.build_candidates(groups, exact_size=P.WEAK_GROUP)
    all_c = {c["item_id"]: c for c in strong + weak}
    rows = {r["item_id"]: r for r in conn.execute(
        "SELECT * FROM cards WHERE item_id IN (%s)" % ",".join("?" * len(all_c)),
        list(all_c))}
    cached = [r[0] for r in conn.execute(
        "SELECT item_id FROM tierb WHERE http_status = 200 ORDER BY item_id")]

    chosen = [i for i in cached if i in all_c]
    seen_slab = {rows[i]["slab_key"] for i in chosen if i in rows}
    covered = collections.Counter()

    def categories(r):
        return [c for c, on in (("qualifier", r["grade_qualifier"]),
                                ("auto", r["auto_grade"]),
                                ("numbered", r["print_run"]),
                                ("rookie", r["is_rookie"])) if on]

    def fill(pool, want, cap_cat=None):
        for cat in ("qualifier", "auto", "numbered", "rookie", None):
            for c in pool:
                iid = c["item_id"]
                if len(chosen) >= want:
                    return
                r = rows.get(iid)
                if r is None or iid in chosen or r["slab_key"] in seen_slab:
                    continue
                cats = categories(r)
                if cat is not None and (cat not in cats
                                        or covered[cat] >= (cap_cat or 12)):
                    continue
                chosen.append(iid)
                seen_slab.add(r["slab_key"])
                for x in cats:
                    covered[x] += 1

    weak_have = sum(1 for i in chosen if i in {c["item_id"] for c in weak})
    fill(weak, len(chosen) + max(0, weak_target - weak_have))
    fill(strong, total)
    return chosen[:total]


def select_pilot(conn, n_high, n_unres):
    """Stratified pilot: HIGH-parallel vs unresolved, seeded with the awkward
    cases (numbered, autograph, qualifier, rookie)."""
    groups, _ = P.eligible_groups(conn)
    cands = {c["item_id"]: c for c in P.build_candidates(groups)}
    rows = {r["item_id"]: r for r in conn.execute(
        "SELECT * FROM cards WHERE item_id IN (%s)" % ",".join("?" * len(cands)),
        list(cands))}

    def categories(r):
        return [c for c, on in (("qualifier", r["grade_qualifier"]),
                                ("auto", r["auto_grade"]),
                                ("numbered", r["print_run"]),
                                ("rookie", r["is_rookie"])) if on]

    def pick(pool, want):
        """Cover each awkward category first, then fill; one item per slab."""
        chosen, seen_slab, covered = [], set(), collections.Counter()
        # Rarest categories first so a 6-listing qualifier pool is not crowded out.
        for cat in ("qualifier", "auto", "numbered", "rookie", None):
            for iid in pool:
                if len(chosen) >= want:
                    break
                r = rows[iid]
                if r["slab_key"] in seen_slab:
                    continue
                cats = categories(r)
                if cat is not None and (cat not in cats or covered[cat] >= 2):
                    continue
                chosen.append(iid)
                seen_slab.add(r["slab_key"])
                for c in cats:
                    covered[c] += 1
        return chosen

    high = [i for i, c in cands.items()
            if i in rows and c["parallel_conf"] == parse.HIGH]
    unres = [i for i, c in cands.items()
             if i in rows and c["parallel_conf"] != parse.HIGH]
    return pick(high, n_high) + pick(unres, n_unres)


def store(conn, item_id, status, raw, tier_a, fetched_at):
    if status != 200:
        # Record the failure without touching any prior successful row.
        conn.execute(
            """INSERT INTO tierb (item_id, fetched_at, http_status, verdict)
               VALUES (?,?,?,'fetch_failed')
               ON CONFLICT(item_id) DO UPDATE SET
                 fetched_at=excluded.fetched_at, http_status=excluded.http_status
               WHERE tierb.http_status IS NOT 200""",
            (item_id, fetched_at, status))
        conn.commit()
        return None, None, "fetch_failed", [f"HTTP {status}"]

    b = extract(raw)
    dis, resolved, verdict, reasons = compare(tier_a, b)
    conn.execute(
        """INSERT OR REPLACE INTO tierb
           (item_id, fetched_at, http_status, grader, grade, cert_number, sport,
            year, brand, set_name, card_number, parallel, features, autographed,
            auto_grade, serial_num, print_run, condition, shipping_cost,
            buying_options, epid, aspects_json, resolved_json, disagreements,
            verdict)
           VALUES (?,?,200,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (item_id, fetched_at, b["grader"], b["grade"], b["cert_number"],
         b["sport"], b["year"], b["brand"], b["set_name"], b["card_number"],
         b["parallel"], b["features"], b["autographed"], b["auto_grade"],
         b["serial_num"], b["print_run"], b["condition"], b["shipping_cost"],
         b["buying_options"], b["epid"], json.dumps(b["aspects"]),
         json.dumps(resolved), json.dumps(dis), verdict))
    conn.commit()
    return b, dis, verdict, reasons


def stage_report(conn, stats, results, quota_before, quota_after):
    print("\n" + "=" * 78)
    print("  TIER B STAGE REPORT")
    print("=" * 78)
    print(f"  Requests attempted   : {stats['attempted']}")
    print(f"  Successful (200)     : {stats['ok']}")
    print(f"  Failed (non-200)     : {stats['failed']}   of which 404: {stats['404']}")
    print(f"  Skipped from cache   : {stats['cached']}")
    print(f"  Quota before / after : {quota_before} / {quota_after}")
    if quota_before is not None and quota_after is not None:
        delta = quota_before - quota_after
        if delta != stats["attempted"]:
            print(f"  QUOTA DELTA MISMATCH : Analytics moved {delta} but "
                  f"{stats['attempted']} HTTP requests were observed locally.")
            print("                         Local count is authoritative; the "
                  "Analytics figure is DELAYED/UNCERTAIN.")
    print(f"  Rows scored (total)  : {len(results)}")

    names = collections.Counter()
    for r in results:
        names.update(r["aspects"].keys())
    n = max(len(results), 1)
    print("\n  Aspect coverage:")
    for field in ASPECT_MAP:
        c = sum(1 for r in results if r["b"].get(field))
        print(f"    {field:14} {c:4}/{n}  ({100*c/n:5.1f}%)")
    print("\n  Fields unavailable from getItem (Tier A remains sole source):")
    for field in NOT_PROVIDED:
        print(f"    {field:14} {NOT_PROVIDED_MARK}")

    v = collections.Counter(r["verdict"] for r in results)
    print("\n  Verdicts:")
    for k in ("verified", "verified_with_missing_fields",
              "held_for_parallel_resolution", "quarantined"):
        print(f"    {k:32} {v.get(k, 0)}")

    mat = collections.Counter(); canon = collections.Counter()
    for r in results:
        for f in r["findings"]:
            if f["severity"] == MATERIAL:
                mat[f["field"]] += 1
            elif f["severity"] == CANONICAL:
                canon[f["field"]] += 1
    print("\n  True material disagreements by field:")
    print("   ", dict(mat) or "none")
    print("  Canonical (formatting-only) matches by field:")
    print("   ", dict(canon) or "none")

    graders = collections.Counter(r["b"].get("grader") for r in results)
    bad = {g: c for g, c in graders.items()
           if g and "PSA" not in g.upper() and "PROFESSIONAL SPORTS" not in g.upper()}
    print(f"\n  Grader conflicts     : {sum(bad.values())} {bad or ''}")

    certs = [r["b"].get("cert_number") for r in results if r["b"].get("cert_number")]
    dup = [c for c, k in collections.Counter(certs).items() if k > 1]
    print(f"  Certification numbers: {len(certs)} present"
          f"{' - DUPLICATES: ' + ', '.join(dup[:5]) if dup else ''}")

    print("\n  Observed aspect names and frequencies:")
    for name, c in names.most_common():
        print(f"    {c:4}x  {name}")

    prevented = [r for r in results if r["verdict"] == "quarantined"]
    print(f"\n  Tier B PREVENTED a false candidate ({len(prevented)}):")
    for r in prevented:
        row, b = r["row"], r["b"]
        reasons = [f for f in r["findings"] if f["severity"] == MATERIAL]
        print(f"\n    item {row['item_id']}  slab {row['slab_key']}")
        print(f"      {row['title'][:70]}")
        print(f"      group identity: {row['year']} {row['set_name']!r} "
              f"#{row['card_number']} PSA {row['grade_value']} "
              f"parallel={row['parallel']!r}")
        print(f"      Tier B: set={b.get('set_name')!r} "
              f"parallel={b.get('parallel')!r} #={b.get('card_number')!r} "
              f"grade={b.get('grade')!r} grader={b.get('grader')!r}")
        if reasons:
            for f in reasons:
                print(f"      MATERIAL {f['field']}: A={f['tier_a']!r} "
                      f"B={f['tier_b']!r}  {f.get('note') or ''}")
        else:
            print("      MATERIAL: rival grader")

    held = [r for r in results if r["verdict"] == "held_for_parallel_resolution"]
    print(f"\n  Tier B CANNOT resolve identity ({len(held)}) - probable-base /")
    print("  unresolved variety, NOT confirmed base:")
    for r in held[:10]:
        print(f"    {r['row']['item_id']}  {r['row']['title'][:62]}")
    if len(held) > 10:
        print(f"    ... and {len(held) - 10} more")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--limit", type=int, default=PILOT_SIZE,
                    help="TOTAL shortlist size, cached items included")
    ap.add_argument("--plan", action="store_true", help="show plan, fetch nothing")
    ap.add_argument("--refresh", action="store_true",
                    help="refetch cached items (otherwise they are frozen)")
    ap.add_argument("--weak", type=int, default=10,
                    help="how many size-2 weak-signal candidates to include")
    args = ap.parse_args()

    if args.limit > HARD_CAP:
        sys.exit(f"error: {args.limit} exceeds the approved cap of {HARD_CAP}")

    conn = db.connect(args.db)
    shortlist = select_shortlist(conn, args.limit, args.weak)
    cached = {r[0] for r in conn.execute(
        "SELECT item_id FROM tierb WHERE http_status = 200")}
    todo = shortlist if args.refresh else [i for i in shortlist if i not in cached]
    expected = len(todo)
    s = shortlist_stats(conn, shortlist)

    print("=" * 78)
    print("  TIER B ENRICHMENT PLAN")
    print("=" * 78)
    print(f"  Endpoint             : GET {ebay_api.ITEM_URL}")
    print(f"  Shortlist size       : {len(shortlist)}  (cap {HARD_CAP})")
    print(f"  Already cached       : {len(shortlist) - expected}"
          f"{' (frozen; --refresh not given)' if not args.refresh else ' (WILL REFETCH)'}")
    print(f"  New requests expected: {expected}")
    print(f"  HIGH parallel conf   : {s['high']}")
    print(f"  parallel_unresolved  : {s['unresolved']}")
    print(f"  numbered             : {s['numbered']}")
    print(f"  autograph            : {s['autograph']}")
    print(f"  qualifier            : {s['qualifier']}")
    print(f"  size-2 / size-3+     : {s['size2']} / {s['size3plus']}")

    try:
        token = ebay_api.get_token()
    except ebay_api.EbayError as exc:
        if args.plan:
            print(f"  Quota                : not checked ({exc})")
            return
        sys.exit(f"error: {exc}")

    q_before_at = dt.datetime.now(dt.timezone.utc).isoformat()
    limits, err = ebay_api.get_rate_limit(token)
    print(f"  Quota read at        : {q_before_at}")
    quota_before = None
    if limits:
        for l in limits[:4]:
            print(f"  Quota [{l['resource']}]  : {l['remaining']}/{l['limit']} "
                  f"remaining, resets {l['reset']}")
        quota_before = ebay_api.browse_quota(limits)
    else:
        print(f"  Quota                : UNREADABLE ({err})")

    if quota_before is not None and quota_before < expected:
        sys.exit(f"ABORT: quota {quota_before} < {expected} expected requests")
    if len(shortlist) > args.limit or expected > args.limit:
        sys.exit(f"ABORT: shortlist {len(shortlist)} / expected {expected} "
                 f"exceeds the requested limit {args.limit}")
    if not args.refresh and set(todo) & cached:
        sys.exit("ABORT: cached items would be refetched without --refresh")
    if args.plan:
        print("\n  --plan given: nothing fetched.")
        return

    rows = {r["item_id"]: r for r in conn.execute(
        "SELECT * FROM cards WHERE item_id IN (%s)" % ",".join("?" * len(shortlist)),
        shortlist)}
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    stats = collections.Counter({"cached": len(shortlist) - expected})
    print(f"\n  fetching {expected} items...")
    for n, iid in enumerate(todo, 1):
        status, raw = ebay_api.get_item(token, iid)
        stats["attempted"] += 1
        if status == 200:
            stats["ok"] += 1
        else:
            stats["failed"] += 1
            stats["404"] += status == 404
        store(conn, iid, status, raw, rows[iid], fetched_at)
        if n % 10 == 0 or status != 200:
            print(f"    [{n}/{expected}] {iid} -> HTTP {status}")

    q_after_at = dt.datetime.now(dt.timezone.utc).isoformat()
    after, _ = ebay_api.get_rate_limit(token)
    print(f"  Quota re-read at     : {q_after_at}")
    quota_after = None
    if after:
        quota_after = ebay_api.browse_quota(after)

    results = score_all(conn)
    stage_report(conn, stats, results, quota_before, quota_after)


if __name__ == "__main__":
    main()


def effective_fields(a, b):
    """Tier A row + Tier B authoritative overrides = the identity to group on.

    Tier B replaces set, parallel, card number, grade and sport. Tier A-only
    facts (qualifier, autograph grade, serial, print run) are carried through
    untouched - getItem never supplies them, so its silence proves nothing.
    """
    b_par_raw = b.get("parallel")
    b_par = None if is_name_only_parallel(
        b_par_raw, b.get("player"), b.get("card_name")) else b_par_raw
    b_grade, _ = grade_parts(b.get("grade"))
    b_set_canon = canonical_set(b.get("set_name"), a["year"], a["manufacturer"],
                                b.get("season"))
    a_set_canon = canonical_set(a["set_name"], a["year"], a["manufacturer"])
    if b_par_raw and not b_par:  # subject names, not a parallel
        drop = {t for t in re.sub(r"[^\w\s/-]", " ", str(b_par_raw).upper())
                .replace("-", " ").replace("/", " ").split() if t}
        a_set_canon = " ".join(
            t for t in (a_set_canon or "").replace("/", " ").split()
            if t not in drop) or BASE

    return {
        # Tier B authoritative
        "sport": canonical_sport(b.get("sport")) or canonical_sport(a["sport"]),
        "set_name": b_set_canon if b.get("set_name") else a_set_canon,
        "parallel": b_par if b_par else a["parallel"],
        "card_number": b.get("card_number") or a["card_number"],
        "grade_value": b_grade or a["grade_value"],
        "grade_type": a["grade_type"],
        "year": a["year"],
        # Tier A only - never erased by Tier B silence
        "manufacturer": a["manufacturer"], "insert_name": a["insert_name"],
        "athlete": a["athlete"], "is_auto": a["is_auto"], "is_relic": a["is_relic"],
        "print_run": a["print_run"], "serial_num": a["serial_num"],
        "grade_qualifier": a["grade_qualifier"], "auto_grade": a["auto_grade"],
        "_parallel_confirmed": bool(b_par),
        "_parallel_name_only": bool(b_par_raw) and not b_par,
    }


def identity_string(f):
    return (f"{f.get('year')}|{f.get('manufacturer')}|{f.get('set_name')}|"
            f"{f.get('parallel')}|#{f.get('card_number')}|"
            f"{f.get('grade_type')}:{f.get('grade_value')}"
            f"{'+' + f['grade_qualifier'] if f.get('grade_qualifier') else ''}"
            f"{'+AUTO' + f['auto_grade'] if f.get('auto_grade') else ''}"
            f"{'|/' + str(f['print_run']) if f.get('print_run') else ''}|"
            f"{f.get('sport') or 'SPORT?'}")


# Core identity Tier B must supply before a base classification is possible.
CORE_TIER_B_FIELDS = ("player", "set_name", "card_number", "grade", "grader")

# Aspects that describe the listing rather than the card's variety.
NON_PARALLEL_ASPECTS = {"Vintage", "Graded", "Type", "Country of Origin",
                        "Card Name", "Player/Athlete", "Professional Grader",
                        "Sport", "Season", "Grade", "Card Number"}

# Words that are expected in any title and cannot hint at a parallel.
_BASE_NEUTRAL = {"RC", "ROOKIE", "PSA", "GEM", "MT", "MINT", "NM", "CARD",
                 "TRADING", "GRADED", "SP", "HOF", "AUTO", "AUTOGRAPH",
                 "AUTOGRAPHS", "SIGNED", "AUTHENTIC", "ALTERED",
                 "MC", "OC", "ST", "MK", "PD", "OF"}


def base_compatible(a, b, title):
    """Affirmative evidence that a card is the BASE card.

    This is NOT "no parallel field means base". It requires a complete Tier B
    core identity, silence about parallels in BOTH the title and every aspect
    value, no parallel term hiding in the set/product name, no serial or print
    run (a numbered card implies a parallel), and no unexplained token that
    could itself be a parallel.

    Returns (ok, reason).
    """
    # 1. Tier B core identity must be complete.
    missing = [f for f in CORE_TIER_B_FIELDS if not b.get(f)]
    if missing:
        return False, f"core identity incomplete in Tier B: {missing}"

    # 4. Serial numbering or a print run implies a numbered parallel.
    if a["print_run"] or a["serial_num"]:
        return False, ("serial/print-run evidence implies a numbered parallel "
                       f"(serial={a['serial_num']}, run={a['print_run']})")

    # 2a. The complete title must be available and state no parallel term.
    if not title:
        return False, "listing title unavailable; cannot verify parallel silence"
    title_tokens = card_vocab.tokens_of(parse.normalize(title))
    in_title = sorted(title_tokens & card_vocab.TRUE_PARALLEL)
    if in_title:
        return False, f"title states parallel term(s) {in_title}"

    # 2b. No aspect value may state one, and Parallel/Variety must be absent.
    if b.get("parallel"):
        return False, f"Tier B states Parallel/Variety {b['parallel']!r}"
    for name, value in (b.get("aspects") or {}).items():
        if name in NON_PARALLEL_ASPECTS or str(value).strip().upper() in ("YES", "NO"):
            continue          # booleans and descriptive aspects carry no parallel
        hit = sorted(card_vocab.tokens_of(str(value).upper())
                     & card_vocab.TRUE_PARALLEL)
        if hit:
            return False, f"aspect {name!r} states parallel term(s) {hit}"

    # 3. No parallel term inside the normalized set/product identity.
    for label, value in (("tier_a set", a["set_name"]),
                         ("tier_b set", b.get("set_name"))):
        hit = sorted(card_vocab.tokens_of(str(value or "").upper())
                     & card_vocab.TRUE_PARALLEL)
        if hit:
            return False, f"{label} contains parallel term(s) {hit}"

    # 3b. Any token the identity cannot account for might itself be a parallel.
    accounted = set()
    for value in (a["set_name"], b.get("set_name"), a["manufacturer"],
                  a["athlete"], b.get("player"), b.get("card_name"),
                  str(a["year"] or ""), str(b.get("season") or ""),
                  str(a["card_number"] or ""), str(b.get("card_number") or ""),
                  str(a["grade_value"] or ""), b.get("sport"), b.get("type")):
        accounted |= card_vocab.tokens_of(str(value or "").upper())
    # Manufacturer aliases as written in titles ("UD" for UPPER DECK).
    accounted |= _mfr_tokens(a["manufacturer"])
    accounted |= _BASE_NEUTRAL | card_vocab.IGNORABLE
    unknown = sorted(t for t in title_tokens
                     if t not in accounted and not t.isdigit())
    if unknown:
        return False, f"unexplained token(s) {unknown} could be a parallel"

    return True, "structured base-compatible identity"


def substantive_rekey(a, eff):
    """Did the CARD identity change, ignoring sport being filled in?

    Tier A knows the sport for under 10% of listings and Tier B for all of
    them, so including sport would mark almost every listing as re-keyed. Sport
    resolution is an enrichment gain that applies equally to every peer, not a
    reason to eject a listing from its group.
    """
    probe = dict(eff)
    probe["sport"] = a["sport"]
    return parse.make_keys(probe)[1] != a["slab_key"]


def classify(a, b, findings, eff, eff_slab):
    """Final Stage-1 classification for one enriched listing."""
    material = [f for f in findings if f["severity"] == MATERIAL]
    grader = (b.get("grader") or "").upper()
    if grader and "PSA" not in grader and "PROFESSIONAL SPORTS" not in grader:
        return "quarantined_material_conflict"
    if material:
        return "quarantined_material_conflict"
    if substantive_rekey(a, eff):
        return "identity_rekey_required"
    if not eff["_parallel_confirmed"] and eff["parallel"] is None:
        # Both tiers silent on the parallel. Silence alone is not proof of a
        # base card, but affirmative structured evidence is - same rule and
        # same helper `compare()` applies to its verdict.
        ok, _why = base_compatible(a, b, a["title"] if "title" in a.keys()
                                   else None)
        if not ok:
            return "held_for_parallel_resolution"
    if any(f["severity"] == MISSING_B for f in findings):
        return "verified_with_missing_fields"
    return "verified"
