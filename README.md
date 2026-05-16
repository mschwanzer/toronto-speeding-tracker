# Toronto Speeding Tracker

An independent dashboard tracking changes in measured speeding across Toronto, derived from the City's own Watch Your Speed (WYS) driver-feedback radar signs.

## Status: as-is, no warranty, no maintenance commitment

**This site is not produced, reviewed, or endorsed by the City of Toronto, Toronto Police Service, the Province of Ontario, or any government body or agency.**

It is provided **strictly as-is**, with no warranty, no guarantee of accuracy or completeness, no representation that the analysis is fit for any particular purpose, and no liability for any use made of it. Nothing here is professional, legal, traffic-engineering, or policy advice.

The author makes **no commitment to respond to issues, fix errors, correct mistakes, update the data, or otherwise maintain the page**. Issues filed on this repository may not be read or actioned. If you spot an error you believe is consequential, the source datasets are linked below and you have everything you need to verify or correct the math yourself.

## What the dashboard answers

- City-wide speeding trends over time, by ward, and per individual radar sign
- Year-over-year speeder counts at each "X+ km/h over the limit" tier (Jan–Apr 2026 vs 2025) — both rates and raw counts
- A difference-in-differences comparison around November 2025, when the Province banned Automated Speed Enforcement cameras, with 2024 as a seasonal control
- A "typical day" hour-of-day profile per sign (2023 baseline, the most recent year Toronto publishes detailed hourly counts)
- Streets and wards ranked by the largest changes

## Methodology in one paragraph

For each of Toronto's ~1,200 Stationary WYS signs the City publishes a monthly summary: how many vehicles passed and how many fell into each 5 km/h speed bin. We compute, per sign-month, the share of vehicles whose measured speed bin's lower boundary is at least 10 km/h above the sign's posted speed limit. Volume-weighted means across signs give city- and ward-level series. For the year-over-year tier comparison we aggregate raw bin counts at thresholds of 10, 15, 20, 25, and 30 km/h over the posted limit, using only signs that reported in both the current and prior windows so the comparison is apples-to-apples. The full methodology and per-section limitations are documented in the footer of the dashboard.

## How the page was built

Construction of the page and its analysis code was **AI-assisted**. The numbers themselves are derived deterministically from the named source datasets — they are public, the methodology is described in full on the page itself, and anyone can reproduce them independently of the author or the tooling used to build the page.

## Data sources (Open Government Licence – Toronto)

- [Safety Zone Watch Your Speed Program – Monthly Summary](https://open.toronto.ca/dataset/safety-zone-watch-your-speed-program-monthly-summary/)
- [School Safety Zone Watch Your Speed Program – Locations](https://open.toronto.ca/dataset/school-safety-zone-watch-your-speed-program-locations/)
- [Automated Speed Enforcement – Locations](https://open.toronto.ca/dataset/automated-speed-enforcement-locations/)
- [School Safety Zone WYS Program – Detailed Speed Counts](https://open.toronto.ca/dataset/school-safety-zone-watch-your-speed-program-detailed-speed-counts/) (hourly archive, 2017–2023 only)

Contains information licensed under the [Open Government Licence – Toronto](https://open.toronto.ca/open-data-license/). Source datasets © City of Toronto.

## How to verify any specific claim

Open the source CSV linked above, filter to the sign and months of interest, and sum the speed-bin columns whose lower boundary meets your threshold. The dashboard performs no aggregation that is not described in its methodology footer.

## Hosting

This is a static page (`index.html` + `data.json`). GitHub Pages serves it directly from the `main` branch root.

## Licence

The dashboard page is released under the MIT Licence (see `LICENSE`). The underlying data is licensed by the City of Toronto under [OGL-Toronto](https://open.toronto.ca/open-data-license/) and remains the City's. The licence on the data does not change because of this page.
