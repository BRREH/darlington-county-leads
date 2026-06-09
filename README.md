# Darlington County, SC — Motivated Seller Lead Scraper

Sister pipeline to `horry-county-leads`. Same output schema, same scoring,
same rules (no corporations, must have a property address, validate every
field). **Separate repo so the two counties' data never mix.** The combined
dashboard reads both repos' published JSON at view time.

---

## What it produces

Darlington's records systems are very different from Horry's, so the legitimate
daily sources are different. Here's the honest picture:

| Lead type | Source | Daily? | Notes |
|---|---|---|---|
| Pre-foreclosure / Lis pendens | SC Public Notices → Master-in-Equity *Notice of Sale* | ✅ | Defendant + property address + judgment amount + sale date. Strongest leads. |
| Delinquent tax | SC Public Notices *Tax Sale* + the county's annual **tax sale list (PDF)** | ⚠️ | Daily near sale season; the full list publishes once a year (Oct–Dec). |
| Probate / Pre-probate / Inheritance | SC Public Notices *Notice to Creditors* | ⚠️ | Notice gives decedent + personal representative, **not** the property address — enriched via qPublic by owner name. Lead survives only if enrichment finds an address. |
| Judgments ("judges") | SC Public Index / ROD judgment liens | ❌ | Public Index **bans scraping**; ROD is paywalled. Manual/paid only. |
| Liens / HOA lien / tax liens | Cott ROD (`recordhub`) | ❌ | Robots-blocked, images paywalled, no bulk export. Manual/subscription only. |
| Divorce | SC Family Court | ❌ | Records are restricted/confidential in SC and scraping is prohibited. |

So the automated pipeline covers **pre-foreclosure/lis-pendens,
tax-delinquent, and probate/inheritance**. The other three need the manual
or paid-data workflow below.

## How it's sourced

The backbone is the SC Press Association's **SC Public Notices**
(`scpublicnotices.com`) — free, county-filterable, and its robots policy allows
access. It carries Darlington's foreclosure, probate, and tax-sale legals.
The scraper queries it once per run (like a human checking the day's notices),
parses each notice, then enriches probate leads against **qPublic/Schneider**
for the property address. No login is required for either source.

---

## No secrets required

Unlike Horry (which needed `HORRY_USERNAME` / `HORRY_PASSWORD` for AcclaimWeb),
the Darlington base pipeline uses only public pages. There is **nothing to add**
under repo *Settings → Secrets and variables → Actions*.

---

## Sanctioned alternative: Smart Search email

SC Public Notices offers **Smart Search** — save a county+keyword search and it
emails you matching notices daily. This is the publisher's own opt-in channel
and is the cleanest possible feed. If you'd rather not run the scraper at all
(or want a redundant backstop), sign up at
`scpublicnotices.com/SmartSearchSignup.aspx`, save searches for Darlington
filtered to *Foreclosures*, *Notice to Creditors*, and *Tax Sales*, and route
the emails wherever you like.

---

## Seasonal step: the annual tax sale list

The full delinquent list is published as a PDF each fall (registration opens
October, sale in December). When the county posts it:

1. Open `scraper/fetch.py`.
2. Set `ENABLE_TAX_SALE_PDF = True`.
3. Paste the PDF URL into `TAX_SALE_PDF_URL = "..."`.
4. Re-check the column mapping in `_tax_row_to_lead()` against the actual PDF
   layout (it's marked `# CALIBRATE`), then commit.

Tax Collector: 1 Public Square, Room 207, Darlington — (843) 398-4170.

---

## One-time calibration

Two live sites couldn't be exercised in the build sandbox (SC Public Notices
blocks non-browser robots and is JS/WebForms-driven; qPublic has anti-bot and
the seasonal PDF didn't exist yet). Their selectors are written against the
observed page structure and flagged in the code with `# <CALIBRATE>`. On first
real run, if a source returns nothing, calibrate it once:

- **SC Public Notices** — locators for the county checkbox, keyword box, date
  fields, search button, result links, and the "Next" pager live in
  `_pn_run_search()`. Run the scraper locally with a headed browser
  (`headless=False`) and adjust any selector that doesn't match.
- **qPublic** — the owner-search box, results link, and the
  mailing-address/property-address parse live in `_qpublic_lookup()`.
- **Tax PDF** — column order in `_tax_row_to_lead()` (see seasonal step above).

The pipeline is built to **fail soft**: if a source errors, it logs and writes
an empty-but-valid `records.json` rather than crashing the Action, so the
dashboard never breaks.

---

## Local run

```bash
cd darlington-county-leads
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium
python scraper/fetch.py            # writes dashboard/ + data/ outputs
python scraper/fetch.py --selftest # offline parser sanity check (no network)
```

## Outputs

- `dashboard/records.json` — served by GitHub Pages, read by the dashboard.
- `data/records.json` — same payload, archived in the repo.
- `data/leads_export.csv` — GHL-ready, identical columns to Horry **plus** a
  trailing `County` column.

Every record is tagged `county: "DARLINGTON"`.
