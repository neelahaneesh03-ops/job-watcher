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


def log(*a):
    print(*a, file=sys.stderr, flush=True)


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


def title_ok(t):
    return bool(t) and bool(INC.search(t)) and not EXC.search(t)


def loc_ok(loc):
    if CFG["location"]["allow_non_us"] or not loc:
        return True
    for p in re.split(r"[;|/•]| or |\n", loc):
        p = p.strip()
        if not p or US_EXPLICIT.search(p) or not NON_US.search(p):
            return True
    return False


def wanted(j):
    return title_ok(j.get("title", "")) and loc_ok(j.get("location", ""))


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


def min_years(text):
    """Smallest number of years of experience the posting mentions, or None."""
    vals = []
    for m in YEARS.finditer(text):
        a = m.group(1).lower()
        n = WORDNUM.get(a) if not a.isdigit() else int(a)
        if n is not None and n <= 20:
            vals.append(n)
    return min(vals) if vals else None


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
        job = {"id": f"lever:{j.get('id')}", "title": j.get("text", ""),
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


FETCHERS = {"ashby": ashby, "greenhouse": greenhouse, "lever": lever, "workday": workday, "yc": yc_page}


def describe(j):
    """Fetch a job's description text (only called for a handful of new jobs per run)."""
    if j.get("_desc"):
        return j["_desc"]
    url = j.get("_desc_url")
    if not url:
        return ""
    r = get(url, headers={"Accept": "application/json"} if j.get("_kind") == "workday" else None)
    if not r:
        return ""
    if j.get("_kind") == "workday":
        info = (as_json(r) or {}).get("jobPostingInfo") or {}
        if info.get("location"):
            j["location"] = info["location"]
        return html_to_text(info.get("jobDescription", ""))
    if "greenhouse.io" in url:
        return html_to_text((as_json(r) or {}).get("content", ""))
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
    log(f"Simplify: {len(out)} active new-grad roles match your filters")
    return out


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
            if p in ("greenhouse", "ashby", "lever", "workday") and t:
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

        def disc(c):
            b, s = discover(c)
            return c["slug"], {"board": b, "slug": s, "checked": TODAY}

        with ThreadPoolExecutor(16) as ex:
            for slug, info in ex.map(disc, todo):
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

    do_workday = run_no % max(CFG["sources"]["workday_every_n_runs"], 1) == 0
    if not do_workday:
        boards = {k: v for k, v in boards.items() if v["platform"] != "workday"}
    return boards


def board_jobs(boards):
    def fetch(item):
        key, b = item
        return key, b, FETCHERS[b["platform"]](b["token"])

    fast = [(k, b) for k, b in boards.items() if b["platform"] != "workday"]
    slow = [(k, b) for k, b in boards.items() if b["platform"] == "workday"]
    jobs, ok_boards = [], []
    for items, threads in ((fast, 24), (slow, 6)):
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
        w.writerow(["company", "title", "location", "posted", "source", "tags", "apply_url", "what_they_do"])
        for j in jobs:
            w.writerow([j["company"], j["title"], j.get("location", ""), (j.get("posted_at") or "")[:10],
                        j["source"], " · ".join(badges(j)), j["url"], j.get("one_liner", "")])


def check_job(j):
    """Read the description; return (job, reason_dropped or None)."""
    text = describe(j)
    j["years"] = min_years(text) if text else None
    if text and CFG["sponsorship"]["drop_no_sponsorship"] and NO_SPONSOR.search(text):
        return j, "no sponsorship"
    if j["years"] is not None and j["years"] > CFG["experience"]["max_required_years"]:
        return j, "too many years"
    if text and (SPONSOR_FRIENDLY.search(text) or OPT_FRIENDLY.search(text)):
        j["sponsor"] = True
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


def digest():
    """Once a day: fully check every open match (not just new ones) and email the best ones."""
    n = int(os.environ.get("DIGEST_SIZE", "25"))
    jobs, _ = collect_jobs(1)  # skip Workday's slow per-board search in the digest
    already = load_json("digest_sent.json", {})
    mine = mine_index()
    pool = []
    for j in jobs:
        t = parse_time(j.get("posted_at"))
        if j["id"] in already or (t and (NOW - t).days > 30):
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
            else:
                kept.append(j)
    kept.sort(key=fit_score, reverse=True)
    top = kept[:n]
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
    if not top_all:
        return
    html_body, text_body, total = build_email([("Best open jobs for you today, ranked", top),
                                               ("YC startups worth a look (⭐ gems first)", yc_top)])
    html_body = html_body.replace("new jobs for you", "top picks from all open jobs", 1)
    send_email(f"Today's top {len(top)} jobs + {len(yc_top)} YC startups for you", html_body, text_body)
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
        fresh = j["id"] not in seen
        seen[j["id"]] = TODAY
        if not fresh or first_run or j["board"] not in seeded:
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

    if first_run:
        subj = "Job watcher is live"
        body = (f"<p>Baseline saved: {len(jobs)} matching open jobs across {len(ok_boards)} job boards. "
                f"From the next run you'll only get NEW postings. The full list of currently open matches "
                f"is in this run's 'open-jobs' download on GitHub.</p>")
        send_email(subj, body, re.sub("<[^>]+>", "", body))
        push(subj, f"Watching {len(ok_boards)} job boards")
        return

    order = lambda j: (not j.get("sponsor"), not j.get("newgrad"), -j.get("gem_score", 0))
    newgrad = sorted([j for j in kept if j.get("newgrad")], key=order)
    yc = sorted([j for j in kept if j.get("yc_batch") and not j.get("newgrad")], key=order)
    rest = sorted([j for j in kept if not j.get("newgrad") and not j.get("yc_batch")],
                  key=lambda j: (not j.get("mine"), not j.get("sponsor")))
    sections = [("New-grad roles", newgrad), ("YC startups", yc), ("Company career sites", rest)]
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
