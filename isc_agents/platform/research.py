"""Resumable crawling and cited research. Never feeds credentials to the internet."""
import json
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urldefrag
from urllib.robotparser import RobotFileParser

from ..models import Rejected, digest
from .security import public_url


OFFICIAL_HOSTS = {"documentation.sailpoint.com", "developer.sailpoint.com"}
RESEARCH_HOSTS = OFFICIAL_HOSTS | {"community.sailpoint.com", "stackoverflow.com"}
USER_AGENT = "ISCIntegrationResearch/0.2"


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts, self.links, self.title_parts = [], [], []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer", "noscript"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer", "noscript"} and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())
            if self.in_title:
                self.title_parts.append(data.strip())

    @property
    def text(self):
        return "\n".join(self.parts)

    @property
    def title(self):
        return " ".join(self.title_parts) or "Untitled documentation"


class Research:
    def __init__(self, db, http, search_key=None, extra_hosts=()):
        self.db, self.http, self.search_key = db, http, search_key
        self.hosts = RESEARCH_HOSTS | set(extra_hosts)

    def ingest(self, url, content, title, trust, *, scope="global", etag=None, modified=None):
        if trust not in {"official", "community", "client", "fixture"}:
            raise Rejected("Invalid evidence classification")
        sha = digest(content)
        # Chunking keeps headings and URL attached to every evidence excerpt.
        chunks = [content[i:i + 6000] for i in range(0, len(content), 5500)]
        with self.db.tx():
            old = self.db.all("SELECT id FROM documents WHERE url=? AND scope=?", (url, scope))
            for row in old:
                self.db.execute("DELETE FROM documents_fts WHERE id=?", (row["id"],))
            self.db.execute("DELETE FROM documents WHERE url=? AND scope=?", (url, scope))
            for index, chunk in enumerate(chunks):
                doc_id = digest([url, scope, index])
                self.db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (doc_id, scope, url, title, chunk, sha, time.time(), etag, modified, trust))
                self.db.execute("INSERT INTO documents_fts VALUES(?,?,?)", (doc_id, title, chunk))
        return {"url": url, "chunks": len(chunks), "hash": sha}

    def search(self, query, environment_id, limit=6):
        if not isinstance(query, str) or len(query) > 16000:
            raise Rejected("Search query must be at most 16,000 characters")
        tokens = list(dict.fromkeys(re.findall(r"[a-zA-Z0-9_]{2,}", query)))[:20]
        if not tokens:
            return []
        expression = " OR ".join('"' + word + '"' for word in tokens)
        environment = self.db.one("SELECT config FROM environments WHERE id=?", (environment_id,))
        spec_id = json.loads(environment["config"]).get("spec_id") if environment else None
        spec = self.db.one("SELECT origin,hash FROM specs WHERE id=?", (spec_id,)) if spec_id else None
        spec_prefix = spec["origin"] + "?sha256=" + spec["hash"] + "#%" if spec else "no-selected-contract"
        rows = self.db.all("SELECT d.*,bm25(documents_fts) AS rank FROM documents_fts "
                           "JOIN documents d ON documents_fts.id=d.id WHERE documents_fts MATCH ? "
                           "AND d.scope IN ('global',?) AND (d.url NOT LIKE '%/deref-sailpoint-api.%' OR d.url LIKE ?) "
                           "ORDER BY rank LIMIT ?", (expression, environment_id, spec_prefix, limit))
        return [{k: row[k] for k in ("id", "url", "title", "content", "hash", "fetched", "trust")} for row in rows]

    def fetch(self, url):
        public_url(url, self.hosts)
        response = self.http.require_ok(self.http.request("GET", url, headers={"User-Agent": USER_AGENT}))
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and content_type:
            raise Rejected("Research page must be HTML")
        parser = Page()
        parser.feed(response.text)
        return {"url": url, "title": parser.title, "content": parser.text[:18000],
                "trust": "official" if urlsplit(url).hostname in OFFICIAL_HOSTS else "community",
                "fetched": time.time(), "hash": digest(parser.text)}

    def web_search(self, query):
        if not isinstance(query, str) or not 1 <= len(query) <= 2000:
            raise Rejected("Web search query must contain 1–2000 characters")
        if not self.search_key:
            raise Rejected("Internet search needs BRAVE_SEARCH_API_KEY; direct approved-page fetch is available")
        response = self.http.require_ok(self.http.request("GET", "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 5}, headers={"X-Subscription-Token": self.search_key, "Accept": "application/json"}))
        return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description"), "trust": "unverified"}
                for r in response.json().get("web", {}).get("results", [])[:5]]

    def crawl_page(self, state):
        """One page per checkpoint, enabling restarts and bounded recurring refreshes."""
        seeds = state.get("seeds", [])
        if not seeds or len(seeds) > 10:
            raise Rejected("Provide 1–10 official documentation seed URLs")
        for seed in seeds:
            public_url(seed, OFFICIAL_HOSTS)
        max_pages = min(max(int(state.get("max_pages", 50)), 1), 500)
        queue = state.get("queue", seeds.copy())
        visited = state.get("visited", [])
        if not queue or len(visited) >= max_pages:
            return {**state, "done": True, "visited": visited, "queue": queue}
        url = queue.pop(0)
        public_url(url, OFFICIAL_HOSTS)
        origin = "https://" + urlsplit(url).hostname
        robots = self.http.request("GET", origin + "/robots.txt", headers={"User-Agent": USER_AGENT}, max_bytes=256000)
        policy = RobotFileParser()
        if robots.status_code == 404:
            policy.parse([])
        else:
            self.http.require_ok(robots)
            policy.parse(robots.text.splitlines())
        if not policy.can_fetch(USER_AGENT, url):
            visited.append(url)
            return {**state, "queue": queue, "visited": visited, "done": False}
        previous = self.db.one("SELECT * FROM documents WHERE url=? LIMIT 1", (url,))
        headers = {"User-Agent": USER_AGENT}
        if previous and previous["etag"]:
            headers["If-None-Match"] = previous["etag"]
        if previous and previous["modified"]:
            headers["If-Modified-Since"] = previous["modified"]
        response = self.http.request("GET", url, headers=headers, max_bytes=2_000_000)
        if response.status_code == 304:
            # Follow the prior frontier; unchanged pages do not need re-indexing.
            self.db.execute("UPDATE documents SET fetched=? WHERE url=? AND scope='global'", (time.time(), url))
            old_links = self.db.one("SELECT links FROM crawled_pages WHERE url=?", (url,))
            links = json.loads(old_links["links"]) if old_links else []
        else:
            self.http.require_ok(response)
            parser = Page()
            if "xml" in response.headers.get("content-type", "") or urlsplit(url).path.endswith(".xml"):
                if "<!DOCTYPE" in response.text.upper() or "<!ENTITY" in response.text.upper():
                    raise Rejected("XML entities and DTDs are forbidden")
                try:
                    tree = ET.fromstring(response.text)
                except ET.ParseError:
                    raise Rejected("Malformed sitemap XML") from None
                links = [element.text for element in tree.iter() if element.tag.rsplit("}", 1)[-1] == "loc" and element.text][:3000]
            else:
                parser.feed(response.text)
                links = parser.links
                self.ingest(url, parser.text, parser.title, "official", etag=response.headers.get("etag"),
                            modified=response.headers.get("last-modified"))
            self.db.execute("INSERT OR REPLACE INTO crawled_pages VALUES(?,?)", (url, json.dumps(links)))
        for link in links:
            candidate = urldefrag(urljoin(url, link))[0]
            parsed = urlsplit(candidate)
            if (parsed.hostname == urlsplit(url).hostname and parsed.scheme == "https" and not parsed.query
                    and not re.search(r"\.(?:pdf|png|jpg|zip|css|js)$", parsed.path, re.I)
                    and any(candidate.startswith(("https://" + urlsplit(seed).hostname + "/") if urlsplit(seed).path.endswith(".xml") else seed.rsplit("/", 1)[0] + "/") for seed in seeds)
                    and candidate not in visited and candidate not in queue and candidate != url and len(queue) < 3000):
                queue.append(candidate)
        visited.append(url)
        # Conservative per-job crawl delay. The worker processes jobs serially.
        delay = min(max(policy.crawl_delay(USER_AGENT) or 0.3, 0.3), 10)
        time.sleep(delay)
        return {**state, "queue": queue, "visited": visited, "done": not queue or len(visited) >= max_pages}
