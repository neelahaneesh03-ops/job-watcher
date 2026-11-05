"""Hourly job watcher: new-grad ML / SWE roles, US + remote, sponsorship-friendly.

Sources
  1. SimplifyJobs New-Grad-Positions: curated new-grad list with sponsorship tags.
  2. Public job-board directory: ~12k company boards on Greenhouse / Ashby / Lever / Workday.
  3. Every hiring YC company (its job board is detected automatically).

Only jobs that pass the title + location filters are tracked. A job counts as new the first
time its ID shows up. For new jobs, the description is read to drop roles that ask for too
many years or rule out sponsorship. Whatever survives is emailed.
"""
import csv
import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
import time
import tomllib
import zlib
from concurrent.futures import ThreadPoolExecutor
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state")
OUT = os.path.join(ROOT, "out")
with open(os.path.join(ROOT, "config.toml"), "rb") as f:
    CFG = tomllib.load(f)

NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
YC_DATA = "https://raw.githubusercontent.com/yc-oss/api/main/companies/hiring.json"
SIMPLIFY = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
DIRECTORY = "https://kalebconfer-sys.github.io/job-board-directory/job-board-directory.json"
OLD_AFTER = dt.timedelta(days=3)   # a "new" job posted longer ago than this is ignored

session = requests.Session()
session.headers["User-Agent"] = UA
session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=48, pool_maxsize=48))


START = time.time()


def log(*a):
    print(f"[{time.time() - START:5.0f}s]", *a, file=sys.stderr, flush=True)


def get(url, **kw):
    for attempt in range(2):
        try:
            r = session.get(url, timeout=25, **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                time.sleep(4 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1)
    return None


def post(url, payload):
    for attempt in range(3):
        try:
            r = session.post(url, json=payload, timeout=25,
                             headers={"Accept": "application/json", "Content-Type": "application/json"})
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                time.sleep(4 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1)
    return None


def as_json(r):
    try:
        return r.json() if r is not None else None
    except ValueError:
        return None


def load_json(name, default):
    p = os.path.join(STATE, name)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except ValueError:
            pass
    return default


def save_json(name, data):
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, name), "w") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)


def parse_time(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- filters
INC = re.compile(CFG["roles"]["include_regex"], re.I)
EXC = re.compile(CFG["roles"]["exclude_regex"], re.I)
US_EXPLICIT = re.compile(r"united states|\busa?\b|\bu\.s\.", re.I)
NON_US = re.compile(
    r"\b(india|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|gurgaon|gurugram|noida|"
    r"united kingdom|\buk\b|england|london|scotland|canada|toronto|vancouver|montreal|ontario|"
    r"germany|berlin|munich|france|paris|spain|madrid|barcelona|portugal|lisbon|italy|milan|"
    r"netherlands|amsterdam|ireland|dublin|switzerland|zurich|sweden|stockholm|denmark|"
    r"copenhagen|norway|finland|poland|warsaw|krakow|czech|prague|austria|vienna|belgium|"
    r"europe|emea|apac|latam|singapore|australia|sydney|melbourne|new zealand|japan|tokyo|"
    r"korea|seoul|china|beijing|shanghai|shenzhen|hong kong|taiwan|taipei|israel|tel aviv|"
    r"brazil|são paulo|sao paulo|mexico|argentina|colombia|chile|peru|costa rica|philippines|"
    r"manila|vietnam|indonesia|malaysia|thailand|nigeria|lagos|kenya|south africa|egypt|uae|"
    r"dubai|saudi|qatar|pakistan|turkey|istanbul|ukraine|romania|bulgaria|serbia|greece)\b", re.I)


US_STATE = re.compile(
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|"
    r"NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b|\b(san francisco|bay area|new york|nyc|seattle|"
    r"boston|austin|los angeles|chicago|denver|atlanta|miami|tampa|dallas|houston|san jose|san diego|"
    r"palo alto|mountain view|menlo park|sunnyvale|cupertino|redmond|pittsburgh|philadelphia|"
    r"washington,?\s*d\.?c|raleigh|salt lake|phoenix|portland, or|minneapolis|detroit|nashville)\b")
FOREIGN_TITLE = re.compile(r"desenvolvedor|desarrollador|ingeniero|engenheir|pessoa |entwickler|d[eé]veloppeur|"
                           r"ing[eé]nieur|programador|analista|s[eê]nior|sviluppatore", re.I)


def title_ok(t):
    if FOREIGN_TITLE.search(t or ""):
        return False
    return bool(t) and bool(INC.search(t)) and not EXC.search(t)


NON_US_MORE = re.compile(
    r"\b(hungary|budapest|slovakia|bratislava|jordan|amman|croatia|zagreb|lithuania|vilnius|latvia|riga|"
    r"estonia|tallinn|slovenia|ljubljana|belarus|minsk|russia|moscow|kazakhstan|almaty|armenia|yerevan|"
    r"tbilisi|morocco|casablanca|tunisia|ghana|accra|sri lanka|colombo|bangladesh|dhaka|nepal|kathmandu|"
    r"uruguay|montevideo|ecuador|quito|guatemala|venezuela|cyprus|malta|luxembourg|iceland|lebanon|beirut|"
    r"kuwait|bahrain|oman|ethiopia|addis|uganda|kampala|rwanda|kigali|tanzania|cameroon|senegal|dakar|"
    r"zimbabwe|cairo|riyadh|jeddah|abu dhabi|doha|karachi|lahore|islamabad|hanoi|ho chi minh|bangkok|"
    r"vadodara|ahmedabad|kolkata|jaipur|kochi|cochin|coimbatore|trivandrum|thiruvananthapuram|indore|nagpur|"
    r"chandigarh|mohali|lucknow|bhubaneswar|visakhapatnam|vizag|mysore|mysuru|mangalore|surat|nashik|"
    r"kuala lumpur|jakarta|auckland|wellington|bogot[aá]|medell[ií]n|lima|buenos aires|guadalajara|"
    r"monterrey|s[aã]o paulo|rio de janeiro|brasil|remoto|teletrabajo|sant cugat|catalonia|catalu[nñ]a|"
    r"valencia|seville|porto|krak[oó]w|wroc[lł]aw|gda[nń]sk|pozna[nń]|cluj|bucharest|sofia|belgrade|"
    r"kyiv|kiev|lviv|kharkiv|odesa|tbilisi|baku|manila|cebu|taguig|makati|shenzhen|hangzhou|chengdu|"
    r"guangzhou|osaka|busan|edinburgh|manchester|glasgow|belfast|cambridge,?\s*uk|oxford,?\s*uk|"
    r"ottawa|calgary|edmonton|waterloo|quebec|qu[eé]bec|halifax|winnipeg|brussels|antwerp|rotterdam|"
    r"the hague|eindhoven|hamburg|frankfurt|cologne|stuttgart|lyon|toulouse|nice|geneva|lausanne|basel|"
    r"oslo|helsinki|gothenburg|aarhus|athens|thessaloniki|nicosia|valletta|reykjavik)\b", re.I)
# Workday-style country codes at the start, e.g. "ESP - CT - Sant Cugat", "IND-Bangalore", "PRT - Lisbon"
ISO_PREFIX = re.compile(r"^\s*([A-Z]{3})\s*[-,]")


def loc_ok(loc):
    """US or US-remote only. A location mentioning a foreign place counts as foreign unless it also
    clearly names the US ("Remote/Ukraine" -> foreign; "Boston, MA; London" -> kept)."""
    if CFG["location"]["allow_non_us"] or not loc:
        return True
    us = bool(US_EXPLICIT.search(loc) or US_STATE.search(loc))
    foreign = bool(NON_US.search(loc) or NON_US_MORE.search(loc))
    for part in re.split(r"[;|•\n]", loc):
        m = ISO_PREFIX.match(part)
        if m:
            if m.group(1) == "USA":
                us = True
            else:
                foreign = True
    return us or not foreign


OFF_TARGET = re.compile(r"biologist|chemist|physicist|clinician|physician|pharmacist|\bnurse\b|\btutor\b|"
                        r"\btrainer\b|annotat|\blabeler\b|writer", re.I)
TITLE_SKIPPED = {}  # jobs that matched your roles but were skipped for an off-target word in the title


def wanted(j):
    t = j.get("title", "")
    ok = title_ok(t) and loc_ok(j.get("location", ""))
    if not ok and t and INC.search(t) and OFF_TARGET.search(t) and loc_ok(j.get("location", "")):
        TITLE_SKIPPED[j["id"]] = j
    return ok


NEG = (r"(no|not|unable to|cannot|can ?not|can't|won't|will not|do not|does not|don't|are not able to|"
       r"is not able to|not in a position to)\s+(currently\s+|be\s+)?(able\s+to\s+)?")
NO_SPONSOR = re.compile(
    NEG + r"sponsor"
    + r"|" + NEG + r"(provide|offer|support|consider|accept)[^.]{0,60}?(sponsor|visa|h-?1b)"
    r"|no\s+(visa\s+|immigration\s+|employment\s+)?sponsorship"
    r"|sponsorship\s+(is\s+|will\s+)?(not|un)\s*(be\s+)?(available|provided|offered|possible)"
    r"|requir\w+\s+(visa\s+|immigration\s+)?sponsorship[^.]{0,40}(not\s+be\s+considered|ineligible|not\s+eligible)"
    r"|without\s+(the\s+)?(need\s+for\s+|requiring\s+|requirement\s+of\s+)?(current\s+or\s+future\s+|future\s+)?"
    r"(employer\s+|company\s+|visa\s+|employment\s+|immigration\s+)?(visa\s+)?sponsorship"
    r"|must\s+be\s+(a\s+)?(u\.?\s?s\.?|united\s+states)\s+citizen"
    r"|(u\.?\s?s\.?|united\s+states)\s+citizenship\s+(is\s+)?required"
    r"|requires?\s+(u\.?\s?s\.?|united\s+states)\s+citizenship"
    r"|green\s+card\s+holders?\s+only|citizens?\s+(or|and)\s+(green\s+card|permanent\s+resident)"
    r"|(active|current|ability\s+to\s+(obtain|maintain)|eligib\w+\s+(to|for))\s+[^.]{0,30}(security\s+)?clearance"
    r"|citizen\s*/\s*visa\s+only|\bno\s+visas?\b|\bvisa:\s*no\b"
    r"|\bitar\b|\bu\.?\s?s\.?\s+persons?\b", re.I)
SPONSOR_FRIENDLY = re.compile(
    r"\bh-?1b\b|visa\s+sponsorship\s+(is\s+)?(available|provided|offered)"
    r"|(will|can|able\s+to|happy\s+to|we)\s+sponsor|sponsorship\s+(is\s+)?available", re.I)
OPT_FRIENDLY = re.compile(r"\b(STEM\s+)?OPT\b|\bCPT\b")
NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10}
YEARS = re.compile(
    rf"(?:minimum\s+(?:of\s+)?|at\s+least\s+)?{NUM}\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*{NUM}\s*)?\+?\s*years?"
    rf"(?:\s+of)?[^.;\n]{{0,60}}?(experience|exp\b|industry|professional|work)", re.I)


LABEL_YEARS = re.compile(r"\bexperience\s*[:\-]?\s*(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?", re.I)
YC_ANY_EXP = re.compile(r"\bexperience\s*[:\-]?\s*(any|new grads? ok|0\s*-\s*1)", re.I)


def max_years(text):
    vals = [int(m.group(1)) for m in LABEL_YEARS.finditer(text)]
    for m in YEARS.finditer(text):
        a = m.group(1).lower()
        n = WORDNUM.get(a) if not a.isdigit() else int(a)
        if n is not None and n <= 20:
            vals.append(n)
    return max(vals) if vals else None


def min_years(text):
    """Smallest number of years of experience the posting mentions, or None."""
    if YC_ANY_EXP.search(text):
        return 0
    vals = [int(m.group(1)) for m in LABEL_YEARS.finditer(text)]
    for m in YEARS.finditer(text):
        a = m.group(1).lower()
        n = WORDNUM.get(a) if not a.isdigit() else int(a)
        if n is not None and n <= 20:
            vals.append(n)
    return min(vals) if vals else None


SENIOR_SIGNALS = [re.compile(x, re.I) for x in [
    r"\b(looking for|seeking|hiring|need)\s+(an?\s+)?(highly\s+)?(experienced|seasoned|senior)\b",
    r"\bhelp\s+(us\s+)?lead\b|\blead\s+(this|the)\s+(effort|team|initiative)",
    r"\byou(['’]ve|\s+have)\s+led\b",
    r"\bproven\s+track\s+record\b",
    r"\bextensive\s+(professional\s+|industry\s+)?experience\b",
    r"\bseasoned\b",
    r"\bmentor(ing|ship)?\s+(junior|other|less experienced)\b",
    r"\btechnical\s+(leadership|lead)\b|\btech\s+lead\b",
    r"\b(significant|substantial|deep)\s+experience\b",
    r"\bfrom\s+design\s+through\s+production\b",
]]


PHD = re.compile(r"\bph\.?\s?d\.?s?\b|\bdoctorate\b|\bdoctoral\s+degree\b", re.I)
PHD_SOFT = re.compile(r"prefer|\bplus\b|bonus|nice[- ]to[- ]have|ideal|desired|\bor\b[^.]{0,40}(master|m\.?s\.?\b|"
                      r"bachelor|b\.?s\.?\b|equivalent|experience)|(master|m\.?s\.?|bachelor|b\.?s\.?)[^.]{0,30}\bor\b"
                      r"|/\s*(ms|m\.s|master)|candidates?\b|students?\b|intern", re.I)
PHD_HARD = re.compile(r"^\W*(in|degree|required|requirement|is required|holder)\b|\b(required|must|minimum|"
                      r"requirements?)\b", re.I)


def phd_required(text):
    """True if the posting requires a PhD (not 'PhD preferred' / 'PhD or MS' / 'our team of PhDs')."""
    for m in PHD.finditer(text):
        before, after = text[max(0, m.start() - 60):m.start()], text[m.end():m.end() + 90]
        window = before[-40:] + " " + after
        if PHD_SOFT.search(window):
            continue
        starts_item = re.search(r"(^|[\n•·\-:]|requirements?\W*)\s*$", before, re.I)
        if PHD_HARD.search(after[:40]) or (starts_item and re.match(r"\s*(in|degree)\b", after, re.I)):
            return True
    return False


def senior_signals(text):
    return sum(1 for rx in SENIOR_SIGNALS if rx.search(text))


def html_to_text(s):
    if not s:
        return ""
    s = html.unescape(s)
    return BeautifulSoup(s, "html.parser").get_text(" ", strip=True)


# ---------------------------------------------------------------- company matching
SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd",
          "plc", "group", "holdings", "the", "lp", "sa", "ag", "nv"}


def tokens(name):
    name = (name or "").lower().replace("&", " and ")
    return [t for t in re.findall(r"[a-z0-9]+", name) if t not in SUFFIX]


class CompanyIndex:
    def __init__(self, names):
        self.items = []
        for name in names:
            variants = [name] + re.findall(r"\(([^)]+)\)", name) + [re.sub(r"\([^)]*\)", "", name)]
            for v in variants:
                t = tokens(v)
                if t:
                    self.items.append((t, "".join(t)))

    def match(self, company):
        ct = tokens(company)
        if not ct:
            return False
        cns = "".join(ct)
        return any(ns == cns or (len(ns) >= 3 and ct[:len(t)] == t) for t, ns in self.items)


# ---------------------------------------------------------------- board fetchers
# Each returns a list of job dicts (only jobs passing title/location filters) or None if
# the board doesn't exist. Jobs carry enough info to fetch their description later.

def ashby(slug):
    data = as_json(get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"))
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    out = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        sec = [x.get("location", "") for x in (j.get("secondaryLocations") or []) if isinstance(x, dict)]
        if sec:
            loc = "; ".join([loc] + sec)
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        job = {"id": f"ashby:{j.get('id')}", "title": j.get("title", ""), "location": loc,
               "emp_type": j.get("employmentType") or "",
               "posted_at": j.get("publishedAt"), "url": j.get("jobUrl") or j.get("applyUrl") or "",
               "_desc": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))}
        if wanted(job):
            out.append(job)
    return out


def greenhouse(slug):
    data = as_json(get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"))
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    out = []
    for j in data["jobs"]:
        job = {"id": f"gh:{j.get('id')}", "title": j.get("title", ""),
               "location": (j.get("location") or {}).get("name", ""),
               "posted_at": j.get("first_published") or j.get("updated_at"),
               "url": j.get("absolute_url", ""),
               "_desc_url": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{j.get('id')}"}
        if wanted(job):
            out.append(job)
    return out


def lever(slug):
    data = as_json(get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}))
    if not isinstance(data, list):
        return None
    out = []
    for j in data:
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        lists = " ".join(f"{x.get('text', '')} {html_to_text(x.get('content', ''))}" for x in (j.get("lists") or []))
        job = {"id": f"lever:{j.get('id')}", "title": j.get("text", ""), "emp_type": cat.get("commitment") or "",
               "location": cat.get("location") or "; ".join(cat.get("allLocations") or []),
               "posted_at": dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).isoformat() if ts else None,
               "url": j.get("hostedUrl", ""),
               "_desc": f"{j.get('descriptionPlain', '')} {lists} {j.get('additionalPlain', '')}"}
        if wanted(job):
            out.append(job)
    return out


def workday(token):
    # token looks like "wd1/<tenant>/<site>"
    try:
        shard, tenant, site = token.split("/", 2)
    except ValueError:
        return None
    base = f"https://{tenant}.{shard}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    out, ids, any_ok = [], set(), False
    for q in CFG["sources"]["workday_queries"]:
        data = as_json(post(f"{api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q}))
        if not isinstance(data, dict):
            continue
        any_ok = True
        for j in data.get("jobPostings") or []:
            path = j.get("externalPath") or ""
            if not path or path in ids:
                continue
            ids.add(path)
            posted = (j.get("postedOn") or "").lower()
            days = 0 if "today" in posted else 1 if "yesterday" in posted else \
                int((re.findall(r"(\d+)", posted) or ["30"])[0])
            job = {"id": f"wd:{tenant}{path}", "title": j.get("title", ""),
                   "location": j.get("locationsText", ""),
                   "posted_at": (NOW - dt.timedelta(days=days)).isoformat(),
                   "url": f"{base}/{site}{path}", "_desc_url": f"{api}{path}", "_kind": "workday"}
            if wanted(job):
                out.append(job)
        time.sleep(0.3)
    return out if any_ok else None


YC_JOB_LINK = re.compile(r"/companies/([a-z0-9-]+)/jobs/([A-Za-z0-9]+)-([a-z0-9-]+)")


def yc_page(slug):
    r = get(f"https://www.ycombinator.com/companies/{slug}/jobs")
    if not r:
        return None
    text = html.unescape(r.text)
    soup = BeautifulSoup(text, "html.parser")
    titles = {}
    for a in soup.find_all("a", href=YC_JOB_LINK):
        t = a.get_text(" ", strip=True)
        m = YC_JOB_LINK.search(a["href"])
        if t and m and len(t) > 3:
            titles.setdefault(m.group(2), t)
    jobs = {}
    for m in YC_JOB_LINK.finditer(text):
        if m.group(1) != slug or m.group(2) in jobs:
            continue
        jid = m.group(2)
        title = titles.get(jid) or re.sub(r"\b(Ai|Ml|Llm|Swe|Ios|Api|Cv|Gpu)\b", lambda x: x.group(1).upper(),
                                          m.group(3).replace("-", " ").title())
        url = f"https://www.ycombinator.com/companies/{slug}/jobs/{jid}-{m.group(3)}"
        jobs[jid] = {"id": f"yc:{jid}", "title": title, "location": "", "posted_at": None,
                     "url": url, "_desc_url": url, "_kind": "html"}
    return [j for j in jobs.values() if wanted(j)]


def workable(token):
    data = as_json(get(f"https://apply.workable.com/api/v1/widget/accounts/{token}", params={"details": "true"}))
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    out = []
    for j in data["jobs"]:
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        if j.get("telecommuting"):
            loc = f"{loc} (Remote)".strip()
        code = j.get("shortcode") or j.get("id")
        job = {"id": f"wk:{token}:{code}", "title": j.get("title", ""), "location": loc,
               "posted_at": j.get("published_on") or j.get("created_at"),
               "url": j.get("url") or j.get("application_url") or f"https://apply.workable.com/{token}/j/{code}/",
               "_desc": html_to_text(j.get("description", "")) + " " + html_to_text(j.get("requirements", "")),
               "_desc_url": f"https://apply.workable.com/api/v2/accounts/{token}/jobs/{code}"}
        if not job["_desc"].strip():
            job.pop("_desc")
        if wanted(job):
            out.append(job)
    return out


def smartrecruiters(token):
    out, offset, any_ok = [], 0, False
    while offset < 300:
        data = as_json(get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                           params={"limit": 100, "offset": offset, "country": "us"}))
        if not isinstance(data, dict) or "content" not in data:
            break
        any_ok = True
        for j in data["content"]:
            loc = j.get("location") or {}
            where = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            if loc.get("remote"):
                where = f"{where} (Remote)".strip()
            job = {"id": f"sr:{j.get('id')}", "title": j.get("name", ""), "location": where,
                   "emp_type": (j.get("typeOfEmployment") or {}).get("label", "") if isinstance(j.get("typeOfEmployment"), dict) else "",
                   "posted_at": j.get("releasedDate"),
                   "url": f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
                   "_desc_url": j.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{j.get('id')}"}
            if wanted(job):
                out.append(job)
        if len(data["content"]) < 100:
            break
        offset += 100
    if not any_ok:
        return None
    return out


FETCHERS = {"ashby": ashby, "greenhouse": greenhouse, "lever": lever, "workday": workday, "yc": yc_page,
            "workable": workable, "smartrecruiters": smartrecruiters}


def json_text(x):
    """All text inside a JSON value, HTML stripped."""
    if isinstance(x, str):
        return html_to_text(x) if "<" in x or "&lt;" in x else x
    if isinstance(x, dict):
        return " ".join(json_text(v) for v in x.values())
    if isinstance(x, list):
        return " ".join(json_text(v) for v in x)
    return ""


WD_URL = re.compile(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.+?)/?(?:\?.*)?$")


def describe(j):
    """Fetch a job's description text (only called for a handful of new jobs per run)."""
    if j.get("_desc"):
        return j["_desc"]
    url = j.get("_desc_url")
    if not url:
        return ""
    m = WD_URL.match(url)
    if m and "/wday/cxs/" not in url:
        tenant, shard, site, path = m.group(1), m.group(2), m.group(3), m.group(4)
        url = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
        j["_kind"] = "workday"
    r = get(url, headers={"Accept": "application/json"} if j.get("_kind") == "workday" else None)
    if not r:
        return ""
    if j.get("_kind") == "workday":
        info = (as_json(r) or {}).get("jobPostingInfo") or {}
        if info.get("location"):
            j["location"] = info["location"]
        country = (info.get("country") or {}).get("descriptor") if isinstance(info.get("country"), dict) else None
        if country:  # Workday often lists only a city ("Vadodara 73 East Ave"); the country settles it
            j["location"] = f"{j.get('location', '')}, {country}".strip(", ")
        return html_to_text(info.get("jobDescription", ""))
    if "json" in (getattr(r, "headers", {}).get("Content-Type") or "") or r.text.lstrip()[:1] in "{[":
        data = as_json(r)
        if data is not None:
            return json_text(data)[:30000]
    soup = BeautifulSoup(r.text, "html.parser")
    for s in soup(["script", "style", "nav", "header", "footer"]):
        s.decompose()
    return soup.get_text(" ", strip=True)[:20000]


# ---------------------------------------------------------------- sources
def simplify_jobs():
    data = as_json(get(SIMPLIFY))
    if not isinstance(data, list):
        log("Simplify: could not load list")
        return []
    out = []
    for x in data:
        if not x.get("active") or not x.get("is_visible", True):
            continue
        sp = x.get("sponsorship") or ""
        if CFG["sponsorship"]["drop_no_sponsorship"] and sp in ("Does Not Offer Sponsorship",
                                                                 "U.S. Citizenship is Required"):
            continue
        ts = x.get("date_posted")
        job = {"id": f"sim:{x.get('id')}", "title": x.get("title", ""), "company": x.get("company_name", ""),
               "location": "; ".join(x.get("locations") or []),
               "posted_at": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else None,
               "url": x.get("url", ""), "source": "Simplify new-grad", "board": "simplify",
               "_desc_url": x.get("url", ""), "_kind": "html", "newgrad": True,
               "sponsor": sp == "Offers Sponsorship"}
        if wanted(job):
            out.append(job)
    SIMPLIFY_COMPANIES.update(x.get("company_name", "") for x in data if x.get("company_name"))
    log(f"Simplify: {len(out)} active new-grad roles match your filters")
    return out


SIMPLIFY_COMPANIES = set()


# ---------------------------------------------------------------- big-company custom career sites
# These companies don't use a standard job system, so each gets its own small reader.
# If a site changes or blocks us, its count in the log drops to 0 and the rest keeps working.

def _title_from_slug(slug):
    return re.sub(r"\b(Ai|Ml|Llm|Swe|Ios|Api|Cv|Gpu|Ar|Vr|Sde|Ii|Iii)\b", lambda x: x.group(1).upper(),
                  slug.replace("-", " ").title())


def amazon_jobs(q):
    data = as_json(get("https://www.amazon.jobs/en/search.json",
                       params={"base_query": q, "loc_query": "United States", "country": "USA",
                               "sort": "recent", "result_limit": 100, "offset": 0}))
    out = []
    for j in (data or {}).get("jobs") or []:
        try:
            posted = dt.datetime.strptime(j.get("posted_date", ""), "%B %d, %Y").replace(tzinfo=dt.timezone.utc).isoformat()
        except ValueError:
            posted = None
        out.append({"id": f"amzn:{j.get('id_icims') or j.get('id')}", "title": j.get("title", ""),
                    "location": j.get("normalized_location") or j.get("location", ""), "posted_at": posted,
                    "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                    "_desc": " ".join(html_to_text(j.get(k, "")) for k in
                                      ("description", "basic_qualifications", "preferred_qualifications"))})
    return out


def microsoft_jobs(q):
    out = []
    data = as_json(get("https://apply.careers.microsoft.com/api/apply/v2/jobs",
                       params={"domain": "microsoft.com", "query": q, "location": "United States",
                               "sort_by": "timestamp", "start": 0, "num": 50}))
    for j in (data or {}).get("positions") or []:
        ts = j.get("t_create")
        out.append({"id": f"msft:{j.get('id')}", "title": j.get("name", ""),
                    "location": j.get("location") or "; ".join(j.get("locations") or []),
                    "posted_at": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else None,
                    "url": j.get("canonicalPositionUrl") or f"https://apply.careers.microsoft.com/careers/job/{j.get('id')}",
                    "_desc_url": f"https://apply.careers.microsoft.com/api/apply/v2/jobs/{j.get('id')}?domain=microsoft.com"})
    if out:
        return out
    data = as_json(get("https://gcsservices.careers.microsoft.com/search/api/v1/search",
                       params={"q": q, "lc": "United States", "l": "en_us", "pg": 1, "pgSz": 20,
                               "o": "Recent", "flt": "true"}))
    for j in (((data or {}).get("operationResult") or {}).get("result") or {}).get("jobs") or []:
        p = j.get("properties") or {}
        out.append({"id": f"msft:{j.get('jobId')}", "title": j.get("title", ""),
                    "location": p.get("primaryLocation", ""), "posted_at": j.get("postingDate"),
                    "url": f"https://jobs.careers.microsoft.com/global/en/job/{j.get('jobId')}",
                    "_desc": html_to_text(p.get("description", "")) + " " + html_to_text(p.get("qualifications", ""))})
    return out


def google_jobs(q):
    r = get("https://www.google.com/about/careers/applications/jobs/results",
            params={"q": q, "location": "United States", "sort_by": "date"})
    out, ids = [], set()
    for m in re.finditer(r"jobs/results/(\d{6,})-([a-z0-9-]+)", r.text if r else ""):
        if m.group(1) in ids:
            continue
        ids.add(m.group(1))
        url = f"https://www.google.com/about/careers/applications/jobs/results/{m.group(1)}-{m.group(2)}"
        out.append({"id": f"goog:{m.group(1)}", "title": _title_from_slug(m.group(2)), "location": "",
                    "posted_at": None, "url": url, "_desc_url": url, "_kind": "html"})
    return out


def apple_jobs(q):
    r = get("https://jobs.apple.com/en-us/search",
            params={"search": q, "sort": "newest", "location": "united-states-USA"})
    out, ids = [], set()
    text = html.unescape(r.text) if r else ""
    for m in re.finditer(r"/en-us/details/([0-9][0-9-]{5,})/([a-z0-9-]+)", text):
        if m.group(1) in ids:
            continue
        ids.add(m.group(1))
        url = f"https://jobs.apple.com/en-us/details/{m.group(1)}/{m.group(2)}"
        out.append({"id": f"aapl:{m.group(1)}", "title": _title_from_slug(m.group(2)), "location": "",
                    "posted_at": None, "url": url, "_desc_url": url, "_kind": "html"})
    return out


def meta_jobs(q):
    r = get("https://www.metacareers.com/jobs", params={"q": q})
    out, ids = [], set()
    text = r.text if r else ""
    for m in re.finditer(r'"id":"(\d{12,})","title":"([^"]{5,140})"', text):
        if m.group(1) in ids:
            continue
        ids.add(m.group(1))
        url = f"https://www.metacareers.com/jobs/{m.group(1)}/"
        out.append({"id": f"meta:{m.group(1)}", "title": m.group(2).encode().decode("unicode_escape", "ignore"),
                    "location": "", "posted_at": None, "url": url, "_desc_url": url, "_kind": "html"})
    return out


HN_JUNIOR = re.compile(
    r"new[- ]?grads?|recent grad|university grad|college grad|entry[- ]level|early[- ]career|\bjunior\b|"
    r"\bjr\.?\s+(engineer|developer|swe)|all (experience |seniority )?levels|any level|every level|"
    r"\b0\s*[-–+]\s*[1-3]\s*(\+\s*)?years|\b[12]\+?\s*years|\bgrads?\b|\bopt\b|\bstem opt\b", re.I)
HN_VISA = re.compile(r"(^|\|)\s*visa(\s+sponsorship)?\s*(\||$)|\bvisa\s+(sponsorship\s+)?(available|ok|yes|friendly)\b"
                     r"|\bwill sponsor\b|\bsponsor(s|ing)?\s+visas?\b", re.I)


def hn_whos_hiring():
    """Monthly 'Ask HN: Who is hiring?' thread — lots of small/new startups post only here."""
    s = as_json(get("https://hn.algolia.com/api/v1/search_by_date",
                    params={"tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": 5}))
    hits = [h for h in (s or {}).get("hits") or [] if "who is hiring" in (h.get("title") or "").lower()]
    if not hits:
        log("HN Who's Hiring: thread not found")
        return []
    story = hits[0]["objectID"]
    out, page = [], 0
    while page < 10:
        d = as_json(get("https://hn.algolia.com/api/v1/search",
                        params={"tags": f"comment,story_{story}", "hitsPerPage": 1000, "page": page}))
        hs = (d or {}).get("hits") or []
        for h in hs:
            if str(h.get("parent_id")) != str(story):
                continue  # replies, not job posts
            raw = h.get("comment_text") or ""
            text = html_to_text(raw)
            first = html_to_text(re.split(r"<p>", raw, maxsplit=1)[0])[:220]
            parts = [p.strip() for p in re.split(r"\s\|\s", first)]
            company = parts[0][:60] if parts else "HN post"
            is_loc = lambda p: re.search(r"(?i:remote|onsite|on-site|in-office|hybrid|\bsf\b|nyc|\busa\b|united states)|,\s?[A-Z]{2}\b", p)
            loc = " ".join(p for p in parts[1:] if is_loc(p))
            # Most HN posts are for experienced engineers; keep only posts that say they hire junior people.
            if CFG["sources"].get("hn_require_junior", True) and not HN_JUNIOR.search(text):
                continue
            # Role = the parts of the header line that name a role you want; skip posts that only hire senior.
            junior_ok = re.compile(r"junior|new grad|entry|early career|all levels|any level|mid", re.I)
            role_parts = [p for p in parts[1:] if INC.search(p) and not is_loc(p)]
            if role_parts:
                keep = [p for p in role_parts if not EXC.search(p) or junior_ok.search(p)]
                if not keep:
                    continue
                role = " | ".join(keep)[:160]
            else:
                m = INC.search(text)
                if not m:
                    continue
                snip = text[max(0, m.start() - 40):m.end() + 60]
                if EXC.search(snip) and not junior_ok.search(snip):
                    continue
                role = "…" + snip.strip() + "…"
            job = {"id": f"hn:{h['objectID']}", "title": role or first[:160],
                   "company": company, "location": loc, "posted_at": h.get("created_at"),
                   "url": f"https://news.ycombinator.com/item?id={h['objectID']}", "_desc": text,
                   "source": "HN Who's Hiring", "board": f"hn:{story}",
                   "sponsor": bool(HN_VISA.search(first) or HN_VISA.search(text))}
            # The first line lists several roles; match on the whole post instead of a single title.
            us_ok = not NON_US.search(first) or re.search(
                r"\b(us|usa|united states|sf|san francisco|bay area|new york|nyc|seattle|boston|austin)\b", first, re.I)
            if INC.search(text[:600]) and us_ok:
                out.append(job)
        if len(hs) < 1000:
            break
        page += 1
    log(f"HN Who's Hiring: {len(out)} matching posts in this month's thread")
    return out


REMOTE_READ = set()


def remote_boards():
    """Remotive + Himalayas: remote job boards that publish their listings openly."""
    out = []
    d = as_json(get("https://remotive.com/api/remote-jobs", params={"category": "software-dev", "limit": 300}))
    for j in (d or {}).get("jobs") or []:
        out.append({"id": f"remotive:{j.get('id')}", "title": j.get("title", ""), "company": j.get("company_name", ""),
                    "location": j.get("candidate_required_location") or "Remote", "emp_type": j.get("job_type", ""),
                    "posted_at": j.get("publication_date"), "url": j.get("url", ""),
                    "_desc": html_to_text(j.get("description", "")), "source": "Remotive", "board": "remotive"})
    d = as_json(get("https://himalayas.app/jobs/api", params={"limit": 100}))
    for j in (d or {}).get("jobs") or []:
        ts = j.get("pubDate")
        locs = j.get("locationRestrictions") or []
        out.append({"id": f"himalayas:{j.get('guid') or j.get('applicationLink')}", "title": j.get("title", ""),
                    "company": j.get("companyName", ""), "location": "; ".join(locs) if locs else "Remote (worldwide)",
                    "emp_type": j.get("employmentType", ""),
                    "posted_at": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if isinstance(ts, (int, float)) else ts,
                    "url": j.get("applicationLink") or "", "_desc": html_to_text(j.get("description", "")),
                    "source": "Himalayas", "board": "himalayas"})
    REMOTE_READ.clear()
    REMOTE_READ.update(j["board"] for j in out)
    keep = [j for j in out if wanted(j)]
    log(f"Remote boards (Remotive + Himalayas): {len(out)} read, {len(keep)} matching")
    return keep


CONTRACT_TITLE = re.compile(r"\bcontract(or)?\b|contract[- ]to[- ]hire|\bc2h\b|\bc2c\b|\b1099\b|\bw-?2\b|"
                            r"\btemp(orary)?\b|\bfreelance\b|\b\d+\s*[- ]?(month|mo)s?\b", re.I)
CONTRACT_DESC = re.compile(r"contract[- ]to[- ]hire|\bc2c\b|corp[- ]to[- ]corp|\b1099\b|\bw-?2\s+(only|contract|basis)|"
                           r"(this is|this|a)\s+(an?\s+)?\d*\s*[- ]?(month\s+)?contract\s+(role|position|opportunity|assignment)|"
                           r"\bcontract\s+(duration|length|term)\b|duration:\s*\d+\s*(months?|weeks?)", re.I)


def contract_info(j, text=""):
    """Tag contract jobs. Returns a label or None."""
    if re.search(r"contract|temp|freelance", j.get("emp_type") or "", re.I) or CONTRACT_TITLE.search(j["title"]) \
            or (text and CONTRACT_DESC.search(text)):
        blob = f"{j['title']} {j.get('emp_type', '')} {text}"
        if re.search(r"\bc2c\b|corp[- ]to[- ]corp|\b1099\b|freelance", blob, re.I):
            return "📝 contract · ⚠️ C2C/1099: usually not allowed on STEM OPT"
        if re.search(r"\bw-?2\b", blob, re.I):
            return "📝 contract · W-2"
        if re.search(r"contract[- ]to[- ]hire|\bc2h\b", blob, re.I):
            return "📝 contract-to-hire"
        return "📝 contract"
    return None


CUSTOM_SITES = {"Amazon": amazon_jobs, "Microsoft": microsoft_jobs, "Google": google_jobs,
                "Apple": apple_jobs, "Meta": meta_jobs}


def custom_site_jobs():
    def run(item):
        name, fn = item
        found = {}
        for q in CFG["sources"]["custom_queries"]:
            try:
                for j in fn(q):
                    found.setdefault(j["id"], j)
            except Exception as e:  # one broken site must never stop the run
                log(f"{name}: error {e}")
        return name, list(found.values())

    jobs, ok, counts = [], [], {}
    with ThreadPoolExecutor(5) as ex:
        for name, js in ex.map(run, CUSTOM_SITES.items()):
            counts[name] = len(js)
            if js:
                ok.append(f"custom:{name.lower()}")
            for j in js:
                j.update(company=name, board=f"custom:{name.lower()}", source=f"{name} careers")
            jobs += [j for j in js if wanted(j)]
    log(f"Big-company sites (postings read): {counts}; matching: {len(jobs)}")
    return jobs, ok


def slug_candidates(c):
    cands = [c.get("slug") or ""]
    name = c.get("name", "").lower()
    cands += [re.sub(r"[^a-z0-9]", "", name), re.sub(r"[^a-z0-9]+", "-", name).strip("-")]
    host = urlparse(c.get("website") or "").netloc.lower().removeprefix("www.")
    if host:
        cands.append(host.split(".")[0])
    seen, out = set(), []
    for s in cands:
        if len(s) >= 3 and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def discover(c):
    for board in ("ashby", "greenhouse", "lever"):
        for s in slug_candidates(c):
            url = {"ashby": f"https://api.ashbyhq.com/posting-api/job-board/{s}",
                   "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
                   "lever": f"https://api.lever.co/v0/postings/{s}?mode=json"}[board]
            data = as_json(get(url))
            jobs = data.get("jobs") if isinstance(data, dict) else data
            if jobs:
                return board, s
    return None, None


def discover_any(name):
    """Find a job board for a company known only by name. Returns (platform, token) or (None, None)."""
    low = name.lower().replace("&", "and")
    low = re.sub(r"\([^)]*\)", "", low)
    for suf in (" inc", " corporation", " corp", " company", " llc", " technologies", " labs", " ai"):
        low = low.removesuffix(suf)
    cands = []
    for s in (re.sub(r"[^a-z0-9]", "", low), re.sub(r"[^a-z0-9]+", "-", low).strip("-")):
        if len(s) >= 3 and s not in cands:
            cands.append(s)
    sr = re.sub(r"[^A-Za-z0-9]", "", re.sub(r"\([^)]*\)", "", name))
    probes = [("ashby", s) for s in cands] + [("greenhouse", s) for s in cands] + \
             [("lever", s) for s in cands] + [("workable", s) for s in cands] + \
             [("smartrecruiters", x) for x in dict.fromkeys([sr, sr.lower()]) if len(x) >= 3]
    for platform, token in probes:
        url = {"ashby": f"https://api.ashbyhq.com/posting-api/job-board/{token}",
               "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
               "lever": f"https://api.lever.co/v0/postings/{token}?mode=json&limit=1",
               "workable": f"https://apply.workable.com/api/v1/widget/accounts/{token}",
               "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1"}[platform]
        data = as_json(get(url))
        if isinstance(data, list) and data:
            return platform, token
        if isinstance(data, dict) and (data.get("jobs") or data.get("content")):
            return platform, token
    return None, None


def discover_more(boards):
    """Look for job boards of companies from your list + the Simplify list that we don't track yet.
    Done a few hundred companies per run and remembered, so hourly runs stay quick."""
    with open(os.path.join(ROOT, "companies.csv")) as f:
        names = [r["company"] for r in csv.DictReader(f)]
    names += sorted(SIMPLIFY_COMPANIES)
    known_norm = {"".join(tokens(b["company"])) for b in boards.values()}
    known_norm |= {"".join(tokens(b["token"].split("/")[-1])) for b in boards.values()}
    found = load_json("discovered_boards.json", {})
    stale = (NOW - dt.timedelta(days=CFG["sources"]["rediscover_days"])).date().isoformat()
    todo = []
    for n in dict.fromkeys(names):
        k = "".join(tokens(n))
        if not k or k in known_norm:
            continue
        if n not in found or found[n].get("checked", "") < stale:
            todo.append(n)
    todo = todo[:CFG["sources"]["discovery_per_run"]]

    budget_end = time.time() + CFG["sources"]["discovery_seconds"]

    def disc(n):
        if time.time() > budget_end:
            return n, None  # out of time this run; try again next run
        return n, discover_any(n)

    done = 0
    with ThreadPoolExecutor(32) as ex:
        for n, res in ex.map(disc, todo):
            if res is not None:
                found[n] = {"board": res[0], "token": res[1], "checked": TODAY}
                done += 1
    todo = todo[:done]
    save_json("discovered_boards.json", found)
    added = 0
    for n, info in found.items():
        if info.get("board"):
            key = f"{info['board']}:{info['token']}"
            if key not in boards:
                boards[key] = {"platform": info["board"], "token": info["token"], "company": n, "yc": None}
                added += 1
    log(f"Discovery: checked {len(todo)} more companies this run; tracking {added} extra boards "
        f"({sum(1 for v in found.values() if v.get('board'))} found so far)")


def gem_score(c):
    g = CFG["gems"]
    tags = set(c.get("tags") or []) | set(c.get("industries") or [])
    score = 2 if tags & set(g["interest_tags"]) else 0
    score += (c.get("team_size") or 999) <= g["small_team"]
    m = re.search(r"(\d{4})", c.get("batch") or "")
    score += bool(m and int(m.group(1)) >= g["recent_batch_year"])
    score += not c.get("top_company")
    return score


def collect_boards(run_no):
    """Return {board_key: {"platform", "token", "company", "yc": {...} or None}}."""
    boards = {}
    if CFG["sources"]["job_board_directory"]:
        data = as_json(get(DIRECTORY))
        if isinstance(data, list):
            save_json("directory_cache.json", data)
        else:
            data = load_json("directory_cache.json", [])
            log("Directory: download failed, using cached copy")
        for x in data:
            p, t = x.get("platform"), x.get("token")
            if p in ("greenhouse", "ashby", "lever", "workday", "workable") and t:
                boards[f"{p}:{t}"] = {"platform": p, "token": t, "company": x.get("company") or t, "yc": None}
        log(f"Directory: {len(boards)} boards")

    if CFG["sources"]["yc"]:
        data = as_json(get(YC_DATA)) or []
        ycfg = CFG["yc"]
        companies = [c for c in data if set(c.get("regions") or []) & set(ycfg["regions"])]
        known = load_json("yc_boards.json", {})
        stale = (NOW - dt.timedelta(days=ycfg["rediscover_days"])).date().isoformat()
        todo = [c for c in companies if c["slug"] not in known or known[c["slug"]].get("checked", "") < stale]
        log(f"YC: {len(companies)} hiring companies, discovering boards for {len(todo)}")

        yc_end = time.time() + CFG["sources"]["discovery_seconds"]

        def disc(c):
            if time.time() > yc_end:
                return c["slug"], None  # out of time; continue next run
            b, s = discover(c)
            return c["slug"], {"board": b, "slug": s, "checked": TODAY}

        with ThreadPoolExecutor(32) as ex:
            for slug, info in ex.map(disc, todo):
                if info:
                    known[slug] = info
        save_json("yc_boards.json", known)
        for c in companies:
            info = known.get(c["slug"], {})
            yc = {"batch": c.get("batch", ""), "team_size": c.get("team_size"),
                  "one_liner": c.get("one_liner", ""), "gem_score": gem_score(c)}
            if info.get("board"):
                key = f"{info['board']}:{info['slug']}"
                boards.setdefault(key, {"platform": info["board"], "token": info["slug"]})
                boards[key].update(company=c["name"], yc=yc)
            elif ycfg["check_yc_page_for_unknown"]:
                boards[f"yc:{c['slug']}"] = {"platform": "yc", "token": c["slug"], "company": c["name"], "yc": yc}

    if CFG["sources"]["discover_more"]:
        discover_more(boards)

    # Workday is slow, so each run checks a different slice of Workday boards (all covered every N runs).
    k = max(CFG["sources"]["workday_every_n_runs"], 1)
    boards = {key: v for key, v in boards.items()
              if v["platform"] != "workday" or zlib.crc32(key.encode()) % k == run_no % k}
    return boards


def board_jobs(boards):
    def fetch(item):
        key, b = item
        return key, b, FETCHERS[b["platform"]](b["token"])

    fast = [(k, b) for k, b in boards.items() if b["platform"] != "workday"]
    slow = [(k, b) for k, b in boards.items() if b["platform"] == "workday"]
    jobs, ok_boards = [], []
    for items, threads in ((fast, 48), (slow, 12)):
        with ThreadPoolExecutor(threads) as ex:
            for key, b, js in ex.map(fetch, items):
                if js is None:
                    continue
                ok_boards.append(key)
                name = b["company"]
                if name.islower():
                    name = name.replace("-", " ").title()
                for j in js:
                    j.update(company=name, board=key, source=b["platform"].title())
                    if b.get("yc"):
                        j.update(yc_batch=b["yc"]["batch"], team_size=b["yc"]["team_size"],
                                 one_liner=b["yc"]["one_liner"], gem_score=b["yc"]["gem_score"],
                                 gem=b["yc"]["gem_score"] >= CFG["gems"]["min_score"])
                    jobs.append(j)
    by = {}
    for k in ok_boards:
        p = boards[k]["platform"]
        by[p] = by.get(p, 0) + 1
    log(f"Boards read: {by}; matching open jobs: {len(jobs)}")
    return jobs, ok_boards


# ---------------------------------------------------------------- output
def ago(iso):
    t = parse_time(iso)
    if not t:
        return ""
    mins = int((NOW - t).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


def badges(j):
    b = []
    if j.get("contract"):
        b.append(j["contract"])
    if j.get("sponsor"):
        b.append("🛂 mentions sponsorship/OPT")
    if j.get("gem"):
        b.append("⭐ gem")
    if j.get("yc_batch"):
        b.append(f"YC {j['yc_batch']}")
    if j.get("mine"):
        b.append("★ your list")
    if j.get("years") is not None:
        b.append(f"{j['years']}+ yrs")
    if j.get("checked") is False:
        b.append("⚠️ couldn't read description: check years/sponsorship")
    if j.get("team_size"):
        b.append(f"{j['team_size']} people")
    return b


def row_html(j):
    td = "padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top"
    sub = html.escape(j.get("one_liner") or "")
    return (
        f"<tr><td style='{td}'><b>{html.escape(j['company'])}</b>"
        f"<br><span style='color:#777;font-size:12px'>{html.escape(' · '.join(badges(j)))}</span>"
        + (f"<br><span style='color:#999;font-size:12px'>{sub}</span>" if sub else "")
        + f"</td><td style='{td}'><a href='{html.escape(j['url'])}'>{html.escape(j['title'])}</a></td>"
        f"<td style='{td};font-size:13px'>{html.escape(j.get('location') or '—')}</td>"
        f"<td style='{td};font-size:13px;color:#555'>{html.escape(ago(j.get('posted_at')) or 'new')}<br>"
        f"<span style='color:#999'>{html.escape(j['source'])}</span></td></tr>"
    )


def build_email(sections):
    parts = ["<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:920px'>"]
    total = sum(len(v) for _, v in sections)
    parts.append(f"<p style='color:#555'>{total} new jobs for you · checked {NOW:%b %d, %H:%M} UTC · "
                 f"new-grad level, sponsorship not ruled out, US/remote</p>")
    for name, jobs in sections:
        if jobs:
            parts.append(f"<h3 style='margin:18px 0 6px'>{html.escape(name)} ({len(jobs)})</h3>"
                         "<table style='border-collapse:collapse;width:100%'>")
            parts += [row_html(j) for j in jobs]
            parts.append("</table>")
    parts.append("</div>")
    text = "\n".join(f"[{n}] {j['company']} | {j['title']} | {j.get('location', '')} | {j['url']}"
                     for n, js in sections for j in js)
    return "".join(parts), text, total


def send_email(subject, html_body, text_body):
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("TO_EMAIL") or user
    if not (user and pw):
        log("Email not configured; skipping")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())
    log(f"Emailed {to}")


def push(title, body):
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                          headers={"Title": title.encode("ascii", "ignore").decode()}, timeout=15)
        except requests.RequestException as e:
            log(f"ntfy failed: {e}")


def write_csv(name, jobs):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "title", "location", "posted", "source", "tags", "apply_url", "what_they_do",
                    "why_dropped"])
        for j in jobs:
            w.writerow([j.get("company", ""), j["title"], j.get("location", ""), (j.get("posted_at") or "")[:10],
                        j.get("source", ""), " · ".join(badges(j)), j["url"], j.get("one_liner", ""),
                        j.get("why", "")])


DROPPED = []  # new jobs removed by the description checks this run (saved to dropped_this_run.csv)


def check_job(j):
    """Read the description; return (job, reason_dropped or None)."""
    text = describe(j)
    if not loc_ok(j.get("location", "")):
        return j, "outside US"
    j["years"] = min_years(text) if text else None
    j["checked"] = bool(text)
    if text and CFG["sponsorship"]["drop_no_sponsorship"] and NO_SPONSOR.search(text):
        return j, "no sponsorship"
    if j["years"] is not None and j["years"] > CFG["experience"]["max_required_years"]:
        return j, "too many years"
    # "At least 9 years..." in the basic requirements, even if "3+ years of AWS" appears lower down.
    top = max_years(text) if text else None
    if top is not None and top >= CFG["experience"].get("hard_cap_years", 6):
        j["years"] = top
        return j, "too many years"
    if text and CFG["experience"].get("drop_phd_required", True) and phd_required(text):
        return j, "PhD required"
    # No number given, but written for senior people ("experienced engineer to help lead...").
    lim = CFG["experience"].get("max_senior_signals", 1)
    if text and j["years"] is None and senior_signals(text) > lim:
        return j, "senior wording"
    if text and (SPONSOR_FRIENDLY.search(text) or OPT_FRIENDLY.search(text)):
        j["sponsor"] = True
    j["contract"] = contract_info(j, text)
    if j["contract"] and not CFG["contract"]["include"]:
        return j, "contract"
    return j, None


def collect_jobs(run_no):
    jobs, ok_boards = [], []
    if CFG["sources"]["simplify_new_grad"]:
        sj = simplify_jobs()
        jobs += sj
        if sj:
            ok_boards.append("simplify")
    bj, bk = board_jobs(collect_boards(run_no))
    jobs += bj
    ok_boards += bk
    if CFG["sources"]["custom_sites"]:
        cj, ck = custom_site_jobs()
        jobs += cj
        ok_boards += ck
    if CFG["sources"].get("remote_boards", True):
        rj = remote_boards()
        jobs += rj
        ok_boards += sorted(REMOTE_READ)
    if CFG["sources"]["hn_whos_hiring"]:
        hj = hn_whos_hiring()
        jobs += hj
        ok_boards += list({j["board"] for j in hj})

    # Drop duplicates (same company + title seen through two sources). Prefer the board copy.
    dedup = {}
    for j in sorted(jobs, key=lambda j: j["source"] == "Simplify new-grad"):
        k = ("".join(tokens(j["company"])), re.sub(r"\W+", "", j["title"].lower()))
        dedup.setdefault(k, j)
        if j["source"] == "Simplify new-grad" and dedup[k] is not j:
            dedup[k]["newgrad"] = True
            dedup[k]["sponsor"] = dedup[k].get("sponsor") or j.get("sponsor")
    return list(dedup.values()), ok_boards


def mine_index():
    with open(os.path.join(ROOT, "companies.csv")) as f:
        return CompanyIndex([r["company"] for r in csv.DictReader(f)])


FOCUS = re.compile(r"machine learning|\bml\b|\bai\b|computer vision|vision|perception|robotic|deep learning|"
                   r"applied scientist|research engineer|inference|llm", re.I)


def fit_score(j):
    """How good a fit a job is for you; higher is better."""
    s = 0
    s += 3 if j.get("sponsor") else 0
    s += 2 if j.get("newgrad") or re.search(r"new grad|university|entry|early career|junior|\b(i|1)\b",
                                             j["title"], re.I) else 0
    s += 2 if FOCUS.search(j["title"]) else 0
    s += 1 if j.get("years") in (0, 1) else 0
    s += 1 if j.get("mine") else 0
    s += 1 if j.get("gem") else 0
    t = parse_time(j.get("posted_at"))
    if t:
        age = (NOW - t).days
        s += 2 if age <= 3 else 1 if age <= 7 else 0
    return s


# ---------------------------------------------------------------- research + tech news (daily email)
def trending_papers():
    """Today's most-upvoted papers on Hugging Face Daily Papers that touch vision / robotics / your niche."""
    rx = re.compile(CFG["research"]["topic_regex"], re.I)
    shown = load_json("papers_shown.json", {})
    out = []
    for back in range(0, 3):  # today, or the last day that has papers (weekends are empty)
        day = (NOW - dt.timedelta(days=back)).date().isoformat()
        data = as_json(get("https://huggingface.co/api/daily_papers", params={"date": day}))
        if isinstance(data, list) and data:
            break
    for x in data if isinstance(data, list) else []:
        p = x.get("paper") or {}
        pid, title, summ = p.get("id"), (p.get("title") or x.get("title") or "").strip(), p.get("summary") or ""
        if not pid or pid in shown or not rx.search(f"{title} {summ}"):
            continue
        out.append({"title": title, "url": f"https://huggingface.co/papers/{pid}",
                    "note": f"▲{p.get('upvotes', 0)} · " + re.sub(r"\s+", " ", summ)[:230] + "…",
                    "_up": p.get("upvotes", 0), "_id": pid})
    out.sort(key=lambda x: -x["_up"])
    return out


def niche_papers():
    """New arXiv papers in computer vision / robotics matching your specific interests."""
    import xml.etree.ElementTree as ET
    rx = re.compile(CFG["research"]["niche_regex"], re.I)
    out = []
    for cat in ("cs.CV", "cs.RO"):
        r = get(f"https://rss.arxiv.org/rss/{cat}")
        if not r:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            desc = html_to_text(it.findtext("description") or "")
            ann = (it.findtext("{http://arxiv.org/schemas/atom}announce_type") or "new").strip()
            if ann != "new" or not rx.search(f"{title} {desc}"):
                continue
            abstract = re.sub(r"^.*?Abstract:\s*", "", desc)
            link = (it.findtext("link") or "").strip()
            if any(o["_id"] == link for o in out):
                continue  # cross-listed in both categories
            out.append({"title": title, "url": (it.findtext("link") or "").strip(),
                        "note": abstract[:230] + "…", "_id": (it.findtext("link") or "").strip()})
    return out


def hn_ai_stories():
    """Most-discussed AI / vision / robotics stories on Hacker News in the last day."""
    d = as_json(get("https://hn.algolia.com/api/v1/search",
                    params={"tags": "story", "numericFilters": f"created_at_i>{int(NOW.timestamp()) - 86400},points>80",
                            "hitsPerPage": 100}))
    rx = re.compile(CFG["research"]["topic_regex"] + r"|\b(ai|llm|model|gpu|nvidia|openai|anthropic)\b", re.I)
    out = []
    for h in (d or {}).get("hits") or []:
        t = h.get("title") or ""
        if rx.search(t):
            out.append({"title": f"{t} ({h.get('points', 0)} pts)",
                        "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                        "note": f"Discussion: https://news.ycombinator.com/item?id={h['objectID']}",
                        "_up": h.get("points", 0), "_id": h["objectID"]})
    out.sort(key=lambda x: -x["_up"])
    return out


def research_sections():
    if not CFG["research"]["enabled"]:
        return ""
    shown = load_json("papers_shown.json", {})
    n = CFG["research"]["max_items"]
    sections = []
    for name, fn in (("📄 Trending vision & robotics papers today", trending_papers),
                     ("🔬 New papers in your niche (arXiv)", niche_papers),
                     ("📰 Top AI stories on Hacker News", hn_ai_stories)):
        try:
            items = [x for x in fn() if x["_id"] not in shown][:n]
        except Exception as e:  # never let news break the job email
            log(f"{name}: error {e}")
            items = []
        log(f"{name}: {len(items)}")
        for x in items:
            shown[x["_id"]] = TODAY
        sections.append(links_html(name, items))
    cutoff = (NOW - dt.timedelta(days=30)).date().isoformat()
    save_json("papers_shown.json", {k: v for k, v in shown.items() if v >= cutoff})
    return "".join(sections)


NEWS_FEEDS = ["https://techcrunch.com/category/venture/feed/",
              "https://techcrunch.com/category/startups/feed/",
              "https://news.crunchbase.com/feed/"]
RAISE = re.compile(r"\b(raises?|raised|funding|seed|series [a-c]|secures|lands|nabs|bags|closes)\b", re.I)


def funding_news():
    """Startups that just announced funding, in your areas, from the last ~2 days."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    interests = re.compile(CFG["news"]["interest_regex"], re.I)
    sent = load_json("news_sent.json", {})
    items = {}
    for feed in NEWS_FEEDS:
        r = get(feed)
        if not r:
            log(f"News: could not read {feed}")
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            desc = html_to_text(it.findtext("description") or "")
            try:
                when = parsedate_to_datetime(it.findtext("pubDate") or "")
            except (TypeError, ValueError):
                when = None
            if not link or link in sent or (when and NOW - when > dt.timedelta(days=2)):
                continue
            if RAISE.search(title) and interests.search(f"{title} {desc}"):
                items[link] = {"title": title, "url": link, "note": desc[:220]}
    out = list(items.values())[:CFG["news"]["max_items"]]
    for x in out:
        sent[x["url"]] = TODAY
    cutoff = (NOW - dt.timedelta(days=30)).date().isoformat()
    save_json("news_sent.json", {k: v for k, v in sent.items() if v >= cutoff})
    log(f"News: {len(out)} funding stories in your areas")
    return out


SEASON = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


def new_yc_companies():
    """Companies from the newest YC batches that match your interests (each shown once)."""
    data = as_json(get(YC_DATA)) or []
    shown = load_json("yc_shown.json", {})

    def bkey(b):
        m = re.match(r"(\w+)\s+(\d{4})", b or "")
        return (int(m.group(2)), SEASON.get(m.group(1).lower(), 0)) if m else (0, 0)

    batches = sorted({c.get("batch") for c in data if c.get("batch")}, key=bkey, reverse=True)[:2]
    want = set(CFG["gems"]["interest_tags"])
    picks = [c for c in data if c.get("batch") in batches and c["slug"] not in shown
             and (set(c.get("tags") or []) | set(c.get("industries") or [])) & want
             and set(c.get("regions") or []) & set(CFG["yc"]["regions"])]
    picks = picks[:CFG["news"]["max_items"]]
    for c in picks:
        shown[c["slug"]] = TODAY
    save_json("yc_shown.json", shown)
    log(f"New YC companies: {len(picks)} from {batches}")
    return [{"title": f"{c['name']} ({c.get('batch')}, {c.get('team_size') or '?'} people)",
             "url": c.get("url") or f"https://www.ycombinator.com/companies/{c['slug']}",
             "note": c.get("one_liner", "")} for c in picks]


def links_html(heading, items):
    if not items:
        return ""
    rows = "".join(
        f"<li style='margin:6px 0'><a href='{html.escape(x['url'])}'>{html.escape(x['title'])}</a>"
        + (f"<br><span style='color:#777;font-size:13px'>{html.escape(x['note'])}</span>" if x.get("note") else "")
        + "</li>" for x in items)
    return f"<h3 style='margin:18px 0 6px'>{html.escape(heading)} ({len(items)})</h3><ul>{rows}</ul>"


def digest():
    """Once a day: fully check every open match (not just new ones) and email the best ones."""
    n = int(os.environ.get("DIGEST_SIZE", "25"))
    jobs, _ = collect_jobs(1)  # skip Workday's slow per-board search in the digest
    already = load_json("digest_sent.json", {})
    mine = mine_index()
    pool = []
    for j in jobs:
        t = parse_time(j.get("posted_at"))
        if j["id"] in already or (t and (NOW - t).days > CFG["digest"]["max_age_days"]):
            continue
        j["mine"] = mine.match(j["company"])
        pool.append(j)
    # Pre-rank on what we know without descriptions, then read descriptions for the best few hundred.
    pool.sort(key=fit_score, reverse=True)
    all_pool = pool
    pool = pool[:600]
    kept, dropped = [], {}
    with ThreadPoolExecutor(16) as ex:
        for j, why in ex.map(check_job, pool):
            if why:
                dropped[why] = dropped.get(why, 0) + 1
                j["why"] = why
                DROPPED.append(j)
            else:
                kept.append(j)
    kept.sort(key=fit_score, reverse=True)
    # At most N jobs per company, so one startup can't fill the list.
    per, top = {}, []
    for j in kept:
        c = "".join(tokens(j["company"]))
        if per.get(c, 0) < CFG["digest"]["max_per_company"]:
            per[c] = per.get(c, 0) + 1
            top.append(j)
        if len(top) >= n:
            break
    # Always include a separate YC startup section, best gems first.
    yc_n = int(os.environ.get("DIGEST_YC_SIZE", "10"))
    top_ids = {j["id"] for j in top}
    yc_cands = [j for j in all_pool if j.get("yc_batch") and j["id"] not in top_ids]
    yc_cands.sort(key=lambda j: (j.get("gem_score", 0), fit_score(j)), reverse=True)
    yc_top = []
    with ThreadPoolExecutor(16) as ex:
        for j, why in ex.map(check_job, yc_cands[:yc_n * 5]):
            if not why and len(yc_top) < yc_n:
                yc_top.append(j)
    yc_top.sort(key=lambda j: (j.get("gem_score", 0), fit_score(j)), reverse=True)
    log(f"Digest: {len(pool)} checked, dropped {dropped}, sending top {len(top)} + {len(yc_top)} YC")
    top_all = top + yc_top
    for j in top_all:
        already[j["id"]] = TODAY
    cutoff = (NOW - dt.timedelta(days=45)).date().isoformat()
    save_json("digest_sent.json", {k: v for k, v in already.items() if v >= cutoff})
    write_csv("digest_full_ranked.csv", kept)
    write_csv("dropped_this_run.csv", DROPPED)
    news = funding_news() if CFG["news"]["funding_news"] else []
    new_yc = new_yc_companies() if CFG["news"]["new_yc_companies"] else []
    research = research_sections()
    if not (top_all or news or new_yc or research):
        return
    html_body, text_body, total = build_email([("Best open jobs for you today, ranked", top),
                                               ("YC startups worth a look (⭐ gems first)", yc_top)])
    html_body = html_body.replace("new jobs for you", "top picks from all open jobs", 1)
    extra = links_html("💰 Just raised money: reach out before they post jobs", news) + \
        links_html("🆕 Newest YC companies in your areas", new_yc) + research
    html_body = html_body[:-len("</div>")] + extra + "</div>"
    text_body += "\n\n" + "\n".join(f"{x['title']} | {x['url']}" for x in news + new_yc)
    send_email(f"Today's top {len(top)} jobs + {len(yc_top)} YC startups"
               + (f" + {len(news)} funding news" if news else ""), html_body, text_body)
    push(f"Today's top {total} jobs", "\n".join(f"{j['company']}: {j['title']}" for j in top[:10]))


# ---------------------------------------------------------------- main
def main():
    if os.environ.get("MODE") == "digest":
        return digest()
    meta = load_json("meta.json", {"runs": 0})
    run_no = meta["runs"]
    seen = load_json("seen.json", {})
    seeded = set(load_json("seeded_boards.json", []))
    first_run = not seen
    mine = mine_index()
    jobs, ok_boards = collect_jobs(run_no)

    new = []
    for j in jobs:
        # Same job can arrive from two sources with different IDs (company board + Simplify list),
        # so also remember it by company + title.
        ck = "ct:" + "".join(tokens(j["company"])) + "|" + re.sub(r"\W+", "", j["title"].lower())
        fresh = j["id"] not in seen and ck not in seen
        seen[j["id"]] = TODAY
        seen[ck] = TODAY
        # HN threads are monthly and most posts land on day 1, so they're not baselined (the 3-day age cap applies).
        if not fresh or first_run or (j["board"] not in seeded and not j["board"].startswith("hn:")):
            continue
        t = parse_time(j.get("posted_at"))
        if t and NOW - t > OLD_AFTER:
            continue
        new.append(j)
    seeded |= set(ok_boards)

    # Read descriptions of new jobs: experience + sponsorship checks.
    kept, dropped = [], {}
    with ThreadPoolExecutor(12) as ex:
        for j, why in ex.map(check_job, new):
            if why:
                dropped[why] = dropped.get(why, 0) + 1
                j["why"] = why
                DROPPED.append(j)
            else:
                j["mine"] = mine.match(j["company"])
                kept.append(j)
    kept.sort(key=fit_score, reverse=True)
    log(f"New jobs: {len(new)}; dropped {dropped}; sending {len(kept)}")

    cutoff = (NOW - dt.timedelta(days=14)).date().isoformat()
    save_json("seen.json", {k: v for k, v in seen.items() if v >= cutoff})
    save_json("seeded_boards.json", sorted(seeded))
    meta["runs"] = run_no + 1
    save_json("meta.json", meta)

    yc_open = sorted([j for j in jobs if j.get("yc_batch")], key=lambda j: j.get("posted_at") or "", reverse=True)
    yc_open.sort(key=lambda j: -j.get("gem_score", 0))
    write_csv("open_yc_jobs.csv", yc_open)
    write_csv("open_newgrad_jobs.csv", sorted([j for j in jobs if j.get("newgrad")],
                                              key=lambda j: j.get("posted_at") or "", reverse=True))
    write_csv("sent_this_run.csv", kept)
    write_csv("dropped_this_run.csv", DROPPED)
    skipped = [dict(j, why="off-target word in title") for j in TITLE_SKIPPED.values()]
    write_csv("title_skipped_open.csv", skipped)

    if first_run:
        subj = "Job watcher is live"
        body = (f"<p>Baseline saved: {len(jobs)} matching open jobs across {len(ok_boards)} job boards. "
                f"From the next run you'll only get NEW postings. The full list of currently open matches "
                f"is in this run's 'open-jobs' download on GitHub.</p>")
        send_email(subj, body, re.sub("<[^>]+>", "", body))
        push(subj, f"Watching {len(ok_boards)} job boards")
        return

    order = lambda j: (not j.get("sponsor"), not j.get("newgrad"), -j.get("gem_score", 0))
    contract = sorted([j for j in kept if j.get("contract")], key=lambda j: "C2C" in j["contract"])
    kept = [j for j in kept if not j.get("contract")]
    newgrad = sorted([j for j in kept if j.get("newgrad")], key=order)
    yc = sorted([j for j in kept if j.get("yc_batch") and not j.get("newgrad")], key=order)
    hn = sorted([j for j in kept if j["source"] == "HN Who's Hiring" and not j.get("newgrad")], key=order)
    rest = sorted([j for j in kept if not j.get("newgrad") and not j.get("yc_batch") and j not in hn],
                  key=lambda j: (not j.get("mine"), not j.get("sponsor")))
    sections = [("New-grad roles", newgrad), ("YC startups", yc), ("Company career sites", rest),
                ("Startups hiring on Hacker News (click to read the post; apply as it says)", hn),
                ("📝 Contract jobs (check with USF's international office before accepting)", contract)]
    cap, trimmed = CFG["notify"]["max_jobs_per_email"], []
    for name, js in sections:
        trimmed.append((name, js[:max(cap, 0)]))
        cap -= len(js)
    html_body, text_body, total = build_email(trimmed)
    if total == 0 and not CFG["notify"]["send_when_empty"]:
        return
    spons = sum(1 for _, js in trimmed for j in js if j.get("sponsor"))
    subj = f"{total} new job{'s' if total != 1 else ''}" + (f" · {spons} mention sponsorship" if spons else "")
    send_email(subj, html_body, text_body)
    push(subj, "\n".join(f"{j['company']}: {j['title']}" for _, js in trimmed for j in js[:10])[:3000])


if __name__ == "__main__":
    main()
