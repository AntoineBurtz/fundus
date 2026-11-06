# Fundus

Selbst gehostete Plattform zum **Sammeln, Sichern, Einordnen und Darstellen** von Rechercheinhalten – für Reportagen, Business Research und alles, wo viele Quellen zusammenkommen.

## Was Phase 1 kann

| Bereich | Funktion |
|---|---|
| **Recherchen** | Getrennte Projekte mit Beschreibung, Tags, Status-Filtern |
| **Webseiten** | Original-HTML wird gesichert, lesbarer Haupttext + Metadaten (Titel, Autor:in, Medium, Datum) extrahiert, SHA-256-Prüfsumme |
| **Videos & Podcasts** | Download per yt-dlp (YouTube, Mediatheken, ARD Audiothek, SoundCloud, direkte MP3/MP4 …), lokale Transkription mit Whisper inkl. Zeitstempeln |
| **Dateien** | PDF, Word, Text, HTML, Audio, Video, Bilder hochladen |
| **Notizen** | Eigene Notizen (Telefonate, Beobachtungen) als vollwertige Quelle |
| **Zeitliche Einordnung** | Drei getrennte Daten: *veröffentlicht*, *gesichert* (automatisch), *betrifft* (Ereignisdatum, auch als Zeitraum). Unvollständige Daten wie „2019“ oder „03.2019“ sind erlaubt |
| **Markieren** | Text auswählen → Farbe wählen. Notiz, Tags und eigenes Datum je Markierung. In Transkripten mit Timecode |
| **Transkript ↔ Player** | Zeitstempel anklicken springt im Player, Transkript läuft beim Abspielen mit |
| **Zeitstrahl** | Pro Recherche, wahlweise nach Ereignis-, Veröffentlichungs- oder Sicherungsdatum; datierte Markierungen erscheinen als eigene Einträge |
| **Suche** | Volltext über Texte, Transkript-Segmente, Markierungen und Notizen; Präfixsuche, „Phrasen“, `-ausschließen`, `OR` |
| **Bewertung** | Status (neu / zu prüfen / verifiziert / verworfen) und Glaubwürdigkeit 1–5 |
| **Export** | Dossier als Markdown mit allen Quellen, Metadaten, Prüfsummen und Zitaten; „Zitat kopieren“ mit Quellenangabe |
| **Schnell erfassen** | Bookmarklet für den Browser, `/add?url=…` für Teilen-Menüs |

## Starten

### Docker (z. B. Unraid)

```bash
docker compose up -d --build
# → http://<server>:8000
```

Alle Daten (Datenbank, gesicherte Seiten, Mediendateien, Whisper-Modelle) liegen in **einem Ordner** (`/data` im Container). Auf Unraid den Pfad z. B. auf `/mnt/user/appdata/fundus` legen – dann reicht ein Backup dieses Ordners.

Beim ersten Transkribieren lädt Whisper das Modell herunter (Modell `small` ≈ 480 MB).

### Lokal ohne Docker

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
# ffmpeg muss installiert sein
uvicorn app.main:app --reload
pytest
```

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `FUNDUS_DATA_DIR` | `./data` | Datenordner |
| `FUNDUS_WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3` – größer = genauer, langsamer |
| `FUNDUS_WHISPER_LANGUAGE` | *(leer)* | Leer = automatisch, sonst z. B. `de` |
| `FUNDUS_WHISPER_DEVICE` | `auto` | `cpu` oder `cuda` (mit NVIDIA-GPU deutlich schneller) |
| `FUNDUS_KEEP_VIDEO` | `0` | `1` = Video behalten, sonst nur Tonspur |
| `FUNDUS_USER` / `FUNDUS_PASSWORD` | `fundus` / *(leer)* | Einfacher Zugangsschutz; leer = aus |

**Richtwert Transkription auf CPU:** Modell `small` braucht für eine Stunde Audio grob 15–40 Minuten, je nach Prozessor. Mit `base` geht es etwa dreimal so schnell, mit GPU in wenigen Minuten.

## Aufbau

```
app/
  main.py          Web-Oberfläche + JSON-API (FastAPI)
  repo.py          Datenzugriff, Suche, Zeitstrahl, Jobs
  db.py            SQLite-Schema (inkl. FTS5-Volltextindex)
  worker.py        Hintergrund-Abarbeitung (Abruf, Download, Transkription)
  render.py        Text + Markierungen → HTML
  ingest/          web.py · media.py · transcribe.py · files.py
  templates/       Jinja2-Vorlagen
  static/          CSS + JS (kein Build-Schritt)
tests/
docs/ROADMAP.md    Nächste Ausbaustufen
```

Markierungen speichern Zeichenpositionen im extrahierten Text **und** das Zitat selbst. Ändert sich der Text (z. B. durch Neu-Transkription), werden sie automatisch neu verortet; nicht mehr auffindbare Stellen werden gesondert angezeigt statt stillschweigend zu verschwinden.
