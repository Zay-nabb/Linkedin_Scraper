import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

SESSION_FILE = Path(__file__).parent / "linkedin_session.json"
DEBUG_DIR = Path(__file__).parent
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
GRAPHQL_QUERY_IDS = [
    "voyagerSearchDashClusters.7cdf88d3366ad02cc5a3862fb9a24085",
    "voyagerSearchDashClusters.52fec77d08aa4598c8a056ca6bce6c11",
]
QUERY_FILE = Path(__file__).parent / "search_query.json"  # written by discover_search_query()
RESULT_TYPES = {"people": "PEOPLE", "companies": "COMPANIES", "posts": "CONTENT"}
UPDATE = "com.linkedin.voyager.feed.render.UpdateV2"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def save_debug(name, data):
    with open(DEBUG_DIR / f"debug_{name}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ---------------------------------------------------------------- helpers
def _g(o, path, default=None):
    """Safe nested get. 'a.b.0.c' (digits index into lists)."""
    for k in path.split("."):
        if isinstance(o, list):
            try:
                o = o[int(k)]
            except (ValueError, IndexError):
                return default
        elif isinstance(o, dict):
            o = o.get(k)
        else:
            return default
        if o is None:
            return default
    return o


def _img(v, depth=0):
    """Find the first VectorImage anywhere inside v and return the URL of its largest artifact."""
    if isinstance(v, list):
        v = v[0] if v else None
    if not isinstance(v, dict) or depth > 6:
        return None
    root = v.get("rootUrl")
    if root:
        arts = v.get("artifacts") or []
        if not arts:
            return root
        return root + max(arts, key=lambda a: a.get("width", 0)).get("fileIdentifyingUrlPathSegment", "")
    for x in v.values():
        if isinstance(x, (dict, list)):
            r = _img(x, depth + 1)
            if r:
                return r
    return None


def _date(r):
    """{'start': {month, year}, 'end': ...} -> 'Oct 2016 – Present'."""
    if not r:
        return ""

    def f(d):
        if not d:
            return ""
        m = MONTHS[d["month"] - 1] if d.get("month") else ""
        return f"{m} {d.get('year', '')}".strip()

    s, e = f(r.get("start")), f(r.get("end"))
    return f"{s} – {e or 'Present'}" if s else e


def _size(c):
    r = (c or {}).get("employeeCountRange")
    if not r:
        return None
    return f"{r.get('start')}-{r['end']} employees" if r.get("end") else f"{r.get('start')}+ employees"


class LinkedInVoyagerScraper:
    API_BASE = "https://www.linkedin.com/voyager/api"

    def __init__(self, proxy_dsn=None):
        self.session = requests.Session()
        self.proxy_dsn = proxy_dsn or os.getenv("PROXY_DSN")
        self.csrf_token = None
        self.logged_in = False
        if self.proxy_dsn:
            self.session.proxies = {"http": self.proxy_dsn, "https": self.proxy_dsn}
        self.session.headers.update({
            "user-agent": USER_AGENT,
            "accept-language": "en-US,en;q=0.9",
            "x-li-lang": "en_US",
            "x-restli-protocol-version": "2.0.0",
        })

    # ------------------------------------------------------------ session
    def _update_auth_headers(self):
        js = self.session.cookies.get("JSESSIONID")
        if js:
            self.csrf_token = js.strip('"')
            self.session.headers["csrf-token"] = self.csrf_token

    def load_session(self):
        if not SESSION_FILE.exists():
            return False
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
            for k, v in data.get("cookies", {}).items():
                self.session.cookies.set(k, v, domain=".linkedin.com")
            self.csrf_token = data.get("csrf_token")
            if self.csrf_token:
                self.session.headers["csrf-token"] = self.csrf_token
            if self._validate():
                self.logged_in = True
                return True
            return False
        except Exception:
            return False

    def save_session(self):
        with open(SESSION_FILE, "w") as f:
            json.dump({"cookies": dict(self.session.cookies), "csrf_token": self.csrf_token,
                       "timestamp": time.time()}, f, indent=2)

    def _validate(self):
        try:
            self._update_auth_headers()
            return self.session.get(f"{self.API_BASE}/me", timeout=10).status_code == 200
        except Exception:
            return False

    def login_interactive(self):
        print("[*] Opening browser for manual login...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
                page = ctx.new_page()
                page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
                start, ok = time.time(), False
                while time.time() - start < 300:
                    names = {c["name"] for c in ctx.cookies()}
                    if "li_at" in names and "JSESSIONID" in names:
                        ok = True
                        break
                    time.sleep(1)
                if not ok:
                    browser.close()
                    return False
                time.sleep(3)
                cd = {c["name"]: c["value"] for c in ctx.cookies() if c["domain"].endswith("linkedin.com")}
                browser.close()
            for k, v in cd.items():
                self.session.cookies.set(k, v, domain=".linkedin.com")
            self._update_auth_headers()
            self.save_session()
            self.logged_in = True
            print(f"[+] Session saved ({len(cd)} cookies)")
            return True
        except Exception as e:
            print(f"[!] Login error: {e}")
            return False

    def import_cookies(self, cd):
        for k, v in cd.items():
            self.session.cookies.set(k, v, domain=".linkedin.com")
        self._update_auth_headers()
        self.save_session()
        self.logged_in = True
        print("[+] Cookies imported")

    # ------------------------------------------------------------ HTTP
    def _fetch(self, path, params=None):
        try:
            r = self.session.get(f"{self.API_BASE}{path}", params=params, timeout=30)
            print(f"[DEBUG] {path} -> {r.status_code}")
            if r.status_code in (401, 403):
                self.logged_in = False
                return None
            return r
        except Exception as e:
            print(f"[!] {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _deep(obj, key, depth=0):
        if depth > 15:
            return None
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            obj = list(obj.values())
        if isinstance(obj, list):
            for i in obj:
                r = LinkedInVoyagerScraper._deep(i, key, depth + 1)
                if r is not None:
                    return r
        return None

    @staticmethod
    def _deep_all(obj, key, depth=0):
        out = []
        if depth > 15:
            return out
        if isinstance(obj, dict):
            if key in obj:
                out.append(obj[key])
            obj = list(obj.values())
        if isinstance(obj, list):
            for i in obj:
                out.extend(LinkedInVoyagerScraper._deep_all(i, key, depth + 1))
        return out

    # ------------------------------------------------------------ profile
    def get_profile(self, public_id):
        print(f"[*] Fetching profile: {public_id}")
        r = self._fetch("/identity/dash/profiles", params={
            "q": "memberIdentity", "memberIdentity": public_id,
            "decorationId": "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-96",
        })
        if not r or r.status_code != 200:
            print(f"[!] Profile failed: {r.status_code if r else 'no response'}")
            return None
        data = r.json()
        save_debug("profile", data)
        e = (data.get("elements") or [None])[0]
        if not e:
            print("[!] No profile found in response")
            return None

        def section(name):
            return _g(e, f"{name}.elements") or []

        positions = []
        for g in section("profilePositionGroups"):
            for p in _g(g, "profilePositionInPositionGroup.elements") or [g]:
                c = p.get("company") or g.get("company") or {}
                positions.append({
                    "title": p.get("title", ""),
                    "company": p.get("companyName") or g.get("companyName", ""),
                    "companyUrl": c.get("url"), "companyLogo": _img(c.get("logo")),
                    "companySize": _size(c),
                    "employmentType": _g(p, "employmentType.name"),
                    "location": p.get("locationName") or p.get("geoLocationName"),
                    "dates": _date(p.get("dateRange") or g.get("dateRange")),
                    "isCurrent": not _g(p, "dateRange.end"),
                    "description": p.get("description", ""),
                })
        education = [{
            "school": x.get("schoolName", ""), "schoolUrl": _g(x, "school.url"),
            "logo": _img(_g(x, "school.logo")), "degree": (x.get("degreeName") or "").strip(),
            "field": x.get("fieldOfStudy", ""), "dates": _date(x.get("dateRange")),
            "description": x.get("description", ""),
        } for x in section("profileEducations")]
        certs = [{"name": x.get("name", ""), "authority": x.get("authority", ""),
                  "dates": _date(x.get("dateRange"))} for x in section("profileCertifications")]
        volunteer = [{"role": x.get("role", ""), "org": x.get("companyName", ""),
                      "orgUrl": _g(x, "company.url"), "logo": _img(_g(x, "company.logo")),
                      "dates": _date(x.get("dateRange")), "description": x.get("description", "")}
                     for x in section("profileVolunteerExperiences")]
        cur = next((p for p in positions if p["isCurrent"]), None)
        name = f"{e.get('firstName', '')} {e.get('lastName', '')}".strip()

        result = {
            "username": public_id, "name": name, "urn": e.get("entityUrn"),
            "profileUrl": f"https://www.linkedin.com/in/{e.get('publicIdentifier', public_id)}",
            "headline": e.get("headline", ""), "summary": e.get("summary", ""),
            "location": _g(e, "geoLocation.geo.defaultLocalizedName", ""),
            "countryCode": _g(e, "location.countryCode"),
            "industry": _g(e, "industry.name", ""),
            "profilePicture": _img(_g(e, "profilePicture.displayImageReference")),
            "backgroundPicture": _img(_g(e, "backgroundPicture.displayImageReference")),
            "premium": bool(e.get("premium")), "creator": bool(e.get("creator")),
            "influencer": bool(e.get("influencer")), "causes": e.get("volunteerCauses") or [],
            "currentCompany": cur["company"] if cur else None,
            "currentTitle": cur["title"] if cur else None,
            "positions": positions, "education": education,
            "certifications": certs, "volunteer": volunteer,
            "skills": [s["name"] for s in section("profileSkills") if s.get("name")],
            "languages": [x["name"] for x in section("profileLanguages") if x.get("name")],
        }
        print(f"[+] Profile: {name}")
        return result

    # ------------------------------------------------------------ search (GraphQL, Rest.li variables)
    def _query_ids(self):
        ids = [os.getenv("SEARCH_QUERY_ID")]
        if QUERY_FILE.exists():
            ids.append(json.loads(QUERY_FILE.read_text()).get("queryId"))
        return [i for i in ids + GRAPHQL_QUERY_IDS if i]

    def discover_search_query(self):
        """Load a real search page with our cookies and read the current queryId off LinkedIn's own request."""
        print("[*] Discovering the current search queryId (a browser window will flash open)...")
        found = {}
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context(user_agent=USER_AGENT)
                ctx.add_cookies([{"name": k, "value": v, "domain": ".linkedin.com", "path": "/", "secure": True}
                                 for k, v in self.session.cookies.items()])
                page = ctx.new_page()

                def on_request(req):
                    m = re.search(r"queryId=(voyagerSearchDashClusters\.[0-9a-f]+)", req.url)
                    if m:
                        found["queryId"] = m.group(1)

                page.on("request", on_request)
                page.goto("https://www.linkedin.com/search/results/people/?keywords=python",
                          wait_until="domcontentloaded")
                for _ in range(30):
                    if found:
                        break
                    page.wait_for_timeout(1000)
                browser.close()
        except Exception as e:
            print(f"[!] Discovery failed: {e}")
        if found:
            QUERY_FILE.write_text(json.dumps(found))
            print(f"[+] Search queryId: {found['queryId']}")
            return found["queryId"]
        print("[!] Could not find a search request. Set SEARCH_QUERY_ID in .env (see DevTools > Network > graphql).")
        return None

    def _search_get(self, query_id, label, keywords, sort=None, date_posted=None, current_company=None, start=0, count=10):
        """Added pagination parameters (start and count) directly into the GraphQL variables payload."""
        qp = f"(key:resultType,value:List({RESULT_TYPES[label]}))"
        if sort == "recent":  
            qp += ",(key:sortBy,value:List(date_posted))"
        if date_posted:       
            qp += f",(key:datePosted,value:List({date_posted}))"
        if current_company:
            qp += f",(key:currentCompany,value:List({current_company}))"

        origin = "FACETED_SEARCH" if (sort or date_posted or current_company) else "GLOBAL_SEARCH_HEADER"
        kw_part = f"keywords:{quote(keywords)}," if keywords else ""
        
        variables = (f"(start:{start},count:{count},origin:{origin},query:({kw_part}"
                     f"flagshipSearchIntent:SEARCH_SRP,queryParameters:List({qp}),includeFiltersInResponse:false))")
        url = f"{self.API_BASE}/graphql?includeWebMetadata=true&variables={variables}&queryId={query_id}"
        try:
            r = self.session.get(url, timeout=30)
            print(f"[DEBUG] GET /graphql (...{query_id[-6:]}) [start:{start}, count:{count}] -> {r.status_code}")
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            print(f"[!] graphql: {e}")
            return None

    def _txt(self, el, key):
        v = self._deep(el, key)
        return (v.get("text", "") if isinstance(v, dict) else v) or ""

    def _search(self, label, keywords, limit, parse, **opts):
        print(f"[*] Searching {label}: '{keywords}' (target limit: {limit})")
        all_results = []
        seen_keys = set()
        
        # A conservative page size guarantees LinkedIn won't drop elements 
        page_size = 10 
        start = 0

        # Step 1: Find a working GraphQL query ID
        working_qid = None
        for attempt in (0, 1):
            for qid in self._query_ids():
                data = self._search_get(qid, label, keywords, start=0, count=page_size, **opts)
                if data:
                    working_qid = qid
                    break
            if working_qid or opts or (attempt == 0 and not self.discover_search_query()):
                break

        if not working_qid:
            print(f"[!] {label} search failed to find a valid queryId")
            return []

        # Step 2: Paginate securely using the working query ID
        while len(all_results) < limit:
            data = self._search_get(working_qid, label, keywords, start=start, count=page_size, **opts)
            if not data:
                break
                
            save_debug(f"search_{label}_page_{start}", data)

            if label == "posts":
                els = [w.get("update") for w in self._deep_all(data, "searchFeedUpdate") if isinstance(w, dict)]
            else:
                els = (self._deep_all(data, "EntityResultViewModel") + self._deep_all(data, "entityResult")
                       + [i for i in data.get("included", []) if str(i.get("$type", "")).endswith("EntityResultViewModel")])

            page_results = [x for x in map(parse, els) if x]
            
            # Deduplicate entries safely 
            new_items = []
            for item in page_results:
                key = item.get("publicIdentifier") or item.get("permalink") or str(item)
                if key not in seen_keys:
                    seen_keys.add(key)
                    new_items.append(item)

            if not new_items:
                print(f"[!] Hit end of results or received duplicates at start={start}. Stopping.")
                break

            all_results.extend(new_items)
            start += page_size # Safely advance pagination pointer

            if len(all_results) >= limit:
                break

            # Politeness delay to prevent rate-limiting when scraping larger rosters
            time.sleep(1.5)

        print(f"[+] Found {len(all_results)} unique {label}")
        return all_results[:limit]

    def resolve_company_id(self, identifier):
        """Resolves a company slug or URL to its numeric LinkedIn ID (e.g. 'microsoft' -> '1035')."""
        if not identifier:
            return None
        identifier = str(identifier).strip()
        if identifier.isdigit():
            return identifier
        m = re.search(r"linkedin\.com/company/([^/?#\s]+)", identifier)
        slug = m.group(1) if m else identifier.strip("/@")
        comp = self.get_company(slug)
        if comp and comp.get("id"):
            return str(comp["id"])
        return None

    def search_people(self, keywords="", limit=10, current_company=None):
        company_id = self.resolve_company_id(current_company) if current_company else None
        def parse(el):
            if not isinstance(el, dict):
                return None
            nav = self._deep(el, "navigationUrl") or ""
            pid = nav.split("/in/")[1].rstrip("/").split("?")[0] if "/in/" in nav else ""
            return pid and {
                "name": self._txt(el, "title"),
                "headline": self._txt(el, "primarySubtitle"),
                "publicIdentifier": pid,
                "location": self._txt(el, "secondarySubtitle"),
                "profileUrl": f"https://www.linkedin.com/in/{pid}",
            }
        return self._search("people", keywords, limit, parse, current_company=company_id)

    def search_companies(self, keywords, limit=10):
        def parse(el):
            title = self._txt(el, "title") if isinstance(el, dict) else ""
            if not title:
                return None
            nav = self._deep(el, "navigationUrl") or ""
            pid = nav.split("/company/")[1].rstrip("/").split("?")[0] if "/company/" in nav else ""
            return {"name": title, "publicIdentifier": pid, "industry": self._txt(el, "primarySubtitle")}
        return self._search("companies", keywords, limit, parse)

    def search_posts(self, keywords, count=10, sort="recent", date_posted=None):
        """sort: "recent" (newest first) or "relevance". date_posted: None | past-24h | past-week | past-month."""
        parse = lambda u: self._parse_update(u) if u else None
        if sort != "recent" and not date_posted:
            return self._search("posts", keywords, count, parse)
        posts = self._search("posts", keywords, count, parse, sort=sort, date_posted=date_posted)
        if not posts:
            print("[!] Filtered search returned nothing — retrying with default ranking")
            posts = self._search("posts", keywords, count, parse)
        if sort == "recent":  # safety net: also order what we got by the timestamp inside the post id
            posts.sort(key=lambda x: x.get("createdTime") or 0, reverse=True)
        return posts

    # ------------------------------------------------------------ company
    def get_company(self, name):
        print(f"[*] Fetching company: {name}")
        r = self._fetch("/organization/companies", params={
            "decorationId": "com.linkedin.voyager.deco.organization.web.WebFullCompanyMain-12",
            "q": "universalName", "universalName": name})
        if not r or r.status_code != 200:
            return None
        save_debug("company", r.json())
        elems = r.json().get("elements", [])
        if not elems:
            return None
        c = elems[0]
        inds = c.get("companyIndustries") or [{}]
        urn = c.get("entityUrn") or c.get("universalName") or ""
        m = re.search(r"(\d+)", urn)
        comp_id = m.group(1) if m else None
        return {
            "id": comp_id,
            "name": c.get("name", ""),
            "universalName": c.get("universalName", ""),
            "description": c.get("description", ""),
            "industry": inds[0].get("localizedName", ""),
            "staffCount": c.get("staffCount", 0),
            "website": c.get("websiteUrl", ""),
        }

    # ------------------------------------------------------------ feed
    def _feed(self, params, count, start):
        params = {**params, "moduleKey": "member-share", "count": min(count, 100), "start": start}
        r = self._fetch("/feed/updates", params=params)
        if r and r.status_code == 200 and r.json().get("elements"):
            return r.json()
        return None

    def get_profile_posts(self, public_id, count=10, start=0):
        print(f"[*] Fetching posts for: {public_id}")
        data = self._feed({"profileId": public_id, "q": "memberShareFeed"}, count, start)
        if not data:
            return []
        save_debug("posts", data)
        return self._parse_feed(data, count)

    def get_company_posts(self, company_name, count=10, start=0):
        print(f"[*] Fetching company posts: {company_name}")
        data = (self._feed({"companyUniversalName": company_name, "q": "companyFeedByUniversalName"}, count, start)
                or self._feed({"companyId": company_name, "q": "companyFeedByCompanyId"}, count, start))
        if not data:
            print("[!] No posts found — use the exact slug from linkedin.com/company/THIS-PART/")
            return []
        save_debug("company_posts", data)
        return self._parse_feed(data, count)

    def _parse_feed(self, data, limit):
        posts = []
        for item in data.get("elements", []):
            u = (item.get("value") or {}).get(UPDATE)
            if not u:
                continue
            p = self._parse_update(u)
            p["permalink"] = item.get("permalink") or p["permalink"]
            p["sponsored"] = bool(item.get("isSponsored"))
            posts.append(p)
        print(f"[+] Parsed {len(posts)} posts")
        return posts[:limit]

    def _parse_update(self, u):
        """Handles the feed shape (UpdateV2: updateMetadata, miniProfile) and the search shape
        (dash Update: metadata, attributesV2)."""
        a = u.get("actor") or {}
        at = (_g(a, "image.attributes") or [{}])[0]
        mp, mc = at.get("miniProfile"), at.get("miniCompany")
        is_co = bool(mc) or str(a.get("backendUrn", "")).startswith("urn:li:company") \
            or "/company/" in str(_g(a, "navigationContext.actionTarget"))
        author = {
            "name": _g(a, "name.text", ""), "headline": _g(a, "description.text", ""),
            "url": (_g(a, "navigationContext.actionTarget") or "").split("?")[0],
            "image": _img(mp.get("picture")) if mp else _img(mc.get("logo")) if mc else _img(at),
            "type": "company" if is_co else "member",
            "degree": (_g(a, "supplementaryActorInfo.text") or "").replace("•", "").strip(),
        }
        mentions = []
        for m in (_g(u, "commentary.text.attributes") or []) + (_g(u, "commentary.text.attributesV2") or []):
            d = m.get("detailData") or {}
            x, c = m.get("miniProfile") or d.get("profileFullName"), m.get("miniCompany") or d.get("companyName")
            if x:
                mentions.append({"name": f"{x.get('firstName', '')} {x.get('lastName', '')}".strip(),
                                 "url": f"https://www.linkedin.com/in/{x.get('publicIdentifier')}"})
            elif c:
                mentions.append({"name": c.get("name", ""),
                                 "url": c.get("url") or f"https://www.linkedin.com/company/{c.get('universalName')}"})

        sc = _g(u, "socialDetail.totalSocialActivityCounts") or {}

        def comment(c):
            ca = c.get("commenterForDashConversion") or {}
            return {"author": _g(ca, "title.text", ""), "headline": ca.get("subtitle", ""),
                    "url": ca.get("navigationUrl"), "text": _g(c, "commentV2.text", ""),
                    "created": c.get("createdTime"),
                    "likes": _g(c, "socialDetail.totalSocialActivityCounts.numLikes", 0),
                    "replies": [comment(r) for r in _g(c, "socialDetail.comments.elements") or []]}

        media = None
        for key, v in (u.get("content") or {}).items():
            kind = key.rsplit(".", 1)[-1].lower()
            if not isinstance(v, dict):
                continue
            if kind == "imagecomponent":
                media = {"type": "images", "items": [
                    {"url": _img(i.get("attributes")), "alt": i.get("accessibilityText", "")}
                    for i in v.get("images", []) if i.get("attributes")]}
            elif kind == "documentcomponent":
                d = v.get("document") or {}
                pg = max(_g(d, "coverPages.pagesPerResolution") or [{}], key=lambda x: x.get("width", 0))
                media = {"type": "document", "title": d.get("title"), "pages": d.get("totalPageCount"),
                         "cover": (pg.get("imageUrls") or [None])[0]}
            elif kind == "linkedinvideocomponent":
                m = v.get("videoPlayMetadata") or {}
                ps = max(m.get("progressiveStreams") or [{}], key=lambda x: x.get("width", 0))
                media = {"type": "video", "duration": m.get("duration"),  # milliseconds
                         "url": _g(ps, "streamingLocations.0.url"), "thumb": _img(m.get("firstFrameThumbnail") or m.get("thumbnail")),
                         "ai": _g(m, "c2paManifestData.isAiGenerated"),
                         "tool": _g(m, "c2paManifestData.appOrDeviceUsed")}
            elif kind == "articlecomponent":
                media = {"type": "article", "title": _g(v, "title.text"), "source": _g(v, "subtitle.text"),
                         "description": _g(v, "description.text"), "url": _g(v, "navigationContext.actionTarget"),
                         "image": _img(v.get("largeImage") or v.get("smallImage"))}
            elif kind == "pollcomponent":
                sums = _g(v, "pollSummary.pollOptionSummaries") or []
                media = {"type": "poll", "question": _g(v, "question.text"),
                         "voters": _g(v, "pollSummary.uniqueVotersCount"),
                         "status": _g(v, "pollSummary.remainingDuration.text"),
                         "options": [{"text": _g(o, "option.text", ""),
                                      "votes": sums[i].get("voteCount") if i < len(sums) else None}
                                     for i, o in enumerate(v.get("pollOptions") or [])]}

        urn = _g(u, "updateMetadata.urn") or _g(u, "metadata.backendUrn") or ""
        m = re.search(r"(\d{10,})$", urn)
        return {
            "id": urn, "permalink": _g(u, "socialContent.shareUrl", ""),
            "header": _g(u, "header.text.text"), "group": _g(u, "metadata.group.name"), "author": author,
            "text": _g(u, "commentary.text.text", ""), "language": _g(u, "commentary.originalLanguage"),
            "mentions": mentions,
            "createdTime": int(m.group(1)) >> 22 if m else None,
            "age": (_g(a, "subDescription.accessibilityText") or _g(a, "subDescription.text", "")).split("•")[0].strip(),
            "numLikes": sc.get("numLikes", 0), "numComments": sc.get("numComments", 0),
            "numShares": sc.get("numShares", 0),
            "reactions": {x["reactionType"]: x["count"] for x in sc.get("reactionTypeCounts") or []},
            "media": media,
            "comments": [comment(c) for c in _g(u, "socialDetail.comments.elements") or []],
            "resharedFrom": self._parse_update(u["resharedUpdate"]) if u.get("resharedUpdate") else None,
        }

def main():
    s = LinkedInVoyagerScraper()
    if not s.load_session() and not s.login_interactive():
        sys.exit(1)
    p = s.get_profile("williamhgates")
    if p:
        print(f"  {p['name']}: {p['headline']} ({len(p['positions'])} positions)")
    for x in s.get_profile_posts("williamhgates", 5):
        print(f"  [{x['numLikes']}] {x['text'][:60]}")


if __name__ == "__main__":
    main()