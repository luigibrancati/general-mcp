"""Google-web style search tool using a Playwright browser session."""

from __future__ import annotations

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field
from urllib.parse import urlencode, urlparse, parse_qs, unquote
from typing import Literal

class WebSearchInput(BaseModel):
    """Input model for WebSearchTool."""

    query: str = Field(..., min_length=1)
    max_results: int = Field(default=10, gt=0, le=100)
    country: str = Field(default="us", min_length=2, max_length=2)
    language: str = Field(default="en", min_length=2, max_length=5)
    safe_search: bool = True
    engine: Literal["google", "duckduckgo", "bing", "yahoo"] = "google"


class WebSearchResult(BaseModel):
    """A single normalized web result from Google-style search."""

    url: str
    title: str
    snippet: str


class WebSearchOutput(BaseModel):
    """Output model for WebSearchTool."""

    query: str
    engine: str
    results: list[WebSearchResult]
    urls: list[str]
    error: str | None = None


class WebSearchTool:
    """Tool that performs browser-driven web search across multiple engines.

    This implementation uses Playwright to open a real browser page and parse
    organic results from rendered HTML, instead of using a search API.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://serpapi.com",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Retained for backward-compatible construction from existing wiring.
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport

    @property
    def name(self) -> str:
        return "google_web_search_tool"

    async def execute(self, input: WebSearchInput) -> WebSearchOutput:
        """Execute a web search via Playwright and parse results."""
        search_url = self._build_search_url(
            query=input.query,
            max_results=input.max_results,
            country=input.country,
            language=input.language,
            safe_search=input.safe_search,
            engine=input.engine,
        )
        print(f"Executing {input.engine} web search for query: {search_url}")
        try:
            html = await self._fetch_html_with_playwright(search_url, input.engine)
            results = self._parse_results_by_engine(
                html=html,
                engine=input.engine,
                max_results=input.max_results,
            )

            # Fallback: some engines return anti-bot/consent HTML in headless mode.
            if not results:
                fallback_url = self._build_fallback_search_url(
                    query=input.query,
                    max_results=input.max_results,
                    country=input.country,
                    language=input.language,
                    safe_search=input.safe_search,
                    engine=input.engine,
                )
                if fallback_url and fallback_url != search_url:
                    print(f"No results from primary page, retrying with fallback URL: {fallback_url}")
                    fallback_html = await self._fetch_html_with_playwright(fallback_url, input.engine)
                    results = self._parse_results_by_engine(
                        html=fallback_html,
                        engine=input.engine,
                        max_results=input.max_results,
                    )

            # DuckDuckGo-specific hard fallback: query the lite HTML endpoint
            # without a browser when headless rendering is blocked.
            if not results and input.engine.lower() == "duckduckgo":
                fallback_url = self._build_fallback_search_url(
                    query=input.query,
                    max_results=input.max_results,
                    country=input.country,
                    language=input.language,
                    safe_search=input.safe_search,
                    engine=input.engine,
                )
                if fallback_url:
                    print(f"No results from Playwright pages, retrying via HTTP fallback: {fallback_url}")
                    fallback_html = await self._fetch_html_with_http(fallback_url)
                    results = self._parse_results_by_engine(
                        html=fallback_html,
                        engine=input.engine,
                        max_results=input.max_results,
                    )

            urls = [result.url for result in results]

            return WebSearchOutput(
                query=input.query,
                engine=input.engine,
                results=results,
                urls=urls,
            )
        except TimeoutError:
            return WebSearchOutput(
                query=input.query,
                engine=input.engine,
                results=[],
                urls=[],
                error=f"Search request timed out after {self._timeout}s",
            )
        except Exception as exc:
            error_msg = f"Browser search failed for {input.engine}: {type(exc).__name__}: {exc}"
            return WebSearchOutput(
                query=input.query,
                engine=input.engine,
                results=[],
                urls=[],
                error=error_msg,
            )

    def _build_search_url(
        self,
        query: str,
        max_results: int,
        country: str,
        language: str,
        safe_search: bool,
        engine: str,
    ) -> str:
        """Build a search URL for the selected engine."""
        engine_normalized = engine.lower()

        if engine_normalized == "duckduckgo":
            params = {
                "q": query,
                "kl": f"{country.lower()}-{language.lower()}",
                "kp": "1" if safe_search else "-2",
            }
            return f"https://duckduckgo.com/?{urlencode(params)}"

        if engine_normalized == "bing":
            params = {
                "q": query,
                "count": str(max_results),
                "setlang": language.lower(),
                "cc": country.upper(),
                "safeSearch": "Strict" if safe_search else "Off",
            }
            return f"https://www.bing.com/search?{urlencode(params)}"

        if engine_normalized == "yahoo":
            params = {
                "p": query,
                "n": str(max_results),
                "vl": language.lower(),
            }
            return f"https://search.yahoo.com/search?{urlencode(params)}"

        params = {
            "q": query,
            "num": str(max_results),
            "gl": country.lower(),
            "hl": language.lower(),
            "safe": "active" if safe_search else "off",
        }
        return f"https://www.google.com/search?{urlencode(params)}"

    def _build_fallback_search_url(
        self,
        query: str,
        max_results: int,
        country: str,
        language: str,
        safe_search: bool,
        engine: str,
    ) -> str | None:
        """Build fallback URLs that are generally more stable in headless contexts."""
        engine_normalized = engine.lower()

        if engine_normalized == "duckduckgo":
            params = {
                "q": query,
                "kl": f"{country.lower()}-{language.lower()}",
                "kp": "1" if safe_search else "-2",
            }
            return f"https://html.duckduckgo.com/html/?{urlencode(params)}"

        if engine_normalized == "google":
            params = {
                "q": query,
                "num": str(max_results),
                "gl": country.lower(),
                "hl": language.lower(),
                "safe": "active" if safe_search else "off",
                "gbv": "1",
            }
            return f"https://www.google.com/search?{urlencode(params)}"

        return None

    async def _fetch_html_with_playwright(self, url: str, engine: str) -> str:
        """Fetch rendered page HTML using a Playwright browser session."""
        print(f"Fetching HTML with Playwright for URL: {url}")
        timeout_ms = int(self._timeout * 1000)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1440, "height": 920},
            )
            page = await context.new_page()
            page.set_default_timeout(timeout_ms)
            await page.set_extra_http_headers(
                {
                    "Accept-Language": "en-US,en;q=0.9",
                    "Upgrade-Insecure-Requests": "1",
                }
            )
            await page.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
                Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                """
            )
            try:
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                await self._wait_for_results_or_settle(page, engine)
                return await page.content()
            finally:
                await context.close()
                await browser.close()

    async def _fetch_html_with_http(self, url: str) -> str:
        """Fetch HTML directly over HTTP as a fallback for bot-sensitive pages."""
        timeout = httpx.Timeout(self._timeout)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def _wait_for_results_or_settle(self, page, engine: str) -> None:
        """Wait for likely result containers and then settle briefly."""
        selector_map = {
            "google": "div.g, #search, div#rso",
            "duckduckgo": "article[data-testid='result'], li[data-layout='organic'], .result, #links",
            "bing": "li.b_algo, #b_results",
            "yahoo": "#web, div.algo, li[data-layout='organic']",
        }
        selector = selector_map.get(engine.lower(), "body")
        try:
            await page.wait_for_selector(selector, timeout=7000)
        except Exception:
            pass
        await page.wait_for_timeout(900)

    @staticmethod
    def _normalize_result_url(raw_url: str, engine: str) -> str:
        """Normalize engine-specific redirect URLs to outbound destination URLs."""
        url = (raw_url or "").strip()
        if not url:
            return ""

        parsed = urlparse(url)
        engine_normalized = engine.lower()

        if engine_normalized == "google":
            if parsed.path == "/url":
                q = parse_qs(parsed.query)
                target = q.get("q", [""])[0]
                if target:
                    return target
            if url.startswith("/"):
                return ""

        if engine_normalized == "duckduckgo":
            if parsed.path.startswith("/l/"):
                q = parse_qs(parsed.query)
                target = q.get("uddg", [""])[0]
                if target:
                    return unquote(target)
            if url.startswith("//"):
                return f"https:{url}"

        if engine_normalized == "yahoo":
            q = parse_qs(parsed.query)
            target = q.get("RU", [""])[0] or q.get("ru", [""])[0]
            if target:
                return unquote(target)

        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return ""
        return url

    @staticmethod
    def _parse_results_by_engine(
        html: str,
        engine: str,
        max_results: int,
    ) -> list[WebSearchResult]:
        engine_normalized = engine.lower()
        if engine_normalized == "duckduckgo":
            return WebSearchTool._parse_duckduckgo_results(html, max_results)
        if engine_normalized == "bing":
            return WebSearchTool._parse_bing_results(html, max_results)
        if engine_normalized == "yahoo":
            return WebSearchTool._parse_yahoo_results(html, max_results)
        return WebSearchTool._parse_google_results(html, max_results)

    @staticmethod
    def _parse_google_results(html: str, max_results: int) -> list[WebSearchResult]:
        """Extract organic search results from Google result page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        results: list[WebSearchResult] = []

        for block in soup.select("div.g"):
            title_node = block.select_one("h3")
            link_node = block.select_one("a[href]")
            if title_node is None or link_node is None:
                continue

            raw_url = (link_node.get("href") or "").strip()
            url = WebSearchTool._normalize_result_url(raw_url, "google")
            title = title_node.get_text(" ", strip=True)
            if not url or not title:
                continue
            if not url.startswith("http"):
                continue
            if "google.com/search" in url:
                continue

            snippet_node = block.select_one("div.VwiC3b, div.yXK7lf, div.MUxGbd")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""

            results.append(
                WebSearchResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                )
            )
            if len(results) >= max_results:
                break

        return results

    @staticmethod
    def _parse_duckduckgo_results(html: str, max_results: int) -> list[WebSearchResult]:
        """Extract organic search results from DuckDuckGo result page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        results: list[WebSearchResult] = []

        blocks = soup.select(
            "article[data-testid='result'], li[data-layout='organic'], div.result, div.results_links"
        )
        for block in blocks:
            link_node = block.select_one(
                "h2 a[href], a.result__a[href], a[data-testid='result-title-a'][href], a.result-link[href]"
            )
            if link_node is None:
                continue

            raw_url = (link_node.get("href") or "").strip()
            url = WebSearchTool._normalize_result_url(raw_url, "duckduckgo")
            title = link_node.get_text(" ", strip=True)
            if not url or not title or not url.startswith("http"):
                continue

            snippet_node = block.select_one(
                "div[data-result='snippet'], .result__snippet, div[data-testid='result-snippet'], a.result__snippet"
            )
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""

            results.append(
                WebSearchResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                )
            )
            if len(results) >= max_results:
                break

        return results

    @staticmethod
    def _parse_bing_results(html: str, max_results: int) -> list[WebSearchResult]:
        """Extract organic search results from Bing result page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        results: list[WebSearchResult] = []

        for block in soup.select("li.b_algo"):
            link_node = block.select_one("h2 a[href]")
            if link_node is None:
                continue

            raw_url = (link_node.get("href") or "").strip()
            url = WebSearchTool._normalize_result_url(raw_url, "bing")
            title = link_node.get_text(" ", strip=True)
            if not url or not title or not url.startswith("http"):
                continue

            snippet_node = block.select_one("div.b_caption p, p.b_paractl")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""

            results.append(
                WebSearchResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                )
            )
            if len(results) >= max_results:
                break

        return results

    @staticmethod
    def _parse_yahoo_results(html: str, max_results: int) -> list[WebSearchResult]:
        """Extract organic search results from Yahoo result page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        results: list[WebSearchResult] = []

        for block in soup.select("div#web li, div.algo"):
            link_node = block.select_one("h3 a[href], a[href]")
            if link_node is None:
                continue

            raw_url = (link_node.get("href") or "").strip()
            url = WebSearchTool._normalize_result_url(raw_url, "yahoo")
            title = link_node.get_text(" ", strip=True)
            if not url or not title or not url.startswith("http"):
                continue

            snippet_node = block.select_one("div.compText p, p")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""

            results.append(
                WebSearchResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                )
            )
            if len(results) >= max_results:
                break

        return results
