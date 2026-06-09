#!/usr/bin/env python3
"""
Darlington County, SC -- Motivated Seller Lead Scraper
======================================================

Sister scraper to the Horry County pipeline, but adapted to Darlington's
*completely different* records systems. Output schema (records.json + GHL CSV)
is intentionally identical to Horry so the two feed one combined dashboard,
while data stays in a separate repo so the counties never mix.

WHY THIS IS STRUCTURED DIFFERENTLY THAN HORRY
---------------------------------------------
Horry exposed two scraper-friendly sources: AcclaimWeb (CSV export) + Horry GIS
(live delinquent-tax layer). Darlington exposes neither:
  * Register of Deeds  -> Cott Systems "RECORDhub" (robots-blocked, images
                          paywalled, NO bulk export).
  * Courts             -> SC Judicial Public Index, which EXPRESSLY PROHIBITS
                          automated scraping and (since 2026-01-01) no longer
                          displays home addresses.
  * Property/owner     -> qPublic / Schneider (per-parcel lookup, no open
                          ArcGIS layer for Darlington).
  * Delinquent tax     -> an annually published sale list (PDF), not a live feed.

The one richly-legitimate, daily, county-filterable source is the SC Press
Association's SC Public Notices site (scpublicnotices.com, robots: all, free),
which carries Foreclosure "Notice of Sale", "Notice to Creditors" (probate),
and "Tax Sale" notices for Darlington. That is the backbone here.

LEAD TYPES PRODUCED
-------------------
  NOFC / LP  Pre-foreclosure / Lis pendens   <- Public Notices: Notice of Sale
  TAX        Delinquent tax                   <- Public Notices: Tax Sale  (+ annual PDF list)
  PRO / INH  Probate / Inherited estate       <- Public Notices: Notice to Creditors (+ qPublic enrich)

NOT produced automatically (documented manual / paid-data workflow in README):
  JUD (judgments), LN (liens / HOA / tax liens), divorce -- sources are
  scraping-prohibited or paywalled.

Same rules as Horry: NO corporations, MUST have a property address, validate
every field, only emit leads that carry the required information.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, date

# ----------------------------------------------------------------------------
# CONFIG  -- edit here, not in the body
# ----------------------------------------------------------------------------

COUNTY        = "DARLINGTON"          # tag stamped on every record / dashboard tab
COUNTY_FIPS   = "45031"
STATE         = "SC"

LOOK_BACK_DAYS = 30                   # window for daily public-notice pulls
                                      # (foreclosure/estate notices run ~weekly,
                                      #  so 30d avoids gaps from missed runs)

# --- Source switches: turn a source off if it needs calibration / is down ---
ENABLE_PUBLIC_NOTICES = True          # SC Public Notices (foreclosure/probate/tax)
ENABLE_TAX_SALE_PDF   = False         # set True + TAX_SALE_PDF_URL when the
                                      #   annual list is published (Oct-Dec)
ENABLE_QPUBLIC_ENRICH = True          # qPublic owner/parcel/mailing enrichment

# --- SC Public Notices (scpublicnotices.com) --------------------------------
PN_BASE          = "https://www.scpublicnotices.com"
PN_COUNTY_LABEL  = "Darlington"
# Publications that carry Darlington legals (used only as a sanity hint):
PN_PUBLICATIONS  = ["Darlington County News and Press", "The Hartsville Messenger"]
# Keyword buckets -> lead category. We run one search per bucket, county=Darlington.
PN_BUCKETS = {
    "NOFC": ["notice of sale", "master in equity", "foreclosure"],
    "TAX":  ["delinquent tax sale", "tax sale"],
    "PRO":  ["notice to creditors", "estate of", "personal representative"],
}

# --- qPublic / Schneider (Darlington assessor) ------------------------------
QPUBLIC_APP   = "DarlingtonCountySC"
QPUBLIC_URL   = f"https://qpublic.schneidercorp.com/Application.aspx?App={QPUBLIC_APP}&Layer=Parcels&PageType=Search"

# --- Annual delinquent tax sale list (seasonal) -----------------------------
TAX_SALE_PDF_URL = ""   # paste the county's published PDF URL when available

# --- Output paths (relative to repo root) -----------------------------------
OUT_DASHBOARD_JSON = "dashboard/records.json"
OUT_DATA_JSON      = "data/records.json"
OUT_CSV            = "data/leads_export.csv"

# Public-records reference link shown per lead (Darlington ROD / county portal)
COUNTY_RECORDS_PORTAL = "https://recordhub.cottsystems.com/DarlingtonSC/Portal/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ----------------------------------------------------------------------------
# DATA MODEL  (keys mirror the Horry records.json exactly, plus `county`)
# ----------------------------------------------------------------------------

CAT_LABELS = {
    "LP": "Lis Pendens", "NOFC": "Notice of Foreclosure", "TAXDEED": "Tax Deed",
    "JUD": "Judgment", "LN": "Lien", "INH": "Inherited", "PRO": "Probate Document",
    "NOC": "Notice of Commencement", "TAX": "Delinquent Tax", "RELLP": "Release LP",
}

@dataclass
class Lead:
    doc_num: str = ""
    doc_type: str = ""        # short cat code, kept for parity with Horry CSV
    cat: str = ""             # category code (NOFC/LP/TAX/PRO/INH/...)
    cat_label: str = ""
    filed: str = ""           # YYYY-MM-DD
    owner: str = ""           # distressed party (defendant / decedent / taxpayer)
    grantee: str = ""
    amount: float | None = None
    legal: str = ""
    tms_legal: str = ""
    clerk_url: str = ""
    source: str = ""
    prop_address: str = ""
    prop_city: str = ""
    prop_state: str = STATE
    prop_zip: str = ""
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = STATE
    mail_zip: str = ""
    tms: str = ""
    delinquent_tax: str = ""
    flags: list = field(default_factory=list)
    score: int = 0
    first_name: str = ""
    last_name: str = ""
    county: str = COUNTY      # <-- never blank, never "HORRY"

    def key(self) -> str:
        """Dedupe key."""
        base = (self.doc_num or "") + "|" + (self.owner or "") + "|" + (self.prop_address or "")
        return re.sub(r"\s+", " ", base.upper()).strip()


# ----------------------------------------------------------------------------
# CORPORATION / ENTITY FILTER  (rule: NO corporations)
# ----------------------------------------------------------------------------
# NOTE: "ESTATE OF ..." is a PERSON/probate lead, not a corp -> never filtered.

_CORP_TOKENS = [
    r"\bL\.?L\.?C\b", r"\bINC\b", r"\bCORP\b", r"\bCORPORATION\b", r"\bCOMPANY\b",
    r"\bCO\b", r"\bL\.?L\.?P\b", r"\bLP\b", r"\bLTD\b", r"\bTRUST\b", r"\bBANK\b",
    r"\bN\.?A\b", r"\bASSOCIATION\b", r"\bASSN\b", r"\bHOA\b",
    r"\bHOMEOWNERS\b", r"\bCONDOMINIUM\b", r"\bCHURCH\b", r"\bMINISTR", r"\bFOUNDATION\b",
    r"\bPARTNERS?\b", r"\bHOLDINGS?\b", r"\bPROPERTIES\b", r"\bINVESTMENTS?\b",
    r"\bENTERPRISES?\b", r"\bGROUP\b", r"\bFUND\b", r"\bCAPITAL\b", r"\bREALTY\b",
    r"\bMANAGEMENT\b", r"\bSERVICES?\b", r"\bAUTHORITY\b", r"\bDEPARTMENT\b",
    r"\bMORTGAGE\b", r"\bFINANCIAL\b", r"\bLENDING\b", r"\bSOLUTIONS\b",
    r"\bUSA\b", r"\bU\.?S\.?A\b", r"\bNATIONAL\b", r"\bFEDERAL\b", r"\bCREDIT UNION\b",
    r"\bCOUNTY OF\b", r"\bCITY OF\b", r"\bSTATE OF\b", r"\bTOWN OF\b",
]
_CORP_RE = re.compile("|".join(_CORP_TOKENS))

def is_corporation(name: str) -> bool:
    if not name:
        return False
    n = name.upper()
    if n.startswith("ESTATE OF") or " ESTATE OF " in n:
        return False  # probate person, keep
    return bool(_CORP_RE.search(n))


# ----------------------------------------------------------------------------
# NAME PARSING
# ----------------------------------------------------------------------------
_SUFFIX = {"JR", "SR", "II", "III", "IV", "V"}
_NOISE  = re.compile(r"\b(ET\s*AL|ETAL|AKA|FKA|N/?K/?A|F/?K/?A|DECEASED|DEC'?D|"
                     r"A/?K/?A|INDIVIDUALLY|AS TRUSTEE|TRUSTEE|DEFENDANT\(?S?\)?|"
                     r"PERSONAL REPRESENTATIVE|PR|ESTATE OF|HEIRS? OF|"
                     r"AND ALL OTHER|UNKNOWN)\b", re.I)

def clean_person(raw: str) -> str:
    s = _NOISE.sub(" ", raw or "")
    s = re.sub(r"[,&].*$", "", s)          # drop co-defendants after comma/&
    s = re.sub(r"\s+", " ", s).strip(" .,")
    return s

def split_name(raw: str, order: str = "first_last") -> tuple[str, str]:
    """order='first_last' for prose notices, 'last_first' for ROD-style indexes."""
    s = clean_person(raw)
    if not s:
        return "", ""
    parts = [p for p in s.split(" ") if p]
    parts = [p for p in parts if p.upper().strip(".") not in _SUFFIX]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    if order == "last_first":
        return " ".join(parts[1:]), parts[0]
    return " ".join(parts[:-1]), parts[-1]


# ----------------------------------------------------------------------------
# ADDRESS PARSING / VALIDATION
# ----------------------------------------------------------------------------
_STREET_SUFFIX = (r"ST|STREET|RD|ROAD|AVE|AVENUE|DR|DRIVE|LN|LANE|CT|COURT|"
                  r"BLVD|HWY|HIGHWAY|CIR|CIRCLE|WAY|PL|PLACE|TRL|TRAIL|LOOP|"
                  r"PKWY|PARKWAY|TER|TERRACE|RUN|PT|POINT|XING|CROSSING|SQ|SQUARE")
_ADDR_RE = re.compile(
    r"(\d{1,6}\s+[0-9A-Za-z'.\- ]{2,40}?\s+(?:%s)\b\.?)" % _STREET_SUFFIX, re.I)
_DARLINGTON_CITIES = ("DARLINGTON", "HARTSVILLE", "LAMAR", "SOCIETY HILL",
                      "FLORENCE", "LYDIA", "LExINGTON".upper())  # nearby/within

def extract_address(text: str) -> tuple[str, str, str]:
    """Return (street, city, zip) best-effort from free notice text."""
    if not text:
        return "", "", ""
    street = ""
    m = _ADDR_RE.search(text)
    if m:
        street = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
    # zip
    z = re.search(r"\b(2\d{4})(?:-\d{4})?\b", text)
    zipc = z.group(1) if z else ""
    # city (first known Darlington-area city appearing near the address)
    city = ""
    for c in _DARLINGTON_CITIES:
        if re.search(r"\b" + re.escape(c.title()) + r"\b", text, re.I):
            city = c.title()
            break
    return street, city, zipc

def has_property_address(ld: Lead) -> bool:
    return bool(ld.prop_address and re.search(r"\d", ld.prop_address))


# ----------------------------------------------------------------------------
# SCORING + FLAGS  (transparent additive model, mirrors Horry's flag taxonomy)
# ----------------------------------------------------------------------------
_BASE = {"NOFC": 45, "LP": 40, "TAX": 35, "JUD": 35, "PRO": 30, "INH": 30, "LN": 30}

def _is_out_of_state(ld: Lead) -> bool:
    return bool(ld.mail_state and ld.mail_state.upper() != STATE)

def _is_absentee(ld: Lead) -> bool:
    if not (ld.mail_address and ld.prop_address):
        return False
    a = re.sub(r"\s+", " ", ld.mail_address.upper()).strip()
    b = re.sub(r"\s+", " ", ld.prop_address.upper()).strip()
    return a[:18] != b[:18]

def score_and_flag(ld: Lead) -> None:
    flags: list[str] = []
    score = _BASE.get(ld.cat, 25)

    # category-driven flags
    if ld.cat == "NOFC":
        flags.append("Pre-foreclosure")
    if ld.cat == "LP":
        flags.append("Lis pendens")
    if ld.cat == "JUD":
        flags.append("Judgment lien")
    if ld.cat == "LN":
        flags.append("HOA lien")
    if ld.cat == "PRO":
        flags.append("Probate / estate")
    if ld.cat == "INH":
        flags.append("Inherited / estate")

    # tax-debt tiers
    debt = 0.0
    try:
        debt = float(ld.delinquent_tax or ld.amount or 0)
    except (TypeError, ValueError):
        debt = 0.0
    if ld.cat == "TAX" or ld.delinquent_tax:
        flags.append("Tax delinquent")
        if debt > 50000:
            score += 25; flags.append("Tax debt >$50k")
        elif debt > 25000:
            score += 18; flags.append("Tax debt >$25k")
        elif debt > 10000:
            score += 12; flags.append("Tax debt >$10k")
        elif debt > 0:
            score += 6

    # owner-location signals
    if _is_out_of_state(ld):
        score += 12; flags.append("Out-of-state owner")
    if _is_absentee(ld):
        score += 8; flags.append("Absentee owner")

    # recency
    if ld.filed:
        try:
            d = datetime.strptime(ld.filed, "%Y-%m-%d").date()
            if (date.today() - d).days <= 7:
                score += 6; flags.append("New this week")
        except ValueError:
            pass

    # contactability (rare for these sources, but reward it)
    # (phones/emails are not populated by these public sources by default)

    ld.flags = flags
    ld.score = max(0, min(100, int(round(score))))


# ----------------------------------------------------------------------------
# SOURCE 1 -- SC PUBLIC NOTICES (scpublicnotices.com)
# ----------------------------------------------------------------------------
# ASP.NET WebForms app (ViewState + __doPostBack + session-in-path). We drive it
# with Playwright like a human: open advanced search, filter county=Darlington,
# set the date window, run a keyword search per bucket, open each result, read
# the notice body, then parse fields by notice type.
#
# CALIBRATION: selectors below are written against the page structure observed
# at build time. If the site markup shifts, run  `python fetch.py --debug-pn`
# to dump the search page HTML and adjust the locators flagged  # <CALIBRATE>.

def fetch_public_notices() -> list[Lead]:
    leads: list[Lead] = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa
        log(f"[public_notices] Playwright unavailable: {e}")
        return leads

    since = (date.today() - timedelta(days=LOOK_BACK_DAYS)).strftime("%-m/%-d/%Y")
    until = date.today().strftime("%-m/%-d/%Y")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        for cat, keywords in PN_BUCKETS.items():
            for kw in keywords:
                try:
                    rows = _pn_run_search(page, kw, since, until)
                except Exception as e:  # noqa
                    log(f"[public_notices] search '{kw}' failed: {e}")
                    continue
                for body, url in rows:
                    ld = _pn_parse(cat, body, url)
                    if ld:
                        leads.append(ld)
                time.sleep(1.0)  # be polite
        browser.close()

    log(f"[public_notices] parsed {len(leads)} raw notice leads")
    return leads


def _pn_run_search(page, keyword: str, since: str, until: str) -> list[tuple[str, str]]:
    """Run one advanced search; return list of (notice_text, detail_url)."""
    page.goto(f"{PN_BASE}/Search.aspx", wait_until="domcontentloaded", timeout=45000)

    # County = Darlington  # <CALIBRATE: checkbox label>
    try:
        page.get_by_label(PN_COUNTY_LABEL, exact=True).check(timeout=4000)
    except Exception:
        page.locator(f"text='{PN_COUNTY_LABEL}'").first.click(timeout=4000)

    # Keyword  # <CALIBRATE: input id>
    kw_box = page.locator("input[id*='txtSearch'], input[type='text']").first
    kw_box.fill(keyword)

    # Date range  # <CALIBRATE: date input ids>
    try:
        page.locator("input[id*='From'], input[id*='Start']").first.fill(since)
        page.locator("input[id*='To'], input[id*='End']").first.fill(until)
    except Exception:
        pass

    # Submit  # <CALIBRATE: search button>
    page.locator("a[id*='btnSearch'], input[id*='btnSearch'], button:has-text('Search')").first.click()
    page.wait_for_load_state("networkidle", timeout=45000)

    results: list[tuple[str, str]] = []
    seen_pages = 0
    while True:
        # Each result links to a Public Notice Detail page  # <CALIBRATE>
        links = page.locator("a[href*='Details.aspx'], a[href*='PublicNotice']").all()
        detail_urls = []
        for a in links:
            href = a.get_attribute("href") or ""
            if href and ("Detail" in href or "Notice" in href):
                detail_urls.append(href if href.startswith("http") else f"{PN_BASE}/{href.lstrip('/')}")
        for u in dict.fromkeys(detail_urls):
            try:
                d = page.context.new_page()
                d.goto(u, wait_until="domcontentloaded", timeout=30000)
                body = d.locator("body").inner_text()
                d.close()
                results.append((body, u))
            except Exception:
                pass
        # pagination: click "Next" if present  # <CALIBRATE>
        nxt = page.locator("a:has-text('Next'), a[id*='Next']")
        if nxt.count() and seen_pages < 10:
            try:
                nxt.first.click(); page.wait_for_load_state("networkidle", timeout=30000)
                seen_pages += 1
                continue
            except Exception:
                break
        break
    return results


def _pn_parse(cat: str, text: str, url: str) -> Lead | None:
    """Parse a single notice body into a Lead based on its bucket category."""
    if not text or len(text) < 40:
        return None
    blob = re.sub(r"\s+", " ", text).strip()

    ld = Lead(cat=cat, cat_label=CAT_LABELS.get(cat, cat), doc_type=cat,
              source=f"SC Public Notices ({COUNTY} legals)", clerk_url=url)

    # filed date (first date-like token)
    md = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", blob)
    if md:
        mm, dd, yy = md.groups()
        try:
            ld.filed = date(int(yy), int(mm), int(dd)).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # amount (judgment / debt)
    am = re.search(r"\$\s?([\d,]+\.\d{2})", blob)
    if am:
        try:
            ld.amount = float(am.group(1).replace(",", ""))
        except ValueError:
            pass

    # TMS / parcel
    tms = re.search(r"\bTMS\s*#?\s*([\d\-\.]{6,})", blob, re.I)
    if tms:
        ld.tms = re.sub(r"[^\d]", "", tms.group(1))
        ld.tms_legal = ld.tms

    st, city, zipc = extract_address(blob)

    # owner / distressed party + legal description, by notice type
    if cat in ("NOFC", "LP"):
        # capture defendant: stop at ", Defendant" / "Defendant" / end-of-clause,
        # NOT at a middle-initial period (e.g. "Marcus T. Whitfield").
        m = re.search(r"\bvs\.?\s+(.+?)\s*,?\s*Defendant", blob, re.I) \
            or re.search(r"\bvs\.?\s+([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3})", blob)
        ld.owner = clean_person(m.group(1)) if m else ""
        if re.search(r"lis pendens", blob, re.I):
            ld.cat, ld.cat_label = "LP", CAT_LABELS["LP"]
        # property address comes straight from the sale notice
        ld.prop_address, ld.prop_city, ld.prop_zip = st, city, zipc
        ld.first_name, ld.last_name = split_name(ld.owner, "first_last")
        leg = re.search(r"(described as[:]?\s.+?)(?:TMS|Terms of Sale|Dated|$)", blob, re.I)
        ld.legal = (leg.group(1)[:240] if leg else "")[:240]

    elif cat == "PRO":
        m = re.search(r"estate of\s+(.+?)(?:,|\bdeceased|\bDate of Death|\bcase|\bCase|\.)",
                      blob, re.I)
        ld.owner = clean_person(m.group(1)) if m else ""
        # IMPORTANT: an address in an estate notice is the Personal
        # Representative's CONTACT address, not the decedent's property.
        # Route it to MAILING; leave prop_address for qPublic enrichment by
        # decedent name. (Lead is dropped later if no property is found.)
        if st:
            ld.mail_address, ld.mail_city, ld.mail_zip = st, city, zipc
        ld.first_name, ld.last_name = split_name(ld.owner, "first_last")

    elif cat == "TAX":
        m = re.search(r"(?:defaulting taxpayer|owner)[:\s]+(.+?)(?:,|TMS|\.)", blob, re.I)
        ld.owner = clean_person(m.group(1)) if m else ""
        ld.prop_address, ld.prop_city, ld.prop_zip = st, city, zipc
        ld.delinquent_tax = str(ld.amount) if ld.amount else ""
        ld.first_name, ld.last_name = split_name(ld.owner, "last_first")

    if not ld.owner:
        return None
    return ld


# ----------------------------------------------------------------------------
# SOURCE 2 -- ANNUAL DELINQUENT TAX SALE LIST (PDF)   [seasonal, opt-in]
# ----------------------------------------------------------------------------
def fetch_tax_sale_pdf() -> list[Lead]:
    leads: list[Lead] = []
    if not TAX_SALE_PDF_URL:
        log("[tax_sale_pdf] no TAX_SALE_PDF_URL set; skipping")
        return leads
    try:
        import requests, pdfplumber, io
    except Exception as e:  # noqa
        log(f"[tax_sale_pdf] deps unavailable: {e}")
        return leads
    try:
        r = requests.get(TAX_SALE_PDF_URL, headers={"User-Agent": UA}, timeout=60)
        r.raise_for_status()
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for pg in pdf.pages:
                for row in (pg.extract_table() or []):
                    ld = _tax_row_to_lead(row)
                    if ld:
                        leads.append(ld)
    except Exception as e:  # noqa
        log(f"[tax_sale_pdf] failed: {e}")
    log(f"[tax_sale_pdf] parsed {len(leads)} rows")
    return leads


def _tax_row_to_lead(row: list) -> Lead | None:
    # CALIBRATE to the real column order once the county posts the list.
    cells = [(c or "").strip() for c in row]
    if not cells or not any(cells):
        return None
    joined = " ".join(cells)
    if re.search(r"name|tms|parcel|amount", joined, re.I) and not re.search(r"\d", joined):
        return None  # header row
    name = next((c for c in cells if re.search(r"[A-Za-z]{3,}", c) and not re.search(r"\$", c)), "")
    amt  = next((c for c in cells if re.search(r"\$?\d[\d,]*\.\d{2}", c)), "")
    tms  = next((c for c in cells if re.fullmatch(r"[\d\-\.]{6,}", c.replace(" ", ""))), "")
    if not name:
        return None
    ld = Lead(cat="TAX", cat_label=CAT_LABELS["TAX"], doc_type="TAX",
              owner=clean_person(name),
              source=f"{COUNTY} County Delinquent Tax Sale List",
              clerk_url=COUNTY_RECORDS_PORTAL, tms=re.sub(r"[^\d]", "", tms),
              tms_legal=re.sub(r"[^\d]", "", tms))
    try:
        ld.amount = float(re.sub(r"[^\d.]", "", amt)) if amt else None
        ld.delinquent_tax = str(ld.amount) if ld.amount else ""
    except ValueError:
        pass
    ld.first_name, ld.last_name = split_name(ld.owner, "last_first")
    return ld


# ----------------------------------------------------------------------------
# ENRICHMENT -- qPublic / Schneider (owner -> property/mailing/value)
# ----------------------------------------------------------------------------
# Best-effort, per-lead lookup (like a human researching one parcel). Fragile by
# nature (Schneider has anti-bot); failures degrade gracefully -- the lead is
# kept only if it still satisfies has_property_address().

def enrich_qpublic(leads: list[Lead]) -> None:
    if not ENABLE_QPUBLIC_ENRICH or not leads:
        return
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa
        log(f"[qpublic] Playwright unavailable: {e}")
        return
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=UA).new_page()
        # accept the qPublic disclaimer once
        try:
            page.goto(QPUBLIC_URL, wait_until="domcontentloaded", timeout=45000)
            agree = page.locator("button:has-text('Agree'), a:has-text('Agree')")
            if agree.count():
                agree.first.click(); page.wait_for_load_state("networkidle", timeout=20000)
        except Exception as e:  # noqa
            log(f"[qpublic] disclaimer step failed: {e}")
        for ld in leads:
            # only spend lookups where we need them
            need_addr = not has_property_address(ld)
            need_mail = not ld.mail_address
            if not (need_addr or need_mail):
                continue
            try:
                _qpublic_lookup(page, ld)
                time.sleep(0.8)
            except Exception as e:  # noqa
                log(f"[qpublic] lookup failed for {ld.owner!r}: {e}")
        browser.close()


def _qpublic_lookup(page, ld: Lead) -> None:
    # CALIBRATE selectors to the live Schneider app for Darlington.
    name = ld.owner
    if not name:
        return
    box = page.locator("input[id*='Owner'], input[type='text']").first
    box.fill(name)
    page.locator("button:has-text('Search'), input[type='submit']").first.click()
    page.wait_for_load_state("networkidle", timeout=20000)
    first = page.locator("a[href*='KeyValue'], table a").first
    if not first.count():
        return
    first.click(); page.wait_for_load_state("networkidle", timeout=20000)
    txt = page.locator("body").inner_text()

    if not has_property_address(ld):
        st, city, zipc = extract_address(txt)
        if st:
            ld.prop_address, ld.prop_city, ld.prop_zip = st, city or ld.prop_city, zipc or ld.prop_zip
    mm = re.search(r"Mailing Address[:\s]+(.+?)(?:\n|Owner|Parcel)", txt, re.I)
    if mm:
        line = mm.group(1).strip()
        ld.mail_address = line
        z = re.search(r"\b([A-Z]{2})\s+(\d{5})\b", line)
        if z:
            ld.mail_state, ld.mail_zip = z.group(1), z.group(2)


# ----------------------------------------------------------------------------
# VALIDATION + DEDUPE
# ----------------------------------------------------------------------------
def validate(leads: list[Lead]) -> list[Lead]:
    out: dict[str, Lead] = {}
    dropped = {"corp": 0, "no_addr": 0, "dupe": 0}
    for ld in leads:
        if is_corporation(ld.owner):
            dropped["corp"] += 1
            continue
        if not has_property_address(ld):
            dropped["no_addr"] += 1
            continue
        ld.county = COUNTY
        if not ld.prop_state:
            ld.prop_state = STATE
        score_and_flag(ld)
        k = ld.key()
        if k in out:
            dropped["dupe"] += 1
            # keep the higher score
            if ld.score > out[k].score:
                out[k] = ld
            continue
        out[k] = ld
    log(f"[validate] kept {len(out)} | dropped corp={dropped['corp']} "
        f"no_addr={dropped['no_addr']} dupe={dropped['dupe']}")
    return list(out.values())


# ----------------------------------------------------------------------------
# OUTPUT  (records.json x2  +  GHL CSV)
# ----------------------------------------------------------------------------
CSV_COLS = ["First Name", "Last Name", "Mailing Address", "Mailing City",
            "Mailing State", "Mailing Zip", "Property Address", "Property City",
            "Property State", "Property Zip", "Phone 1", "Phone 2", "Email 1",
            "Email 2", "Lead Type", "Document Type", "Date Filed",
            "Document Number", "Amount/Debt Owed", "TMS Parcel", "Delinquent Tax",
            "Seller Score", "Motivated Seller Flags", "Source",
            "Public Records URL", "County"]

def write_outputs(leads: list[Lead]) -> None:
    leads.sort(key=lambda r: (r.score, r.filed or ""), reverse=True)
    payload = {
        "fetched_at": datetime.now().isoformat(),
        "county": COUNTY,
        "source": f"{COUNTY} County, SC -- SC Public Notices + Tax Sale List (qPublic enriched)",
        "date_range": f"last {LOOK_BACK_DAYS} days",
        "total": len(leads),
        "with_address": sum(1 for r in leads if has_property_address(r)),
        "records": [asdict(r) for r in leads],
    }
    for path in (OUT_DASHBOARD_JSON, OUT_DATA_JSON):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for r in leads:
            w.writerow([
                r.first_name, r.last_name, r.mail_address, r.mail_city,
                r.mail_state or STATE, r.mail_zip, r.prop_address, r.prop_city,
                r.prop_state or STATE, r.prop_zip, "", "", "", "",
                r.cat_label, r.cat, r.filed, r.doc_num,
                (r.amount if r.amount is not None else ""), r.tms, r.delinquent_tax,
                r.score, "; ".join(r.flags), r.source, r.clerk_url, r.county,
            ])
    log(f"[output] wrote {len(leads)} leads -> {OUT_DASHBOARD_JSON}, {OUT_DATA_JSON}, {OUT_CSV}")


# ----------------------------------------------------------------------------
# RUNNER
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)

def main() -> int:
    log(f"=== {COUNTY} County lead scraper starting ===")
    leads: list[Lead] = []

    if ENABLE_PUBLIC_NOTICES:
        leads += fetch_public_notices()
    if ENABLE_TAX_SALE_PDF:
        leads += fetch_tax_sale_pdf()

    log(f"[merge] {len(leads)} raw leads before enrichment")
    enrich_qpublic(leads)

    leads = validate(leads)
    write_outputs(leads)
    log(f"=== done: {len(leads)} validated {COUNTY} leads ===")
    return 0

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from _selftest import run_selftest  # local dev only
        sys.exit(run_selftest())
    try:
        sys.exit(main())
    except Exception:  # noqa
        traceback.print_exc()
        # Never fail the GitHub Action hard -> write an empty-but-valid payload
        try:
            write_outputs([])
        except Exception:
            pass
        sys.exit(0)
