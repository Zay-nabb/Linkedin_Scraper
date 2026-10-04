"""
LinkedIn Voyager API Scraper

Usage:
    python server.py

Then open http://localhost:8000 in your browser.
"""

import json
import os
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv()

# Resolve paths relative to this script file
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "templates", "index.html")
SESSION_PATH = os.path.join(SCRIPT_DIR, "linkedin_session.json")

sys.path.insert(0, SCRIPT_DIR)
from linkedin_scraper import LinkedInVoyagerScraper

# Global scraper + lock for login
scraper = None
login_lock = threading.Lock()
login_in_progress = False


def get_scraper() -> LinkedInVoyagerScraper:
    global scraper
    if scraper is None:
        scraper = LinkedInVoyagerScraper()
        scraper.load_session()
    return scraper


class ScraperHandler(SimpleHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # ---- Serve HTML ----
        if path == "/" or path == "/index.html":
            self.serve_file(TEMPLATE_PATH, "text/html")
            return

        # ---- API: Status ----
        if path == "/api/status":
            s = get_scraper()
            self.send_json({
                "logged_in": s.logged_in,
                "has_session": os.path.exists(SESSION_PATH),
                "login_in_progress": login_in_progress,
            })
            return

        # ---- API: Check login (returns 401 if not logged in) ----
        if path == "/api/check-login":
            s = get_scraper()
            if s.logged_in and s._validate():
                self.send_json({"logged_in": True})
            else:
                self.send_json({"logged_in": False}, 401)
            return

        # ---- API: Profile ----
        if path.startswith("/api/profile/"):
            username = path.split("/api/profile/")[1].split("?")[0]
            s = get_scraper()
            if not self._require_login(s):
                return
            profile = s.get_profile(username)
            if not profile:
                self.send_json({"error": "Profile not found"}, 404)
                return
            self.send_json(profile)
            return

        # ---- API: Search People ----
        if path == "/api/search/people":
            s = get_scraper()
            if not self._require_login(s):
                return
            q = params.get("q", [""])[0]
            limit = int(params.get("limit", ["10"])[0])
            if not q:
                self.send_json({"error": "Query 'q' required"}, 400)
                return
            results = s.search_people(q, limit=limit)
            self.send_json({"results": results, "count": len(results)})
            return

        # ---- API: Search Companies ----
        if path == "/api/search/companies":
            s = get_scraper()
            if not self._require_login(s):
                return
            q = params.get("q", [""])[0]
            limit = int(params.get("limit", ["10"])[0])
            if not q:
                self.send_json({"error": "Query 'q' required"}, 400)
                return
            results = s.search_companies(q, limit=limit)
            self.send_json({"results": results, "count": len(results)})
            return

        # ---- API: Search Posts ----
        if path == "/api/search/posts":
            s = get_scraper()
            if not self._require_login(s):
                return
            q = params.get("q", [""])[0]
            count = int(params.get("count", ["10"])[0])
            if not q:
                self.send_json({"error": "Query 'q' required"}, 400)
                return
            posts = s.search_posts(q, count=count)
            self.send_json({"posts": posts, "count": len(posts)})
            return

        # ---- API: Profile Posts ----
        if path.startswith("/api/posts/profile/"):
            username = path.split("/api/posts/profile/")[1].split("?")[0]
            count = int(params.get("count", ["10"])[0])
            s = get_scraper()
            if not self._require_login(s):
                return
            posts = s.get_profile_posts(username, count=count)
            self.send_json({"posts": posts, "count": len(posts)})
            return

        # ---- API: Company Posts ----
        if path.startswith("/api/posts/company/"):
            company_id = path.split("/api/posts/company/")[1].split("?")[0]
            count = int(params.get("count", ["10"])[0])
            s = get_scraper()
            if not self._require_login(s):
                return
            posts = s.get_company_posts(company_id, count=count)
            self.send_json({"posts": posts, "count": len(posts)})
            return

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

        # ---- API: Interactive Login ----
        if path == "/api/login-interactive":
            global login_in_progress

            if login_in_progress:
                self.send_json({"error": "Login already in progress"}, 409)
                return

            login_in_progress = True
            s = get_scraper()

            try:
                success = s.login_interactive()
                self.send_json({
                    "success": success,
                    "message": "Login successful! Session saved." if success else "Login failed.",
                })
            except Exception as e:
                self.send_json({"success": False, "message": str(e)}, 500)
            finally:
                login_in_progress = False
            return

        # ---- API: Import Cookies ----
        if path == "/api/import-cookies":
            cookies = {}
            if data.get("jsessionid"):
                cookies["JSESSIONID"] = data["jsessionid"]
            if data.get("li_at"):
                cookies["li_at"] = data["li_at"]
            if not cookies:
                self.send_json({"error": "Provide jsessionid and/or li_at"}, 400)
                return
            s = get_scraper()
            s.import_cookies(cookies)
            self.send_json({"success": s.logged_in, "message": "Cookies imported"})
            return

        self.send_json({"error": "Not found"}, 404)

    # ---- Helpers ----

    def _require_login(self, s: LinkedInVoyagerScraper) -> bool:
        """Check if logged in. If not, send login_required response and return False."""
        if s.logged_in and s._validate():
            return True

        # Try reloading saved session
        if s.load_session():
            return True

        self.send_json({
            "error": "login_required",
            "message": "Not logged in. Please log in first.",
        }, 401)
        return False

    def serve_file(self, filepath: str, content_type: str):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content.encode())))
            self.end_headers()
            self.wfile.write(content.encode())
        except FileNotFoundError:
            self.send_json({"error": f"File not found: {filepath}"}, 404)

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        msg = args[0] if args else ""
        if "/api/" in msg:
            print(f"  {msg}")
        else:
            super().log_message(format, *args)


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True
    allow_reuse_port = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

    ports_to_try = [port, 8080, 5000, 3000, 9000]
    server = None

    for p in ports_to_try:
        try:
            server = ReusableHTTPServer(("0.0.0.0", p), ScraperHandler)
            port = p
            break
        except OSError:
            print(f"[!] Port {p} is busy, trying next...")

    if server is None:
        print("[!] All ports busy. Try: python server.py 8888")
        sys.exit(1)

    print(f"\n{'='*50}")
    print(f"  LinkedIn Voyager Scraper")
    print(f"  Open http://localhost:{port}")
    print(f"{'='*50}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()