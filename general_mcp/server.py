import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright

from readability import Document as ReadabilityDocument  # readability-lxml
from lxml import html as lxml_html
import subprocess
import sys
from general_mcp.sentenze import cerca_sentenze  # import dello strumento Sentenze come esempio di tool aggiuntivo


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Installazione automatica dei browser Playwright
# ---------------------------------------------------------------------------


def _ensure_playwright_browsers() -> None:
    """Installa i browser Playwright (Chromium) se non già presenti.

    Necessario in ambienti di deploy (es. Horizon Prefect) dove il pacchetto
    Python è installato ma i binari del browser non sono stati scaricati.
    """
    try:
        logger.info("Installing Playwright Chromium with dependencies")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
            check=True,
            timeout=300,
        )
    except subprocess.CalledProcessError:
        # --with-deps richiede root; riprova senza (solo download binario)
        logger.warning("Playwright install with dependencies failed; retrying without dependencies")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            timeout=300,
        )


_ensure_playwright_browsers()

## SERVER MCP CON BROWSER HEADLESS E READABILITY

@dataclass
class ExtractResult:
    url: str
    final_url: Optional[str]
    status: Optional[int]
    title: Optional[str]
    site_name: Optional[str]
    byline: Optional[str]
    excerpt: Optional[str]
    lang: Optional[str]
    published_at: Optional[str]
    content_source: str
    confidence: float
    main_text: Optional[str]
    paragraphs: List[str]
    html_main: Optional[str]
    html_full: Optional[str]
    network_hints: List[Dict[str, Any]]
    diagnostics: Dict[str, Any]


class BrowserReader:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None

    async def handle_cookie_banners(self, page) -> bool:
            """Best-effort dismissal of common cookie consent banners.

            Returns:
                    True if at least one click/remove action was performed.
            """
            clicked = False

            # Prefer explicit consent/reject actions on common CMP buttons.
            selectors = [
                    "button:has-text('Accept all')",
                    "button:has-text('Accept All')",
                    "button:has-text('Accept')",
                    "button:has-text('I Agree')",
                    "button:has-text('Agree')",
                    "button:has-text('OK')",
                    "button:has-text('Got it')",
                    "button:has-text('Accetta tutti')",
                    "button:has-text('Accetta')",
                    "button:has-text('Accetto')",
                    "button:has-text('Consenti')",
                    "button:has-text('Chiudi')",
                    "button:has-text('Continua')",
                    "[id*='accept']",
                    "[class*='accept']",
                    "[aria-label*='accept' i]",
                    "[aria-label*='cookie' i]",
                    "#onetrust-accept-btn-handler",
                    ".ot-sdk-container button",
                    ".cc-btn",
                    ".cookie-accept",
                    ".cookie-allow",
            ]

            for selector in selectors:
                    try:
                            locator = page.locator(selector).first
                            if await locator.count() == 0:
                                    continue
                            if await locator.is_visible(timeout=700):
                                    await locator.click(timeout=1500)
                                    clicked = True
                                    logger.debug("Cookie banner action clicked with selector: %s", selector)
                                    break
                    except Exception:
                            continue

            # Fallback: hide common blocking overlays if a click was not possible.
            if not clicked:
                    try:
                            removed_count = await page.evaluate(
                                    """
                                    () => {
                                        const selectors = [
                                            '#onetrust-consent-sdk',
                                            '#CybotCookiebotDialog',
                                            '.cookie-banner',
                                            '.cookie-consent',
                                            '.cookies-banner',
                                            '.qc-cmp2-container',
                                            '[id*="cookie" i][role="dialog"]',
                                            '[class*="cookie" i][role="dialog"]',
                                            '[aria-label*="cookie" i]',
                                            'div[style*="z-index"][id*="cookie" i]',
                                        ];

                                        let removed = 0;
                                        for (const sel of selectors) {
                                            for (const el of document.querySelectorAll(sel)) {
                                                if (!el || !(el instanceof HTMLElement)) continue;
                                                el.style.display = 'none';
                                                el.remove();
                                                removed += 1;
                                            }
                                        }

                                        const body = document.body;
                                        if (body) {
                                            body.style.overflow = 'auto';
                                        }
                                        return removed;
                                    }
                                    """
                            )
                            if removed_count:
                                    clicked = True
                                    logger.debug("Cookie overlays removed via DOM fallback: %s", removed_count)
                    except Exception:
                            pass

            return clicked

    async def startup(self) -> None:
        startup_started_at = time.perf_counter()
        if self._playwright is None:
            logger.info("Starting Playwright runtime")
            self._playwright = await async_playwright().start()
        if self._browser is None:
            logger.info("Launching Chromium browser")
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
        logger.info("Browser runtime ready in %.2fs", time.perf_counter() - startup_started_at)

    async def shutdown(self) -> None:
        logger.info("Shutting down browser runtime")
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser runtime shutdown completed")

    async def browse_and_extract(self, url: str, mode: str = "article") -> ExtractResult:
        process_started_at = time.perf_counter()
        logger.info("Browse/extract request started: url=%s mode=%s", url, mode)
        await self.startup()
        page = await self._browser.new_page()
        network_hints: List[Dict[str, Any]] = []

        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "application/json" in ct or "text/json" in ct:
                    network_hints.append(
                        {
                            "url": response.url,
                            "content_type": ct,
                            "status": response.status,
                            "length": response.headers.get("content-length"),
                        }
                    )
            except Exception:
                pass

        page.on("response", on_response)

        diagnostics: Dict[str, Any] = {
            "used_cookie_click": False,
            "cookie_banner_handled": False,
            "reader_success": False,
            "extraction_notes": "",
        }

        status_code: Optional[int] = None
        final_url: Optional[str] = None

        try:
            logger.debug("Navigating page and waiting for body in DOM")
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector("body", state="attached", timeout=60000)
            if resp is not None:
                status_code = resp.status
                final_url = resp.url
                logger.info("Navigation completed: status=%s final_url=%s", status_code, final_url)

            # Best-effort cookie banner handling to avoid blocked content/actions.
            diagnostics["cookie_banner_handled"] = await self.handle_cookie_banners(page)
            diagnostics["used_cookie_click"] = diagnostics["cookie_banner_handled"]

            # scroll per attivare eventuali lazy load
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

            html = await page.content()
        finally:
            await page.close()
            logger.debug("Page closed after extraction attempt")

        # Fallback se non c'è HTML
        if not html:
            logger.warning("Extraction produced empty HTML for url=%s", url)
            return ExtractResult(
                url=url,
                final_url=final_url,
                status=status_code,
                title=None,
                site_name=None,
                byline=None,
                excerpt=None,
                lang=None,
                published_at=None,
                content_source="none",
                confidence=0.0,
                main_text=None,
                paragraphs=[],
                html_main=None,
                html_full=None,
                network_hints=network_hints,
                diagnostics={**diagnostics, "extraction_notes": "empty_html"},
            )

        # Estrazione con Readability
        content_source = "dom"
        confidence = 0.0
        title = None
        excerpt = None
        byline = None
        main_html = None
        main_text = None
        paragraphs: List[str] = []
        site_name = None
        lang = None
        published_at = None

        try:
            doc = ReadabilityDocument(html)
            # readability-lxml consente di passare diverse opzioni; qui andiamo di default.[web:84]
            main_html = doc.summary()      # HTML della porzione principale
            title = doc.short_title()
            excerpt = doc.summary_within_limits(300)  # breve estratto, se disponibile
            # converto main_html in testo/paragraphs
            if main_html:
                tree = lxml_html.fromstring(main_html)
            else:
                tree = lxml_html.fromstring(html)

            # meta generici
            head = lxml_html.fromstring(html)
            # site_name da og:site_name se presente
            site_name_meta = head.xpath("//meta[@property='og:site_name']/@content")
            if site_name_meta:
                site_name = site_name_meta[0]

            lang_attr = head.xpath("string(//html/@lang)")
            lang = lang_attr or None

            # metadata articolo da JSON-LD (molto semplificato)
            jsonld_nodes = head.xpath("//script[@type='application/ld+json']/text()")
            for node in jsonld_nodes:
                try:
                    data = json.loads(node)
                    # json-ld può essere lista o oggetto
                    candidates = data if isinstance(data, list) else [data]
                    for c in candidates:
                        t = c.get("@type")
                        if t in ("NewsArticle", "Article", "BlogPosting"):
                            published_at = c.get("datePublished") or c.get("dateCreated")
                            byline = None
                            author = c.get("author")
                            if isinstance(author, dict):
                                byline = author.get("name")
                            elif isinstance(author, list) and author:
                                # prende il primo autore con name
                                for a in author:
                                    if isinstance(a, dict) and a.get("name"):
                                        byline = a["name"]
                                        break
                            if not title and c.get("headline"):
                                title = c["headline"]
                            break
                except Exception:
                    continue

            # testo e paragrafi
            ps = tree.xpath("//p")
            for p in ps:
                txt = (p.text_content() or "").strip()
                if txt:
                    paragraphs.append(txt)
            if paragraphs:
                main_text = "\n\n".join(paragraphs)
                confidence = min(1.0, 0.4 + 0.01 * len(paragraphs))  # euristica semplicissima
                logger.info("Extracted %s paragraphs with confidence %.2f", len(paragraphs), confidence)

            diagnostics["reader_success"] = main_text is not None
        except Exception as e:
            diagnostics["extraction_notes"] = f"readability_error: {type(e).__name__}"
            logger.exception("Readability extraction failed for url=%s", url)

        # se main_text è vuoto, possiamo abbassare content_source
        if not main_text:
            content_source = "heuristic"
            confidence = 0.1
            # potresti qui aggiungere una seconda passata euristica custom

        elapsed = time.perf_counter() - process_started_at
        logger.info(
            "Browse/extract completed: url=%s status=%s paragraphs=%s confidence=%.2f elapsed=%.2fs",
            url,
            status_code,
            len(paragraphs),
            confidence,
            elapsed,
        )

        return ExtractResult(
            url=url,
            final_url=final_url or url,
            status=status_code,
            title=title,
            site_name=site_name,
            byline=byline,
            excerpt=excerpt,
            lang=lang,
            published_at=published_at,
            content_source=content_source,
            confidence=confidence,
            main_text=main_text,
            paragraphs=paragraphs,
            html_main=main_html,
            html_full=html if confidence < 0.9 else None,  # opzionale: evita di restituire html completo sempre
            network_hints=network_hints,
            diagnostics=diagnostics,
        )


# MCP server setup con FastMCP[web:54][web:60][web:85]

MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("PORT", os.getenv("MCP_PORT", "8051")))

mcp = FastMCP(name="BrowserReader", host=MCP_HOST, port=MCP_PORT)
browser_reader = BrowserReader()


@mcp.tool()
async def browse_extract(url: str, mode: str = "article") -> Dict[str, Any]:
    """
    Carica una pagina in un browser headless, esegue il rendering
    e prova a estrarre il contenuto principale (titolo, testo, meta).
    `mode` per ora è solo informativo.
    """
    logger.info("Tool call received: browse_extract url=%s mode=%s", url, mode)
    result = await browser_reader.browse_and_extract(url, mode=mode)
    logger.info("Tool call finished: browse_extract url=%s", url)
    return asdict(result)

@mcp.tool()
async def cerca_sentenze_wrapper(parole: str, pagina: int = 1) -> Any:
    """Cerca sentenze della Corte di Cassazione sul portale ItalgiureWeb.

    Apre il portale SentenzeWeb, inserisce le parole chiave nel campo
    "Parole o Numero/Anno sentenza" e restituisce i risultati paginati.
    Ogni pagina contiene fino a 10 risultati.

    Esempi di ricerca:
        - Per parole chiave: ``"responsabilità medica"``
        - Per numero/anno: ``"12345/2024"``
        - Combinazioni: ``"danno biologico risarcimento"``

    Args:
        parole: Parole chiave o numero/anno sentenza da cercare.
        pagina: Numero di pagina dei risultati (default: 1, 1-indexed).

    Returns:
        RisultatoRicerca con i metadati di paginazione e la lista delle sentenze.
    """
    logger.info("Tool call received: cerca_sentenze parole=%s pagina=%s", parole, pagina)
    started_at = time.perf_counter()
    result = await cerca_sentenze(parole, pagina)
    logger.info(
        "Tool call finished: cerca_sentenze parole=%s pagina=%s totale=%s elapsed=%.2fs",
        parole,
        pagina,
        result.totale_risultati,
        time.perf_counter() - started_at,
    )
    return result

if __name__ == "__main__":
    # Heroku richiede un processo web in ascolto su PORT,
    # mentre in locale puo rimanere il trasporto stdio.
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    logger.info("Starting MCP server host=%s port=%s transport=%s", MCP_HOST, MCP_PORT, transport)
    mcp.run(transport=transport)