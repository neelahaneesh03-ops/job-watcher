"""Hourly job watcher.

Sources
  1. LinkedIn public job search, limited to the last hour. Jobs at companies in
     companies.csv (your Fortune 500 / big-company list) are flagged.
  2. Every currently hiring YC company, checked directly on its own job board
     (Ashby, Greenhouse, Lever) or, if none is found, its ycombinator.com jobs page.

A job counts as new the first time its ID is seen. Anything new that passes the role and
location filters is emailed (and optionally pushed to your phone via ntfy).
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
with open(os.path.join(ROOT, "config.toml"), "rb") as f:
    CFG = tomllib.load(f)

NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
YC_DATA = "https://raw.githubusercontent.com/yc-oss/api/main/companies/hiring.json"

session = requests.Session()
session.headers["User-Agent"] = UA
adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
session.mount("https://", adapter)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, **kw):
    for attempt in range(2):
        try:
            r = session.get(url, timeout=20, **kw)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1)
    return None


def load_json(name, default):
    p = os.path.join(STATE, name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default


def save_json(name, data):
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, name), "w") as f:
        json.dump(data, f, indent=0, sort_keys=True)


# ---------------------------------------------------------------- filters
INC = re.compile(CFG["roles"]["include_regex"], re.I)
EXC = re.compile(CFG["roles"]["exclude_regex"], re.I)
US_EXPLICIT = re.compile(r"united states|\busa?\b|\bu\.s\.", re.I)
NON_US = re.compile(
    r"\b(india|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|gurgaon|noida|"
    r"united kingdom|\buk\b|england|london|scotland|canada|toronto|vancouver|montreal|"
    r"germany|berlin|munich|france|paris|spain|madrid|barcelona|portugal|lisbon|italy|"
    r"netherlands|amsterdam|ireland|dublin|switzerland|zurich|sweden|stockholm|poland|"
    r"warsaw|europe|emea|apac|latam|singapore|australia|sydney|melbourne|japan|tokyo|"
    r"korea|seoul|china|beijing|shanghai|hong kong|taiwan|israel|tel aviv|brazil|"
    r"são paulo|sao paulo|mexico|argentina|colombia|chile|philippines|vietnam|indonesia|"
    r"nigeria|kenya|egypt|uae|dubai|pakistan|turkey|ukraine|romania)\b", re.I)


def title_ok(t):
    return bool(t) and bool(INC.search(t)) and not EXC.search(t)


def loc_ok(loc):
    if CFG["location"]["allow_non_us"] or not loc:
        return True
    parts = re.split(r"[;|/•]| or |\n", loc)
    for p in parts:
        p = p.strip()
        if not p or US_EXPLICIT.search(p) or not NON_US.search(p):
            return True
    return False


# ---------------------------------------------------------------- company matching
SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd",
          "plc", "group", "holdings", "the", "lp", "sa", "ag", "nv"}


def tokens(name):
    name = name.lower().replace("&", " and ")
    toks = re.findall(r"[a-z0-9]+", name)
    return [t for t in toks if t not in SUFFIX]


class CompanyIndex:
    def __init__(self, names_with_tags):
        self.items = []  # (tokens, nospace, tag)
        for name, tag in names_with_tags:
            variants = [name] + re.findall(r"\(([^)]+)\)", name)
            variants.append(re.sub(r"\([^)]*\)", "", name))
            for v in variants:
                t = tokens(v)
                if t:
                    self.items.append((t, "".join(t), tag))

    def match(self, company):
        ct = tokens(company)
        if not ct:
            return None
        cns = "".join(ct)
        for t, ns, tag in self.items:
            if ns == cns or (len(ns) >= 3 and ct[:len(t)] == t):
                return tag
        return None


# ---------------------------------------------------------------- LinkedIn
def parse_linkedin(text):
    soup = BeautifulSoup(text, "html.parser")
    jobs = []
    for card in soup.select("div.base-card, div.base-search-card"):
        a = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
        title = card.select_one(".base-search-card__title")
        comp = card.select_one(".base-search-card__subtitle")
        loc = card.select_one(".job-search-card__location")
        tm = card.select_one("time")
        if not (a and title):
            continue
        href = a.get("href", "").split("?")[0]
        urn = card.get("data-entity-urn", "")
        jid = urn.rsplit(":", 1)[-1] if urn else (re.findall(r"(\d{6,})", href) or [href])[-1]
        jobs.append({
            "id": f"li:{jid}",
            "title": title.get_text(" ", strip=True),
            "company": comp.get_text(" ", strip=True) if comp else "",
            "location": loc.get_text(" ", strip=True) if loc else "",
            "posted": tm.get_text(" ", strip=True) if tm else "",
            "url": href,
            "source": "LinkedIn",
        })
    return jobs


def linkedin_jobs():
    c = CFG["linkedin"]
    out, ids = [], set()
    for q in c["queries"]:
        for start in range(0, c["max_pages"] * 25, 25):
            r = get("https://www.linkedin.com/jobs-api/seeMoreJobPostings/search",
                    params={"keywords": q, "location": c["location"],
                            "f_TPR": f"r{c['window_seconds']}", "start": start})
            if not r or not r.text.strip():
                break
            page = parse_linkedin(r.text)
            if not page:
                break
            for j in page:
                if j["id"] not in ids:
                    ids.add(j["id"])
                    out.append(j)
            time.sleep(2)
    log(f"LinkedIn: {len(out)} postings in window")
    return out


# ---------------------------------------------------------------- YC: job boards
def ashby(slug):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not r:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        jobs.append({"id": f"ashby:{j.get('id')}", "title": j.get("title", ""),
                     "location": loc, "posted_at": j.get("publishedAt"),
                     "url": j.get("jobUrl") or j.get("applyUrl") or ""})
    return jobs


def greenhouse(slug):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not r:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if "jobs" not in data:
        return None
    return [{"id": f"gh:{j.get('id')}", "title": j.get("title", ""),
             "location": (j.get("location") or {}).get("name", ""),
             "posted_at": j.get("first_published") or j.get("updated_at"),
             "url": j.get("absolute_url", "")} for j in data["jobs"]]


def lever(slug):
    r = get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if not r:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for j in data:
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        out.append({"id": f"lever:{j.get('id')}", "title": j.get("text", ""),
                    "location": cat.get("location") or ", ".join(cat.get("allLocations") or []),
                    "posted_at": dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).isoformat() if ts else None,
                    "url": j.get("hostedUrl", "")})
    return out


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
        if m.group(1) != slug:
            continue
        jid = m.group(2)
        if jid not in jobs:
            title = titles.get(jid) or re.sub(
                r"\b(Ai|Ml|Llm|Swe|Ios|Api|Cv|Gpu)\b", lambda x: x.group(1).upper(),
                m.group(3).replace("-", " ").title())
            jobs[jid] = {"id": f"yc:{jid}", "title": title, "location": "", "posted_at": None,
                         "url": f"https://www.ycombinator.com/companies/{slug}/jobs/{jid}-{m.group(3)}"}
    return list(jobs.values())


FETCHERS = {"ashby": ashby, "greenhouse": greenhouse, "lever": lever, "yc": yc_page}


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
    """Find which job board a YC company uses. Returns (board, slug) or (None, None)."""
    for board in ("ashby", "greenhouse", "lever"):
        for s in slug_candidates(c):
            jobs = FETCHERS[board](s)
            if jobs:  # a board with zero jobs tells us nothing; keep looking
                return board, s
    return None, None


def gem_score(c):
    g = CFG["gems"]
    tags = set(c.get("tags") or []) | set(c.get("industries") or [])
    score = 0
    if tags & set(g["interest_tags"]):
        score += 2
    if (c.get("team_size") or 999) <= g["small_team"]:
        score += 1
    m = re.search(r"(\d{4})", c.get("batch") or "")
    if m and int(m.group(1)) >= g["recent_batch_year"]:
        score += 1
    if not c.get("top_company"):
        score += 1
    return score


def yc_jobs():
    yc = CFG["yc"]
    r = get(YC_DATA)
    if not r:
        log("YC: could not load company list")
        return [], []
    companies = [c for c in r.json()
                 if set(c.get("regions") or []) & set(yc["regions"])]
    log(f"YC: {len(companies)} hiring companies in your regions")

    boards = load_json("yc_boards.json", {})
    stale = (NOW - dt.timedelta(days=yc["rediscover_days"])).date().isoformat()
    todo = [c for c in companies
            if c["slug"] not in boards or boards[c["slug"]].get("checked", "") < stale]
    log(f"YC: discovering job boards for {len(todo)} companies")

    def disc(c):
        board, s = discover(c)
        return c["slug"], {"board": board, "slug": s, "checked": TODAY}

    with ThreadPoolExecutor(16) as ex:
        for slug, info in ex.map(disc, todo):
            boards[slug] = info
    save_json("yc_boards.json", boards)

    def fetch(c):
        info = boards.get(c["slug"], {})
        if info.get("board"):
            jobs = FETCHERS[info["board"]](info["slug"])
            src = info["board"].title()
        elif yc["check_yc_page_for_unknown"]:
            jobs = yc_page(c["slug"])
            src = "YC"
        else:
            return c, None, None
        return c, src, jobs

    out, board_keys = [], []
    with ThreadPoolExecutor(12) as ex:
        for c, src, jobs in ex.map(fetch, companies):
            if jobs is None:
                continue
            board_keys.append(f"yc:{c['slug']}")
            score = gem_score(c)
            for j in jobs:
                j.update({"company": c["name"], "source": src, "board": f"yc:{c['slug']}",
                          "yc_batch": c.get("batch", ""), "team_size": c.get("team_size"),
                          "one_liner": c.get("one_liner", ""), "gem_score": score,
                          "gem": score >= CFG["gems"]["min_score"],
                          "company_url": c.get("url", "")})
                out.append(j)
    found = sum(1 for v in boards.values() if v.get("board"))
    log(f"YC: {found} companies on Ashby/Greenhouse/Lever, {len(out)} open jobs total")
    return out, board_keys


# ---------------------------------------------------------------- output
def ago(iso):
    if not iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        mins = int((NOW - t).total_seconds() // 60)
    except ValueError:
        return ""
    if mins < 60:
        return f"{max(mins, 1)} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


def row_html(j):
    badges = []
    if j.get("gem"):
        badges.append("⭐ gem")
    if j.get("yc_batch"):
        badges.append(f"YC {j['yc_batch']}")
    if j.get("tag"):
        badges.append(j["tag"])
    if j.get("team_size"):
        badges.append(f"{j['team_size']} people")
    when = j.get("posted") or ago(j.get("posted_at")) or "new"
    sub = html.escape(j.get("one_liner") or "")
    return (
        "<tr>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #eee'><b>{html.escape(j['company'])}</b>"
        f"<br><span style='color:#777;font-size:12px'>{html.escape(' · '.join(badges))}</span>"
        + (f"<br><span style='color:#999;font-size:12px'>{sub}</span>" if sub else "")
        + "</td>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #eee'><a href='{html.escape(j['url'])}'>"
        f"{html.escape(j['title'])}</a></td>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #eee;font-size:13px'>{html.escape(j.get('location') or '—')}</td>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #eee;font-size:13px;color:#555'>{html.escape(when)}<br>"
        f"<span style='color:#999'>{html.escape(j['source'])}</span></td>"
        "</tr>"
    )


def build_email(sections):
    parts = ["<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:900px'>"]
    total = sum(len(v) for _, v in sections)
    parts.append(f"<p style='color:#555'>{total} new matching jobs · checked {NOW:%b %d, %H:%M} UTC</p>")
    for name, jobs in sections:
        if not jobs:
            continue
        parts.append(f"<h3 style='margin:18px 0 6px'>{html.escape(name)} ({len(jobs)})</h3>")
        parts.append("<table style='border-collapse:collapse;width:100%'>")
        parts += [row_html(j) for j in jobs]
        parts.append("</table>")
    parts.append("</div>")
    text = "\n".join(f"[{n}] {j['company']} | {j['title']} | {j.get('location','')} | {j['url']}"
                     for n, js in sections for j in js)
    return "".join(parts), text, total


def send_email(subject, html_body, text_body):
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("TO_EMAIL") or user
    if not (user and pw):
        log("Email not configured (GMAIL_USER / GMAIL_APP_PASSWORD); skipping")
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
    if not topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                      headers={"Title": title.encode("ascii", "ignore").decode()}, timeout=15)
    except requests.RequestException as e:
        log(f"ntfy failed: {e}")


def append_log(jobs):
    p = os.path.join(STATE, "new_jobs_log.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["found_utc", "source", "company", "title", "location", "tag", "url"])
        for j in jobs:
            w.writerow([NOW.strftime("%Y-%m-%d %H:%M"), j["source"], j["company"], j["title"],
                        j.get("location", ""), j.get("tag") or ("YC " + j["yc_batch"] if j.get("yc_batch") else ""),
                        j["url"]])


# ---------------------------------------------------------------- main
def main():
    seen = load_json("seen.json", {})
    seeded = set(load_json("seeded_boards.json", []))
    first_run = not seen

    with open(os.path.join(ROOT, "companies.csv")) as f:
        my_list = [(r["company"], "Your list") for r in csv.DictReader(f)]
    my_index = CompanyIndex(my_list)

    li = linkedin_jobs() if CFG["linkedin"]["enabled"] else []
    yc, yc_boards = yc_jobs() if CFG["yc"]["enabled"] else ([], [])
    yc_index = CompanyIndex([(j["company"], "YC") for j in yc])

    new = []
    for j in li:
        if j["id"] in seen:
            continue
        seen[j["id"]] = TODAY
        j["tag"] = my_index.match(j["company"]) or yc_index.match(j["company"])
        if not first_run:
            new.append(j)
    for j in yc:
        if j["id"] in seen:
            continue
        seen[j["id"]] = TODAY
        # A board we've never read before: everything on it is old news, not "just posted".
        if first_run or j["board"] not in seeded:
            continue
        new.append(j)
    seeded |= set(yc_boards)

    # Forget IDs not seen for 45 days so the state file stays small.
    cutoff = (NOW - dt.timedelta(days=45)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    save_json("seen.json", seen)
    save_json("seeded_boards.json", sorted(seeded))

    matches = [j for j in new if title_ok(j["title"]) and loc_ok(j.get("location", ""))]
    append_log(matches)

    if first_run:
        subj = "Job watcher is live"
        body = (f"<p>Baseline saved: {len(li)} LinkedIn postings and {len(yc)} open jobs across "
                f"{len(yc_boards)} YC companies. From the next run you'll only get NEW postings.</p>")
        send_email(subj, body, re.sub("<[^>]+>", "", body))
        push(subj, f"Watching {len(yc_boards)} YC boards + LinkedIn")
        return

    mine = [j for j in matches if j.get("tag") == "Your list"]
    yc_new = sorted([j for j in matches if j.get("yc_batch")], key=lambda j: -j.get("gem_score", 0))
    yc_on_li = [j for j in matches if j.get("tag") == "YC" and not j.get("yc_batch")]
    other = [j for j in matches if j["source"] == "LinkedIn" and not j.get("tag")]
    sections = [("Your company list (LinkedIn, past hour)", mine),
                ("YC startups — straight from their job boards", yc_new + yc_on_li),
                ("Other companies (LinkedIn, past hour)", other)]
    cap = CFG["notify"]["max_jobs_per_email"]
    trimmed, left = [], cap
    for name, js in sections:
        trimmed.append((name, js[:max(left, 0)]))
        left -= len(js)

    html_body, text_body, total = build_email(trimmed)
    log(f"New matching jobs: {len(matches)} (your list {len(mine)}, YC {len(yc_new) + len(yc_on_li)}, other {len(other)})")
    if total == 0 and not CFG["notify"]["send_when_empty"]:
        return
    gems = sum(1 for j in yc_new if j.get("gem"))
    subj = f"{total} new jobs" + (f" · {len(mine)} from your list" if mine else "") + (f" · {gems} ⭐ YC gems" if gems else "")
    send_email(subj, html_body, text_body)
    push(subj, "\n".join(f"{j['company']}: {j['title']}" for _, js in trimmed for j in js[:10])[:3000])


if __name__ == "__main__":
    main()
