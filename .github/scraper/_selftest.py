"""
Offline self-test for the Darlington scraper core.

Feeds realistic synthetic notice text through the SAME parse/score/validate/
output path used in production -- no network -- to prove the backbone is sound.
Run:  python fetch.py --selftest
"""
import json
import fetch as F


# --- realistic notice bodies (paraphrased structures of real SC legals) -----
NOTICE_FORECLOSURE = """
STATE OF SOUTH CAROLINA COUNTY OF DARLINGTON IN THE COURT OF COMMON PLEAS
Case No. 2026-CP-16-00342 Master in Equity
NationStar Mortgage LLC, Plaintiff, vs. Marcus T. Whitfield, Defendant.
NOTICE OF SALE BY VIRTUE of a judgment heretofore granted in the amount of
$142,318.55, the Master in Equity for Darlington County will sell the property
described as: All that lot of land with improvements thereon situate at
118 Sumter Highway, Darlington, SC 29532, TMS# 165-09-07-021, on the regular
sales day, June 1, 2026. Terms of Sale: cash.
"""

NOTICE_ESTATE = """
NOTICE TO CREDITORS OF ESTATE OF Dorothy Mae Coker, Deceased. Date of Death:
March 2, 2026. Case Number 2026-ES-16-00118. All persons having claims against
the said estate must present them to the Personal Representative, James Coker,
204 Pinewood Drive, Hartsville, SC 29550, within the time prescribed by law.
"""

NOTICE_TAX = """
DELINQUENT TAX SALE NOTICE Darlington County. By virtue of executions issued by
the Treasurer, the Delinquent Tax Collector will sell the following. Defaulting
taxpayer: Reginald Owens, TMS# 089-14-02-003, property at 77 Cashua Ferry Road,
Darlington, SC 29540, amount due $13,902.40.
"""

NOTICE_CORP = """
NOTICE OF SALE Wells Fargo Bank, N.A. vs. Palmetto Holdings LLC, Defendant.
The Master in Equity will sell 900 Commerce Blvd, Hartsville, SC 29550,
TMS# 111-00-00-099, judgment $410,000.00.
"""

NOTICE_NO_ADDR = """
NOTICE TO CREDITORS ESTATE OF Henry P. Lucas, Deceased. Personal Representative
contact via counsel. Case 2026-ES-16-00210.
"""  # no property address, no enrichment in selftest -> should be dropped


def run_selftest() -> int:
    raw: list[F.Lead] = []
    raw.append(F._pn_parse("NOFC", NOTICE_FORECLOSURE, "http://x/1"))
    raw.append(F._pn_parse("PRO",  NOTICE_ESTATE,      "http://x/2"))
    raw.append(F._pn_parse("TAX",  NOTICE_TAX,         "http://x/3"))
    raw.append(F._pn_parse("NOFC", NOTICE_CORP,        "http://x/4"))
    raw.append(F._pn_parse("PRO",  NOTICE_NO_ADDR,     "http://x/5"))
    raw = [r for r in raw if r]

    print(f"\nParsed {len(raw)} raw leads from synthetic notices:")
    for r in raw:
        print(f"  - {r.cat:5} owner={r.owner!r:32} addr={r.prop_address!r}")

    # For the estate lead, simulate a successful qPublic enrichment so it keeps
    # an address (in production enrich_qpublic() does this over the network).
    for r in raw:
        if r.cat == "PRO" and "Coker" in r.owner and not F.has_property_address(r):
            r.prop_address, r.prop_city, r.prop_zip = "311 Spring Street", "Darlington", "29532"
            r.mail_address, r.mail_city, r.mail_state, r.mail_zip = \
                "204 Pinewood Drive", "Hartsville", "SC", "29550"
            r.cat, r.cat_label = "INH", F.CAT_LABELS["INH"]

    kept = F.validate(raw)
    print(f"\nValidated -> {len(kept)} leads (corps + address-less dropped):")
    for r in kept:
        print(f"  - score={r.score:3}  {r.cat:5}  {r.owner:28}  {r.prop_address:24}  flags={r.flags}")

    # exercise the real output writer
    F.OUT_DASHBOARD_JSON = "/tmp/dar_dashboard.json"
    F.OUT_DATA_JSON      = "/tmp/dar_data.json"
    F.OUT_CSV            = "/tmp/dar_leads.csv"
    F.write_outputs(kept)

    with open("/tmp/dar_dashboard.json") as fh:
        payload = json.load(fh)
    print("\nrecords.json top-level:",
          {k: payload[k] for k in ("county", "total", "with_address", "date_range")})
    print("first record keys match Horry schema:",
          list(payload["records"][0].keys()))

    print("\nCSV preview:")
    with open("/tmp/dar_leads.csv") as fh:
        for i, line in enumerate(fh):
            if i > 4:
                break
            print("  " + line.rstrip())

    # assertions
    assert all(r.county == "DARLINGTON" for r in kept), "county tag wrong"
    assert not any(F.is_corporation(r.owner) for r in kept), "corp leaked through"
    assert all(F.has_property_address(r) for r in kept), "address-less leaked"
    owners = {r.owner for r in kept}
    assert "Palmetto Holdings" not in " ".join(owners), "corp not filtered"
    print("\nALL ASSERTIONS PASSED ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_selftest())
