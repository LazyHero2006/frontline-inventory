# Frontline Inventory — Server‑drevet webapp (FastAPI)

En enkel, men avansert, server‑drevet lagerløsning for Frontline. Ingen SPA/byggeverktøy nødvendig.
Funksjoner:
- Varer (navn, SKU, kategori, lokasjon, min.beholder, pris, valuta, bilde, notater)
- Søk/filtrering + paginering på server
- Mottak: skann med kamera (ZXing i nettleser) eller legg inn manuelt; transaksjonslogg
- Juster +/- antall per vare (transaksjonslogg føres)
- Import/Export (JSON/CSV)
- Bildeopplasting per vare (lagres i `app/static/uploads`)
- Low‑stock varsel på dashboard (qty <= minQty)
- SSE (Server‑Sent Events) for «live» oppdatering av bevegelseslogg
- Alt rendreres server‑side med Jinja2 + litt HTMX for små oppdateringer

## Kom i gang

1. Opprett et Python‑miljø og installer avhengigheter:
   ```bash
   cd frontline_inventory_web
   pip install -r requirements.txt
   ```

2. Kjør utviklingsserveren:
   ```bash
   uvicorn app.main:app --reload
   ```

3. Åpne i nettleser:
   - http://127.0.0.1:8000

## Konfig (miljøvariabler)

- `INV_DB` — sti til SQLite database (default: `inventory.db` i prosjektroten)
- `ADMIN_TOKEN` — valgfritt. Om satt, må endrende kall ha f.eks. `?token=...` eller skjulte felt i skjema.

## Backup / Flytting
- DB: `inventory.db` (SQLite)
- Opplastede bilder: `app/static/uploads`
- Eksport: bruk /export (JSON/CSV)

