"""Semantic roles for the words that appear in card titles.

Titles mix several kinds of word, and only some of them identify the card:

    manufacturer   PANINI, TOPPS, UPPER DECK
    set/product    PRIZM, CONTENDERS, HOOPS, OPTIC, STAR ROOKIE
    true parallel  SILVER, RED, REFRACTOR, RUBY, WAVE, ICE
    team/city      MARINERS, SEATTLE, LAKERS, SUNS
    league/sport   NBA, NFL, BASEBALL, BASKETBALL
    accolade       HOF
    generic        CARD, TRADING, GRADED, GEM, MINT, RC

Only a conflict in the authoritative set or parallel may reject a comp. A seller
adding "Seattle Mariners" or "NBA Basketball" to a title says nothing about
which card it is.

Kept deliberately conservative: a word is only treated as ignorable context when
it cannot plausibly be a set or parallel name. STAR stays out of the accolade
list because "Star Rookie" is a real subset; MVP stays out because "1992 UD MVP"
is a real set.
"""

import re

import parse

# --- true parallels ------------------------------------------------------
# Colour and finish words that genuinely distinguish one card from another.
# Product/set names that appear INSIDE parallel phrases ("SHOCK PRIZM") and
# would otherwise be mistaken for parallels in their own right.
PRODUCT_WORDS = {
    "PRIZM", "PRIZMS", "CHROME", "OPTIC", "MOSAIC", "SELECT", "CONTENDERS",
    "HOOPS", "FINEST", "HERITAGE", "DONRUSS", "SPECTRA", "REVOLUTION",
    "OBSIDIAN", "CERTIFIED", "ABSOLUTE", "IMMACULATE", "CHRONICLES", "BOWMAN",
    "TOPPS", "PANINI", "SET", "EDITION", "HUDDLE", "BREAK",
    # "NO" arrives via the phrase "NO HUDDLE" and is meaningless alone - it
    # matched the boolean aspect value Vintage="No".
    "NO", "YES",
}

TRUE_PARALLEL = {t.upper() for phrase in
                 (parse.PARALLEL_PHRASES + parse.PARALLEL_COLORS)
                 for t in phrase.split()} - PRODUCT_WORDS

# --- league / sport ------------------------------------------------------
LEAGUE_SPORT = {
    "NBA", "NFL", "MLB", "NHL", "WNBA", "UFC", "MLS", "FIFA", "UEFA", "NCAA",
    "PGA", "ATP", "WTA", "NASCAR", "WWE", "NPB", "KBO", "AFL", "CFL",
    "BASEBALL", "BASKETBALL", "FOOTBALL", "HOCKEY", "SOCCER", "GOLF", "TENNIS",
    "BOXING", "RACING", "WRESTLING", "MMA", "CRICKET", "OLYMPICS", "OLYMPIC",
}

# --- team / city ---------------------------------------------------------
TEAM_CITY = {
    # cities / regions
    "SEATTLE", "PHOENIX", "BOSTON", "CHICAGO", "DETROIT", "HOUSTON", "DALLAS",
    "DENVER", "MIAMI", "ATLANTA", "ORLANDO", "PORTLAND", "SACRAMENTO",
    "MINNESOTA", "MILWAUKEE", "MEMPHIS", "INDIANA", "TORONTO", "BROOKLYN",
    "PHILADELPHIA", "WASHINGTON", "CLEVELAND", "CHARLOTTE", "OKLAHOMA",
    "SAN", "LOS", "NEW", "YORK", "ANGELES", "FRANCISCO", "ANTONIO", "DIEGO",
    "GOLDEN", "STATE", "TAMPA", "BAY", "KANSAS", "CITY", "ST", "SAINT",
    "LOUIS", "PITTSBURGH", "CINCINNATI", "BALTIMORE", "ARIZONA", "COLORADO",
    "TEXAS", "FLORIDA", "CAROLINA", "ENGLAND", "JERSEY", "ORLEANS", "VEGAS",
    "UTAH", "OREGON", "MICHIGAN", "BUFFALO", "GREEN", "TENNESSEE",
    # team names
    "MARINERS", "LAKERS", "SUNS", "BULLS", "CELTICS", "KNICKS", "NETS",
    "HEAT", "MAGIC", "PACERS", "PISTONS", "BUCKS", "CAVALIERS", "HAWKS",
    "HORNETS", "WIZARDS", "RAPTORS", "SIXERS", "76ERS", "WARRIORS", "CLIPPERS",
    "KINGS", "BLAZERS", "TRAIL", "NUGGETS", "JAZZ", "THUNDER", "SPURS",
    "ROCKETS", "MAVERICKS", "GRIZZLIES", "PELICANS", "TIMBERWOLVES",
    "YANKEES", "REDSOX", "DODGERS", "GIANTS", "CUBS", "METS", "PHILLIES",
    "BRAVES", "ASTROS", "RANGERS", "ANGELS", "PADRES", "ROCKIES", "TWINS",
    "TIGERS", "ROYALS", "GUARDIANS", "INDIANS", "WHITE", "SOX", "BREWERS",
    "REDS", "PIRATES", "NATIONALS", "MARLINS", "RAYS", "ATHLETICS", "DIAMONDBACKS",
    "PATRIOTS", "PACKERS", "COWBOYS", "STEELERS", "EAGLES", "49ERS", "CHIEFS",
    "RAVENS", "BENGALS", "BROWNS", "BILLS", "DOLPHINS", "JETS", "TITANS",
    "COLTS", "JAGUARS", "TEXANS", "BRONCOS", "RAIDERS", "CHARGERS", "VIKINGS",
    "LIONS", "BEARS", "SAINTS", "BUCCANEERS", "FALCONS", "SEAHAWKS",
    "COMMANDERS", "CARDINALS", "RAMS",
}

# --- accolades -----------------------------------------------------------
# Only words that cannot double as a set name.
ACCOLADE = {"HOF", "HOFER", "GOAT", "LEGEND", "LEGENDS"}

# --- generic listing language -------------------------------------------
GENERIC = {
    "CARD", "CARDS", "TRADING", "GRADED", "GRADE", "PSA", "GEM", "MT", "MINT",
    "NM", "RC", "ROOKIE", "ROOKIES", "SP", "INVESTMENT", "INVEST", "RARE",
    "CENTERED", "SHARP", "BEAUTIFUL", "VINTAGE", "HOT", "LOOK", "NICE",
    "ON", "OF", "THE", "AND", "WITH", "IN", "FOR", "A", "AT", "TO",
}

# Roles that never identify a card, so an extra or missing one is not a
# conflict. Set/product words are NOT here - they still have to line up.
IGNORABLE = LEAGUE_SPORT | TEAM_CITY | ACCOLADE | GENERIC

EXTRA_PARALLEL = "extra_parallel"
MISSING_PARALLEL = "missing_parallel"


def role(token):
    """Best-effort semantic role for one uppercase token."""
    t = (token or "").upper()
    if t in TRUE_PARALLEL:
        return "parallel"
    if t in LEAGUE_SPORT:
        return "league_sport"
    if t in TEAM_CITY:
        return "team_city"
    if t in ACCOLADE:
        return "accolade"
    if t in GENERIC:
        return "generic"
    if t in {a.upper() for a in parse.MANUFACTURERS}:
        return "manufacturer"
    return "set_product"


def tokens_of(text):
    return {t for t in re.split(r"[^A-Z0-9]+", (text or "").upper()) if t}


def is_ignorable(token):
    return (token or "").upper() in IGNORABLE


def parallel_conflict(cand_tokens, comp_title_tokens, synonym=None):
    """Compare two token sets by role. Returns (kind, tokens) or (None, []).

    - a TRUE PARALLEL word the comp states and the candidate does not
      -> EXTRA_PARALLEL, a genuine different-parallel conflict
    - a TRUE PARALLEL word the candidate states and the comp never mentions
      -> MISSING_PARALLEL, absent evidence rather than a contradiction
    - anything else (team, city, league, sport, accolade, generic, or a set
      word simply worded differently) -> no conflict
    """
    syn = synonym or (lambda t: t)
    cand = {syn(t) for t in cand_tokens if not is_ignorable(t)}
    comp = {syn(t) for t in comp_title_tokens if not is_ignorable(t)}

    extra = sorted(t for t in comp - cand if t in TRUE_PARALLEL)
    if extra:
        return EXTRA_PARALLEL, extra
    missing = sorted(t for t in cand - comp if t in TRUE_PARALLEL)
    if missing:
        return MISSING_PARALLEL, missing
    return None, []
