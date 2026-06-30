"""Top U.S. car insurance companies, ranked by U.S. News & World Report.

Source: U.S. News "Best Car Insurance Companies" (Travelers rated Best Overall).
Ordering reflects U.S. News overall ratings; see the source link on the page.
"""
INS_EDITION = "Best Car Insurance Companies, June 2026"
INS_SOURCE_URL = "https://www.usnews.com/insurance/auto/best-car-insurance-companies"
W = "https://en.wikipedia.org/wiki/"

def _t(rank, company, hq, website, handle, wiki):
    return {"rank": rank, "company": company, "hq": hq,
            "website": website, "handle": handle, "wikipedia": W + wiki}

# Top U.S. News-rated car insurers (Travelers = Best Overall).
INSURERS = [
    _t(1,  "Travelers",            "New York, NY",         "https://www.travelers.com",     "travelers",      "The_Travelers_Companies"),
    _t(2,  "USAA",                 "San Antonio, TX",      "https://www.usaa.com",          "usaa",           "USAA"),
    _t(3,  "Geico",                "Chevy Chase, MD",      "https://www.geico.com",         "geico",          "GEICO"),
    _t(4,  "Nationwide",           "Columbus, OH",         "https://www.nationwide.com",    "nationwide",     "Nationwide_Mutual_Insurance_Company"),
    _t(5,  "State Farm",           "Bloomington, IL",      "https://www.statefarm.com",     "statefarm",      "State_Farm"),
    _t(6,  "Progressive",          "Mayfield Village, OH", "https://www.progressive.com",   "progressive",    "Progressive_Corporation"),
    _t(7,  "Allstate",             "Northbrook, IL",       "https://www.allstate.com",      "allstate",       "Allstate"),
    _t(8,  "Erie Insurance",       "Erie, PA",             "https://www.erieinsurance.com", "erieinsurance",  "Erie_Insurance_Group"),
    _t(9,  "Amica",                "Lincoln, RI",          "https://www.amica.com",         "amica",          "Amica_Mutual_Insurance_Company"),
    _t(10, "Auto-Owners Insurance","Lansing, MI",          "https://www.auto-owners.com",   "AutoOwnersIns",  "Auto-Owners_Insurance"),
    _t(11, "Farmers Insurance",    "Woodland Hills, CA",   "https://www.farmers.com",       "WeAreFarmers",   "Farmers_Insurance_Group"),
    _t(12, "American Family",      "Madison, WI",          "https://www.amfam.com",         "amfam",          "American_Family_Insurance"),
]
