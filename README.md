# Fundus

Selbst gehostete Plattform zum **Sammeln, Sichern, Einordnen und Darstellen** von Rechercheinhalten – für Reportagen, Business Research und alles, wo viele Quellen zusammenkommen.

## Was Phase 1 kann

| Bereich | Funktion |
|---|---|
| **Recherchen** | Getrennte Projekte mit Beschreibung, Tags, Status-Filtern |
| **Webseiten** | Original-HTML, Screenshot der ganzen Seite, PDF und gerenderte Fassung (auch JavaScript-Seiten); lesbarer Haupttext + Metadaten (Titel, Autor:in, Medium, Datum) |
| **Beweissicherung** | Jede Sicherung ist eine *Fassung* mit SHA-256 aller Dateien, Manifest und unabhängigem Zeitstempel (RFC 3161). Nachweis-Paket als ZIP mit Prüfanleitung |
| **Änderungsüberwachung** | Seiten alle 6 h / täglich / wöchentlich prüfen; neue Fassung bei Änderung, Vorher-nachher-Vergleich, Hinweis bei Löschung, optional Push-Nachricht |
| **archive.org** | Auf Knopfdruck zusätzlich öffentlich bei der Wayback Machine sichern |
| **Videos & Podcasts** | Download per yt-dlp (YouTube, Mediatheken, ARD Audiothek, SoundCloud, direkte MP3/MP4 …). Vorhandene Untertitel werden direkt als Transkript übernommen, sonst lokale Transkription mit Whisper – jeweils mit Zeitstempeln |
| **Transkription auf CPU** | Modell pro Quelle wählbar, Fortschritt mit Restzeit, Abbrechen, eigene Warteschlange (Webseiten warten nie hinter einem langen Podcast) |
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

## Beweissicherung

Jede Sicherung einer Quelle wird als **Fassung** abgelegt (`files/<quelle>/v1`, `v2`, …):

| Datei | Inhalt |
|---|---|
| `page.html` | Antwort des Servers, unverändert |
| `screenshot.png` | Ganze Seite, wie sie im Browser aussah (Cookie-Hinweis wird automatisch bestätigt) |
| `seite.pdf` | Druckfassung |
| `gerendert.html` | Seite nach dem Ausführen von JavaScript |
| `manifest.json` | Liste aller Dateien mit SHA-256, URL, Abrufzeit, HTTP-Status |
| `manifest.tsr` | Zeitstempel eines unabhängigen Dienstes über die Prüfsumme des Manifests |

**Zeitstempel:** Fundus schickt nur die SHA-256-Prüfsumme des Manifests an einen Zeitstempeldienst nach RFC 3161 (Standard: DigiCert, Rückfall freetsa.org). Der Inhalt der Recherche verlässt den Server nicht. Die Signatur belegt gegenüber Dritten, dass genau diese Dateien spätestens zu diesem Zeitpunkt vorlagen – nicht nur laut eigener Serveruhr. Ist kein Internet erreichbar, bleibt die Sicherung gültig und der Zeitstempel kann später nachgefordert werden.

**Nachweis-Paket:** Pro Fassung als ZIP herunterladbar, mit allen Dateien, Manifest, Zeitstempel und `PRUEFEN.txt` – einer Anleitung, wie Dritte die Echtheit mit `sha256sum` und `openssl ts -verify` selbst prüfen.

**Überwachung:** In der Seitenleiste unter „Sicherung“ einschalten. Fundus vergleicht den Haupttext (Werbung, Navigation usw. werden ignoriert). Bei einer Änderung entsteht eine neue Fassung mit Screenshot und Zeitstempel, die Quelle wird markiert und der Unterschied lässt sich Absatz für Absatz ansehen. Liefert die Seite 404/410, wird das als eigene Fassung festgehalten. Dein Lesetext und deine Markierungen bleiben bei der ursprünglichen Fassung.

**Benachrichtigung:** `FUNDUS_NOTIFY_URL` auf ein [ntfy](https://ntfy.sh)-Topic (z. B. `https://ntfy.sh/mein-geheimes-topic`) oder einen Home-Assistant-Webhook (`http://homeassistant.local:8123/api/webhook/fundus`) setzen. Home Assistant erhält JSON mit `title`, `message`, `url`.

**archive.org:** Nur auf Knopfdruck. Achtung: Die Sicherung ist öffentlich einsehbar – bei vertraulichen Recherchen ggf. erst nach Veröffentlichung nutzen.

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `FUNDUS_DATA_DIR` | `./data` | Datenordner |
| `FUNDUS_WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3` – größer = genauer, langsamer |
| `FUNDUS_WHISPER_LANGUAGE` | *(leer)* | Leer = automatisch, sonst z. B. `de` |
| `FUNDUS_WHISPER_DEVICE` | `auto` | `cpu` oder `cuda` (mit NVIDIA-GPU deutlich schneller) |
| `FUNDUS_WHISPER_THREADS` | `0` | CPU-Threads für Whisper; `0` = halbe Kernzahl, damit der Server nebenher reaktionsfähig bleibt |
| `FUNDUS_SUBTITLES` | `auto` | `auto` = handgemachte Untertitel bevorzugt, automatische (Originalsprache) akzeptiert · `manual` = nur handgemachte · `off` = immer Whisper |
| `FUNDUS_SUBTITLE_LANGS` | `de,en` | Bevorzugte Untertitelsprachen |
| `FUNDUS_KEEP_VIDEO` | `0` | `1` = Video behalten, sonst nur Tonspur |
| `FUNDUS_CAPTURE` | `1` | Screenshot/PDF per Chromium; `0` = aus |
| `FUNDUS_CAPTURE_ACCEPT_COOKIES` | `1` | Cookie-Hinweise vor dem Screenshot automatisch bestätigen |
| `FUNDUS_TSA_URLS` | DigiCert, freetsa.org | Zeitstempeldienste (Komma-getrennt, der Reihe nach); leer = keine Zeitstempel |
| `FUNDUS_NOTIFY_URL` | *(leer)* | ntfy-Topic oder JSON-Webhook für Änderungsmeldungen |
| `FUNDUS_BASE_URL` | *(leer)* | Adresse von Fundus, z. B. `http://unraid:8000`, für Links in Meldungen |
| `FUNDUS_USER` / `FUNDUS_PASSWORD` | `fundus` / *(leer)* | Einfacher Zugangsschutz; leer = aus |

## Transkription ohne Grafikkarte

1. **Untertitel zuerst:** Bei YouTube, ARD/ZDF-Mediathek & Co. nimmt Fundus vorhandene Untertitel (WebVTT, SRT, TTML) – das dauert Sekunden. Automatisch *übersetzte* Untertitel werden nie verwendet. Die Herkunft steht bei jeder Quelle („Transkript: Untertitel (de)“ bzw. „Whisper small“).
2. **Whisper als Rückfall:** Gibt es keine Untertitel, transkribiert Whisper auf dem Prozessor. Grobe Richtwerte für 1 Stunde Audio: `tiny`/`base` 5–15 Min., `small` 20–60 Min., `medium` 1–3 Std.
3. **Modell je Quelle:** Beim Hinzufügen unter „Transkription“ oder später über „Neu transkribieren“ wählbar – etwa erst `base` für einen schnellen Überblick, dann `medium` für ein Interview, aus dem zitiert wird.
4. **Eigene Spur:** Transkriptionen laufen nacheinander in einer separaten Warteschlange. Webseiten, Uploads und Downloads werden parallel weiter verarbeitet.

Laufende Transkriptionen zeigen Fortschritt und Restzeit und lassen sich abbrechen. Nach einem Neustart des Containers werden unterbrochene Jobs automatisch neu gestartet.

## Aufbau

```
app/
  main.py          Web-Oberfläche + JSON-API (FastAPI)
  repo.py          Datenzugriff, Suche, Zeitstrahl, Jobs
  db.py            SQLite-Schema (inkl. FTS5-Volltextindex)
  worker.py        Hintergrund-Abarbeitung (Abruf, Download, Transkription)
  render.py        Text + Markierungen → HTML
  evidence.py      Fassungen, Manifest, Nachweis-Paket
  tsa.py           RFC-3161-Zeitstempel
  diff.py          Vergleich zweier Fassungen
  ingest/          web.py · media.py · transcribe.py · files.py · capture.py · watch.py
  templates/       Jinja2-Vorlagen
  static/          CSS + JS (kein Build-Schritt)
tests/
docs/ROADMAP.md    Nächste Ausbaustufen
```

Markierungen speichern Zeichenpositionen im extrahierten Text **und** das Zitat selbst. Ändert sich der Text (z. B. durch Neu-Transkription), werden sie automatisch neu verortet; nicht mehr auffindbare Stellen werden gesondert angezeigt statt stillschweigend zu verschwinden.
