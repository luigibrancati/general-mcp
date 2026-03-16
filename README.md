# Sentenze Cassazione — Server MCP

Server [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) per la ricerca di sentenze della **Corte Suprema di Cassazione** italiana tramite il portale [ItalgiureWeb / SentenzeWeb](https://www.italgiure.giustizia.it/sncass/).

Il server utilizza [Playwright](https://playwright.dev/python/) per navigare il portale in modalità headless e restituisce i risultati in formato strutturato, pronti per essere consumati da qualsiasi client MCP (es. Claude Desktop, VS Code Copilot, ecc.).

## Prerequisiti

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** — gestore pacchetti e ambienti virtuali
- **Chromium** — installato tramite Playwright (vedi sotto)

## Installazione

```bash
# Clona il repository
git clone <url-del-repo>
cd test_sentenze_mcp

# Installa le dipendenze
uv sync

# Installa il browser Chromium per Playwright
uv run playwright install chromium
```

## Avvio del server

```bash
uv run python main.py
```

Il server si avvia con trasporto **stdio** (standard input/output), il protocollo predefinito per MCP.

## Configurazione con client MCP

### Claude Desktop

Aggiungi al file di configurazione `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sentenze-cassazione": {
      "command": "uv",
      "args": ["run", "python", "main.py"],
      "cwd": "/percorso/assoluto/a/test_sentenze_mcp"
    }
  }
}
```

### VS Code (GitHub Copilot)

Aggiungi al file `.vscode/mcp.json` del workspace:

```json
{
  "servers": {
    "sentenze-cassazione": {
      "command": "uv",
      "args": ["run", "python", "main.py"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

## Strumenti disponibili

### `cerca_sentenze`

Cerca sentenze della Corte di Cassazione sul portale ItalgiureWeb.

| Parametro | Tipo   | Default | Descrizione                                          |
|-----------|--------|---------|------------------------------------------------------|
| `parole`  | `str`  | —       | Parole chiave o numero/anno sentenza da cercare      |
| `pagina`  | `int`  | `1`     | Numero di pagina dei risultati (1-indexed)           |

**Esempi di ricerca:**

- Per parole chiave: `"responsabilità medica"`
- Per numero/anno: `"12345/2024"`
- Combinazioni: `"danno biologico risarcimento"`

**Risposta** — oggetto `RisultatoRicerca`:

```json
{
  "parole_cercate": "responsabilità medica",
  "totale_risultati": 156,
  "pagina_corrente": 1,
  "totale_pagine": 16,
  "sentenze": [
    {
      "id": "snpen2026408033S",
      "sezione": "QUARTA",
      "tipo_archivio": "PENALE",
      "tipo_provvedimento": "Sentenza",
      "numero": "8033",
      "data_deposito": "02/03/2026",
      "ecli": "(ECLI:IT:CASS:2026:8033PEN)",
      "anno": "2026",
      "data_decisione": "20/01/2026",
      "presidente": "DI SALVO EMANUELE",
      "relatore": "FERRANTI DONATELLA",
      "estratto_testo": "... In tema di responsabilità medica, è dunque indispensabile accertare ..."
    }
  ]
}
```

Ogni pagina contiene fino a **10 risultati**. Per ottenere i risultati successivi, incrementa il parametro `pagina`.

## Struttura del progetto

```
test_sentenze_mcp/
├── main.py           # Server MCP con lo strumento cerca_sentenze
├── pyproject.toml    # Configurazione progetto e dipendenze
└── README.md         # Questa documentazione
```

## Dettagli tecnici

### Come funziona

1. Il server avvia un browser Chromium headless tramite Playwright.
2. Naviga al portale [SentenzeWeb](https://www.italgiure.giustizia.it/sncass/).
3. Compila il campo **"Parole o Numero/Anno sentenza"** con le keyword ricevute.
4. Clicca il pulsante **"Cerca"** e attende il caricamento dei risultati.
5. Estrae i dati strutturati dalle card HTML dei risultati.
6. Se richiesta una pagina diversa dalla prima, naviga tramite il pager.
7. Chiude il browser e restituisce i dati al client MCP.

### Campi estratti per ogni sentenza

| Campo               | Descrizione                                              |
|---------------------|----------------------------------------------------------|
| `id`                | Identificativo interno del documento                     |
| `sezione`           | Sezione della Corte (es. "QUARTA", "TERZA")              |
| `tipo_archivio`     | Archivio: "CIVILE" o "PENALE"                            |
| `tipo_provvedimento`| Tipo: "Sentenza", "Ordinanza", ecc.                      |
| `numero`            | Numero della sentenza                                    |
| `data_deposito`     | Data di deposito (gg/mm/aaaa)                            |
| `ecli`              | European Case Law Identifier                             |
| `anno`              | Anno della decisione                                     |
| `data_decisione`    | Data dell'udienza di decisione (gg/mm/aaaa)              |
| `presidente`        | Presidente del collegio giudicante                       |
| `relatore`          | Giudice relatore                                         |
| `estratto_testo`    | Snippet di testo con le parole cercate evidenziate       |

### Note sulle prestazioni

Il portale ItalgiureWeb può essere lento nel rispondere. I timeout sono configurati a **90 secondi** per gestire i tempi di caricamento variabili. Una singola ricerca richiede tipicamente 15–30 secondi.

## Licenza

Uso interno.