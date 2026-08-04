"""Freeze the valuation-ready candidates to a simple JSON file.

Deliberately minimal: one reproducible export, re-runnable at any time from the
cache. No snapshot history, no lifecycle status, no fingerprinting.

Usage:  python export_pilot.py [--out pilot_candidates.json]
"""

import argparse
import json

import db
import enrich
import parse
import peers


def build(conn, selection_log=None):
    """Freeze one candidate per exact slab identity.

    `selection_log`, when given, receives one entry per group recording which
    member was chosen, which member identity provenance would have chosen, and
    why every other member was rejected.
    """
    enrich.load_surnames(conn)
    groups = peers.affected_groups(conn)
    members = peers.group_members(conn, list(groups))
    by_group = {}
    for m in members:
        by_group.setdefault(m["slab_key"], []).append(m)
    eff = peers.effective_for(conn, [m["item_id"] for m in members])

    out, seen_identities = [], set()
    log = selection_log if selection_log is not None else []
    for slab, ms in by_group.items():
        provenance_id = groups[slab]
        ids = [m["item_id"] for m in ms]
        if peers.classify_group(ids, eff, provenance_id) != "confirmed_same_identity":
            continue
        group_slab = eff[provenance_id]["eff_slab"]
        # One candidate per exact slab identity: two original slab groups can
        # resolve to the same effective identity, and researching both would
        # spend the expensive stage twice on one card.
        if group_slab in seen_identities:
            continue
        # Identity provenance settled the group. It does not get to pick the
        # member: a candidate is the cheapest fully eligible listing of the
        # exact same slab, by complete acquisition_total.
        cand_id, cand_total, rejected = peers.cheapest_eligible(ms, eff, group_slab)
        if cand_id is None:
            log.append({"slab": group_slab, "selected": None,
                        "rejected": rejected})
            continue
        seen_identities.add(group_slab)
        log.append({
            "slab": group_slab, "selected": cand_id,
            "acquisition_total": cand_total,
            "provenance_representative": provenance_id,
            "changed": cand_id != provenance_id, "rejected": rejected})
        e = eff[cand_id]
        f, row = e["eff"], e["row"]
        out.append({
            "item_id": cand_id,
            "acquisition_total": cand_total,
            "title": row["title"],
            "asking_price": row["price"],
            "shipping": row["shipping_cost"],
            "effective_identity": enrich.identity_string(f),
            "effective_card_key": parse.make_keys(f)[0],
            "effective_slab_key": e["eff_slab"],
            "year": f["year"],
            "manufacturer": f["manufacturer"],
            "set": f["set_name"],
            "subject": f["athlete"],
            "card_number": f["card_number"],
            "parallel": f["parallel"],
            "serial_num": f["serial_num"],
            "print_run": f["print_run"],
            "psa_grade": f["grade_value"],
            "grade_type": f["grade_type"],
            "qualifier": f["grade_qualifier"],
            "is_auto": f["is_auto"],
            "auto_grade": f["auto_grade"],
            "group_members": [
                {"item_id": m["item_id"], "title": m["title"],
                 "price": m["price"], "shipping": m["shipping_cost"],
                 "total": peers.total(m)}
                for m in ms],
            # Screening prior only: how far apart this slab's active asking
            # prices sit. Never evidence of market value - Product Research
            # and the valuation policy remain the deciding stage.
            "intra_slab_dispersion": peers.dispersion_of(ms),
        })
    out.sort(key=lambda c: (c["year"] or 0, c["item_id"]))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--out", default="pilot_candidates.json")
    args = ap.parse_args()

    conn = db.connect(args.db)
    cands = build(conn)
    json.dump(cands, open(args.out, "w"), indent=1)
    print(f"exported {len(cands)} confirmed_same_identity candidates -> {args.out}")
    for c in cands:
        print(f"  {c['year']} {str(c['set'])[:22]:22} #{str(c['card_number']):>7} "
              f"{str(c['subject'])[:18]:18} PSA {c['psa_grade']:<3} "
              f"par={str(c['parallel'])[:14]:14} /{c['print_run'] or '-'}"
              f"{' AUTO' + str(c['auto_grade']) if c['auto_grade'] else ''}"
              f"  ${c['asking_price']:,.2f}  n={len(c['group_members'])}")


if __name__ == "__main__":
    main()
