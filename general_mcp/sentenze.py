"""Server MCP per la ricerca di sentenze della Corte di Cassazione italiana.

Questo modulo implementa un server MCP (Model Context Protocol) che utilizza
Playwright per interagire con il portale ItalgiureWeb (SentenzeWeb) della
Corte Suprema di Cassazione. Permette di cercare sentenze tramite parole
chiave o numero/anno, restituendo i risultati in formato strutturato.

Il portale target è:
    https://www.italgiure.giustizia.it/sncass/
"""

import asyncio
import logging
import re
import time

from fastmcp import FastMCP
from playwright.async_api import Page, async_playwright
from pydantic import BaseModel

# URL base del portale SentenzeWeb della Corte di Cassazione
BASE_URL = "https://www.italgiure.giustizia.it/sncass/"

# Numero di risultati per pagina restituiti dal portale
RISULTATI_PER_PAGINA = 10

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modelli Pydantic per la struttura dei dati
# ---------------------------------------------------------------------------


class Sentenza(BaseModel):
    """Singola sentenza restituita dalla ricerca.

    Attributes:
        id: Identificativo interno del documento (es. "snpen2026408033S").
        sezione: Sezione della Corte (es. "QUARTA", "TERZA").
        tipo_archivio: Archivio di appartenenza ("CIVILE" o "PENALE").
        tipo_provvedimento: Tipo di provvedimento ("Sentenza", "Ordinanza", ecc.).
        numero: Numero della sentenza.
        data_deposito: Data di deposito in formato gg/mm/aaaa.
        ecli: Identificativo ECLI (European Case Law Identifier).
        anno: Anno della decisione.
        data_decisione: Data dell'udienza di decisione in formato gg/mm/aaaa.
        presidente: Nome del presidente del collegio giudicante.
        relatore: Nome del giudice relatore.
        estratto_testo: Estratto del testo con le parole cercate evidenziate.
    """

    id: str
    sezione: str
    tipo_archivio: str
    tipo_provvedimento: str
    numero: str
    data_deposito: str
    ecli: str
    anno: str
    data_decisione: str
    presidente: str
    relatore: str
    estratto_testo: str


class RisultatoRicerca(BaseModel):
    """Risultato paginato della ricerca sentenze.

    Attributes:
        parole_cercate: Le parole chiave utilizzate per la ricerca.
        totale_risultati: Numero totale di sentenze trovate.
        pagina_corrente: Numero della pagina corrente (1-indexed).
        totale_pagine: Numero totale di pagine disponibili.
        sentenze: Lista delle sentenze nella pagina corrente (max 10).
    """

    parole_cercate: str
    totale_risultati: int
    pagina_corrente: int
    totale_pagine: int
    sentenze: list[Sentenza]


# ---------------------------------------------------------------------------
# Funzioni helper per l'estrazione dati dalle pagine HTML
# ---------------------------------------------------------------------------


async def _extract_text(card, data_arg: str) -> str:
    """Estrae il contenuto testuale di un campo dalla card HTML di una sentenza.

    Il portale SentenzeWeb rappresenta ogni campo della sentenza come un elemento
    HTML con attributi ``data-role="content"`` e ``data-arg="<nome_campo>"``.

    Args:
        card: Elemento Playwright rappresentante una singola card risultato.
        data_arg: Nome del campo da estrarre (es. "szdec", "kind", "datdep").

    Returns:
        Il testo del campo, o stringa vuota se il campo non è presente.
    """
    el = await card.query_selector(f'[data-role="content"][data-arg="{data_arg}"]')
    if el is None:
        return ""
    return (await el.inner_text()).strip()


async def _extract_cards(page: Page) -> list[Sentenza]:
    """Estrae tutte le sentenze dalla pagina corrente dei risultati.

    Ogni risultato nel portale è rappresentato come una card (``.card``)
    contenente i metadati della sentenza e un estratto del testo OCR
    con le parole cercate evidenziate.

    Args:
        page: Pagina Playwright con i risultati di ricerca caricati.

    Returns:
        Lista di oggetti Sentenza estratti dalla pagina corrente.
    """
    cards = await page.query_selector_all(".card")
    logger.debug("Found %s result cards in current page", len(cards))
    sentenze: list[Sentenza] = []
    for card in cards:
        # L'estratto OCR contiene lo snippet di testo con le keyword evidenziate
        ocr_container = await card.query_selector('[data-role="datasubset"][data-arg="ocr"]')
        estratto = ""
        if ocr_container:
            estratto = (await ocr_container.inner_text()).strip()

        sentenze.append(
            Sentenza(
                id=await _extract_text(card, "id"),
                sezione=await _extract_text(card, "szdec"),
                tipo_archivio=await _extract_text(card, "kind"),
                tipo_provvedimento=await _extract_text(card, "tipoprov"),
                numero=await _extract_text(card, "numcard"),
                data_deposito=await _extract_text(card, "datdep"),
                ecli=await _extract_text(card, "ecli"),
                anno=await _extract_text(card, "anno"),
                data_decisione=await _extract_text(card, "datdec"),
                presidente=await _extract_text(card, "presidente"),
                relatore=await _extract_text(card, "relatore"),
                estratto_testo=estratto,
            )
        )
    return sentenze


async def _get_pagination_info(page: Page) -> tuple[int, int, int]:
    """Legge le informazioni di paginazione dalla pagina dei risultati.

    Il totale dei risultati è contenuto nell'elemento ``#totCount .tot``.
    La pagina corrente e il totale pagine sono nell'attributo ``title``
    dell'elemento ``#contentData`` (formato: "pagina X di Y").

    Args:
        page: Pagina Playwright con i risultati di ricerca caricati.

    Returns:
        Tupla ``(totale_risultati, pagina_corrente, totale_pagine)``.
    """
    # Totale risultati dal contatore in alto a destra dei filtri
    tot_el = await page.query_selector("#totCount .tot")
    totale = 0
    if tot_el:
        tot_text = (await tot_el.inner_text()).strip().replace(".", "").replace(",", "")
        totale = int(tot_text) if tot_text.isdigit() else 0

    # Pagina corrente e totale pagine dall'attributo title di #contentData
    # Formato: "pagina 1 di 16"
    content_data = await page.query_selector("#contentData")
    pagina_corrente = 1
    totale_pagine = 1
    if content_data:
        title = await content_data.get_attribute("title") or ""
        match = re.search(r"pagina\s+(\d+)\s+di\s+(\d+)", title)
        if match:
            pagina_corrente = int(match.group(1))
            totale_pagine = int(match.group(2))

    return totale, pagina_corrente, totale_pagine


async def _navigate_to_page(page: Page, target_page: int) -> bool:
    """Naviga alla pagina specificata dei risultati.

    Il pager del portale mostra un sottoinsieme di numeri di pagina.
    Se la pagina target è direttamente visibile, viene cliccato il link.
    Altrimenti, si naviga con le frecce "successiva"/"precedente" fino
    a raggiungere la pagina desiderata.

    Args:
        page: Pagina Playwright con i risultati di ricerca caricati.
        target_page: Numero della pagina da raggiungere (1-indexed).

    Returns:
        ``True`` se la navigazione è riuscita, ``False`` altrimenti.
    """
    # Tentativo diretto: clicca il link della pagina se visibile nel pager
    pager_link = await page.query_selector(f'.pager[data-arg="{target_page}"]')
    if pager_link:
        logger.debug("Navigating directly to page %s", target_page)
        await pager_link.click()
        await asyncio.sleep(3)
        return True

    # Se il link diretto non è visibile, naviga incrementalmente con le frecce
    _, current, total = await _get_pagination_info(page)
    if target_page > total or target_page < 1:
        logger.warning("Requested page %s is out of bounds (total=%s)", target_page, total)
        return False

    while current != target_page:
        if target_page > current:
            arrow = await page.query_selector('.pagerArrow[title="pagina successiva"]')
        else:
            arrow = await page.query_selector('.pagerArrow[title="pagina precedente"]')
        if not arrow:
            logger.warning("Pager arrow not found while navigating to page %s", target_page)
            return False
        await arrow.click()
        await asyncio.sleep(3)

        _, new_current, _ = await _get_pagination_info(page)
        if new_current == current:
            logger.warning("Pager did not advance from page %s", current)
            return False  # La pagina non è cambiata, impossibile proseguire
        current = new_current

        # Dopo ogni salto, ricontrolla se il link diretto è ora visibile
        pager_link = await page.query_selector(f'.pager[data-arg="{target_page}"]')
        if pager_link:
            logger.debug("Page %s became directly visible in pager", target_page)
            await pager_link.click()
            await asyncio.sleep(3)
            return True

    return True


async def _handle_cookie_banners(page: Page) -> bool:
        """Best-effort dismissal of common cookie consent overlays.

        Returns:
                True if at least one banner interaction/removal succeeded.
        """
        selectors = [
                "button:has-text('Accetta tutti')",
                "button:has-text('Accetta')",
                "button:has-text('Accetto')",
                "button:has-text('Consenti')",
                "button:has-text('Chiudi')",
                "button:has-text('OK')",
                "button:has-text('Accept all')",
                "button:has-text('Accept')",
                "#onetrust-accept-btn-handler",
                ".ot-sdk-container button",
                ".cc-btn",
                ".cookie-accept",
                "[aria-label*='cookie' i]",
        ]

        for selector in selectors:
                try:
                        locator = page.locator(selector).first
                        if await locator.count() == 0:
                                continue
                        if await locator.is_visible(timeout=700):
                                await locator.click(timeout=1500)
                                logger.debug("Cookie banner handled with selector: %s", selector)
                                return True
                except Exception:
                        continue

        try:
                removed = await page.evaluate(
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
                            ];

                            let count = 0;
                            for (const sel of selectors) {
                                for (const el of document.querySelectorAll(sel)) {
                                    if (!el || !(el instanceof HTMLElement)) continue;
                                    el.style.display = 'none';
                                    el.remove();
                                    count += 1;
                                }
                            }

                            if (document.body) {
                                document.body.style.overflow = 'auto';
                            }

                            return count;
                        }
                        """
                )
                return bool(removed)
        except Exception:
                return False


# ---------------------------------------------------------------------------
# Strumento MCP esposto ai client
# ---------------------------------------------------------------------------


async def cerca_sentenze(
    parole: str,
    pagina: int = 1,
) -> RisultatoRicerca:
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
    started_at = time.perf_counter()
    logger.info("Search started: parole=%s pagina=%s", parole, pagina)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            # Timeout elevati: il portale ItalgiureWeb può essere lento
            page.set_default_timeout(90000)
            page.set_default_navigation_timeout(90000)

            # Carica la pagina principale del portale
            await page.goto(BASE_URL, timeout=90000)
            await _handle_cookie_banners(page)
            # Attesa per il caricamento completo del framework ZK
            await asyncio.sleep(5)

            # Compila il campo di ricerca "Parole o Numero/Anno sentenza" (#searchterm)
            search_input = await page.query_selector("#searchterm")
            if not search_input:
                logger.error("Search input #searchterm not found")
                return RisultatoRicerca(
                    parole_cercate=parole,
                    totale_risultati=0,
                    pagina_corrente=0,
                    totale_pagine=0,
                    sentenze=[],
                )

            # Click + keyboard.type simula l'interazione utente reale,
            # necessaria perché il framework ZK non reagisce a .fill()
            await search_input.click()
            await page.keyboard.type(parole, delay=30)
            await asyncio.sleep(1)

            # Avvia la ricerca cliccando il pulsante "Cerca"
            cerca_btn = await page.query_selector('button[value="Cerca"]')
            if cerca_btn:
                await cerca_btn.click()
            else:
                logger.debug("Search button not found; submitting form via JS fallback")
                await page.evaluate("$('#z-form').submit();")

            # Attendi il caricamento AJAX dei risultati
            await asyncio.sleep(5)

            # Verifica se la ricerca non ha prodotto risultati
            no_data = await page.query_selector("#noData")
            if no_data:
                visible = await no_data.is_visible()
                if visible:
                    logger.info("Search completed with no results: parole=%s", parole)
                    return RisultatoRicerca(
                        parole_cercate=parole,
                        totale_risultati=0,
                        pagina_corrente=0,
                        totale_pagine=0,
                        sentenze=[],
                    )

            # Se richiesta una pagina diversa dalla prima, naviga al numero desiderato
            if pagina > 1:
                navigated = await _navigate_to_page(page, pagina)
                if not navigated:
                    totale, _, totale_pagine = await _get_pagination_info(page)
                    logger.warning(
                        "Could not navigate to requested page=%s for parole=%s",
                        pagina,
                        parole,
                    )
                    return RisultatoRicerca(
                        parole_cercate=parole,
                        totale_risultati=totale,
                        pagina_corrente=pagina,
                        totale_pagine=totale_pagine,
                        sentenze=[],
                    )

            # Estrai metadati di paginazione e le sentenze dalla pagina corrente
            totale, pagina_corrente, totale_pagine = await _get_pagination_info(page)
            sentenze = await _extract_cards(page)

            logger.info(
                "Search completed: parole=%s pagina=%s totale=%s page_results=%s elapsed=%.2fs",
                parole,
                pagina_corrente,
                totale,
                len(sentenze),
                time.perf_counter() - started_at,
            )

            return RisultatoRicerca(
                parole_cercate=parole,
                totale_risultati=totale,
                pagina_corrente=pagina_corrente,
                totale_pagine=totale_pagine,
                sentenze=sentenze,
            )
        finally:
            await browser.close()
            logger.debug("Playwright browser closed for search request")
