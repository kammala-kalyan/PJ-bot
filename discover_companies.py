"""Finds which public hiring platform each company uses and writes companies.json.

Usage:  python discover_companies.py            (uses the built-in list below)
        python discover_companies.py "Acme AI" "Foo Labs"   (add more companies)
Companies already in companies.json are kept; new ones are probed and added.
"""
import asyncio
import json
import os
import re
import sys

import httpx

from careers import FETCH, HEADERS, LOCATION_OK

# Tech companies with offices in Bengaluru / India (product, startups, GCCs). Add freely.
COMPANIES = """
Razorpay, CRED, Swiggy, Zomato, Zepto, Meesho, PhonePe, Flipkart, Myntra, Groww, Zerodha, Upstox, Paytm, Ola, Uber,
Rapido, BigBasket, Udaan, ShareChat, Dream11, Games24x7, InMobi, Glance, Freshworks, Postman, BrowserStack, Chargebee,
Druva, Hasura, Atlan, Rippling, Juspay, Setu, Slice, Jupiter, Fi Money, Niyo, Khatabook, OkCredit, Licious,
Urban Company, Nykaa, Lenskart, Cars24, Spinny, Ather Energy, Ola Electric, Unacademy, upGrad, PhysicsWallah, Scaler,
Simplilearn, Curefit, Practo, Innovaccer, HealthifyMe, Acko, Go Digit, PolicyBazaar, Zeta, M2P Fintech, Open Financial,
Cashfree, PayU, BharatPe, Pine Labs, Navi, Smallcase, INDmoney, CoinDCX, CoinSwitch, Polygon Labs, Uniphore,
Observe.AI, Yellow.ai, Haptik, Gupshup, Exotel, LeadSquared, MoEngage, CleverTap, WebEngage, Netcore, Darwinbox, Keka,
Zluri, Whatfix, Mindtickle, HighRadius, Icertis, Sprinklr, Zenoti, Capillary Technologies, Unbxd, Sigmoid,
Fractal Analytics, Tiger Analytics, Mu Sigma, LatentView, MathCo, Tredence, Quantiphi, Course5, Gramener,
Affine Analytics, Mad Street Den, Qure.ai, Sarvam AI, Krutrim, Glean, Databricks, Snowflake, Confluent, MongoDB,
Elastic, HashiCorp, Atlassian, Stripe, Coinbase, Twilio, Okta, Cloudflare, Datadog, New Relic, Dynatrace, PagerDuty,
Zscaler, Palo Alto Networks, CrowdStrike, Rubrik, Cohesity, Nutanix, NetApp, Intuit, Visa, Mastercard, PayPal,
Thoughtworks, Publicis Sapient, EPAM, Grab, Booking.com, Expedia, Agoda, MakeMyTrip, Airbnb, Dropbox, Zendesk,
HubSpot, Shopify, Wayfair, eBay, Lam Research, Synopsys, Cadence, Autodesk, PTC, Commvault, Tekion, Ninjacart, DeHaat,
Jumbotail, Locus, Shiprocket, Delhivery, BlackBuck, Porter, Zetwerk, OfBusiness, Moglix, Vymo, Yubi, Perfios, Signzy,
IDfy, HyperVerge, Hevo Data, Sprinto, Scapia, Kuku FM, Pocket FM, Apna, Plivo, Simpl, NoBroker, Tata 1mg, PharmEasy,
Eka Care, Headout, Sahaj Software, Infrrd, Anakin, Amber, StackBinary, Lanmea, Hiver, CoinDCX, Rubrik, Trilogy,
Wingify, VWO, Clevertap, Sarvam, Fyle, Toplyne, Superops, SuperOps.ai, Gojek, Turing, Deel, Remote, Canva, Notion,
Figma, Miro, Grammarly, Duolingo, Airtable, Asana, Gitlab, GitHub, Sentry, Vercel, Supabase, Replit, Anthropic,
OpenAI, Cohere, Scale AI, Hugging Face, Weights & Biases, Pinecone, LangChain, Mistral, Perplexity, Rapido,
Tala, Jar, Fampay, Kreditbee, Moneyview, Lendingkart, Ixigo, Cleartrip, Swiggy Instamart, Dunzo, BlinkIt, Zepto,
Rebel Foods, Country Delight, Sugar Cosmetics, Mamaearth, Boat, Noise, Bewakoof, Snitch, Zivame, FirstCry, Purplle,
Pepperfry, Livspace, HomeLane, Square Yards, Housing.com, MagicPin, Nearbuy, Bounce, Yulu, Vogo, Zoomcar, Drivezy,
Agnikul, Skyroot, Pixxel, Digantara, Bellatrix, Niramai, SigTuple, Cloudphysician, Wysa, Even Healthcare, Plum,
Loop Health, Onsurity, Ditto, Turtlemint, RenewBuy, Paytm Insider, BookMyShow, Hotstar, JioCinema, Zee5, Sony LIV,
Inshorts, Dailyhunt, Josh, Moj, Roposo, Eloelo, Stage, Winzo, Zupee, Nazara, Gameberry, Loco, Rooter, Kapture CX,
Freshdesk, Zoho, Chargebee, Recko, Juspay, Decentro, Razorpay X, Open Money, Volt Money, Stable Money, Groww,
Sigmoid Analytics, Aquera, Ninestars, Superkalam, Minfy, Gnani, Redacto, Blubridge, Plivo, Wayfair, Honeywell,
Visa Inc, Target, Walmart, Lowe's, Tesco, Sainsbury's, Philips, Siemens, Bosch, GE HealthCare, Shell, Unilever,
Diageo, AB InBev, American Express, Goldman Sachs, Morgan Stanley, Deutsche Bank, Barclays, Standard Chartered,
Fidelity, BlackRock, State Street, Nasdaq, Wells Fargo, JPMorgan Chase, Mercedes-Benz, Volvo, Continental, Harman,
Samsung, LG, Sony, Dell, HP, Cisco, Juniper Networks, Arista Networks, F5, Akamai, Salesforce, ServiceNow, Workday,
Adobe, SAP, Oracle, VMware, Red Hat, IBM, Accenture, Deloitte, KPMG, EY, PwC, Capgemini, Infosys, Wipro, TCS,
HCLTech, Tech Mahindra, LTIMindtree, Mphasis, Persistent Systems, Coforge, Hexaware, Zensar, Happiest Minds,
Sasken, Cyient, Tata Elxsi, KPIT, L&T Technology Services, Quest Global, Altimetrik, Nagarro, Sonata Software,
UST, Virtusa, NTT Data, DXC Technology, Cognizant, Unisys, Kyndryl, Genpact, EXL, WNS, Concentrix, Teleperformance
"""

# Big companies on Workday need their exact job-site address (host|tenant|site); verified by the probe below.
WORKDAY = {
    "NVIDIA": "nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite",
    "Salesforce": "salesforce.wd12.myworkdayjobs.com|salesforce|External_Career_Site",
    "Adobe": "adobe.wd5.myworkdayjobs.com|adobe|external_experienced",
    "Intel": "intel.wd1.myworkdayjobs.com|intel|External",
    "Red Hat": "redhat.wd5.myworkdayjobs.com|redhat|jobs",
    "Walmart": "walmart.wd5.myworkdayjobs.com|walmart|WalmartExternal",
    "Samsung": "sec.wd3.myworkdayjobs.com|sec|SamsungCareers",
    "Mastercard": "mastercard.wd1.myworkdayjobs.com|mastercard|CorporateCareers",
    "Visa": "visa.wd5.myworkdayjobs.com|visa|Visa_Careers",
    "PayPal": "paypal.wd1.myworkdayjobs.com|paypal|jobs",
    "Target": "target.wd5.myworkdayjobs.com|target|targetcareers",
    "Dell": "dell.wd1.myworkdayjobs.com|dell|External",
    "HP": "hp.wd5.myworkdayjobs.com|hp|ExternalCareerSite",
    "Workday": "workday.wd5.myworkdayjobs.com|workday|Workday",
    "Cisco": "cisco.wd5.myworkdayjobs.com|cisco|Cisco_Careers",
    "Autodesk": "autodesk.wd1.myworkdayjobs.com|autodesk|Ext",
    "Zscaler": "zscaler.wd1.myworkdayjobs.com|zscaler|ZscalerCareers",
    "CrowdStrike": "crowdstrike.wd5.myworkdayjobs.com|crowdstrike|crowdstrikecareers",
    "Morgan Stanley": "ms.wd5.myworkdayjobs.com|ms|External",
    "Wells Fargo": "wf.wd1.myworkdayjobs.com|wf|WellsFargoJobs",
    "BlackRock": "blackrock.wd1.myworkdayjobs.com|blackrock|BlackRock_Professional",
    "State Street": "statestreet.wd1.myworkdayjobs.com|statestreet|Global",
    "Philips": "philips.wd3.myworkdayjobs.com|philips|jobs-and-careers",
    "Lam Research": "lamresearch.wd1.myworkdayjobs.com|lamresearch|LAM",
    "Applied Materials": "amat.wd1.myworkdayjobs.com|amat|External",
    "Synopsys": "synopsys.wd1.myworkdayjobs.com|synopsys|careers",
    "Thermo Fisher": "thermofisher.wd5.myworkdayjobs.com|thermofisher|ThermoFisherCareers",
    "GE HealthCare": "gehc.wd5.myworkdayjobs.com|gehc|GEHC_ExternalSite",
    "Honeywell": "honeywell.wd5.myworkdayjobs.com|honeywell|Honeywell",
    "Nasdaq": "nasdaq.wd1.myworkdayjobs.com|nasdaq|Global_External_Site",
    "Accenture": "accenture.wd3.myworkdayjobs.com|accenture|AccentureCareers",
    "Kyndryl": "kyndryl.wd5.myworkdayjobs.com|kyndryl|KyndrylProfessionalCareers",
    "Comcast": "comcast.wd5.myworkdayjobs.com|comcast|Comcast_Careers",
}
ATS_ORDER = ["greenhouse", "lever", "ashby", "smartrecruiters"]


def slugs(name):
    base = name.lower().replace("&", "and")
    words = re.findall(r"[a-z0-9]+", base)
    plain = "".join(words)
    core = "".join(w for w in words if w not in ("ai", "inc", "labs", "technologies", "technology", "software",
                                                  "analytics", "india", "money", "systems", "com", "io"))
    out = [plain, "-".join(words), core, plain + "india", plain + "inc", plain + "hq", plain + "careers",
           plain + "technologies", plain + "labs"]
    return list(dict.fromkeys(s for s in out if s))


async def probe(http, sem, name):
    for ats in ATS_ORDER:
        for slug in slugs(name):
            async with sem:
                try:
                    jobs = await FETCH[ats](http, slug)
                except Exception:
                    continue
            if jobs:
                india = sum(1 for j in jobs if LOCATION_OK.search(j.get("location") or ""))
                return {"name": name, "ats": ats, "slug": slug, "jobs": len(jobs), "india_jobs": india}
    return None


async def probe_workday(http, sem, name, slug):
    async with sem:
        try:
            jobs = await FETCH["workday"](http, slug)
        except Exception:
            return None
    return {"name": name, "ats": "workday", "slug": slug, "jobs": len(jobs), "india_jobs": len(jobs)} if jobs else None


async def main(extra):
    existing = {}
    if os.path.exists("companies.json"):
        existing = {c["name"]: c for c in json.load(open("companies.json", encoding="utf-8"))}
    names = list(dict.fromkeys(n.strip() for n in COMPANIES.replace("\n", ",").split(",") if n.strip()))
    names += [n for n in extra if n not in names]
    todo = [n for n in names if n not in existing and n not in WORKDAY]
    sem = asyncio.Semaphore(24)
    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as http:
        found = await asyncio.gather(*(probe(http, sem, n) for n in todo),
                                     *(probe_workday(http, sem, n, s) for n, s in WORKDAY.items()
                                       if n not in existing))
    for c in filter(None, found):
        existing[c["name"]] = c
    keep = sorted(existing.values(), key=lambda c: (-c["india_jobs"], c["name"]))
    json.dump(keep, open("companies.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    with_india = [c for c in keep if c["india_jobs"]]
    print(f"Probed {len(todo)} new + Workday list. Companies with a public job feed: {len(keep)} "
          f"({len(with_india)} currently have India/remote openings).")
    by_ats = {}
    for c in keep:
        by_ats[c["ats"]] = by_ats.get(c["ats"], 0) + 1
    print("By platform:", by_ats)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
