# Roadmap

## ✅ Phase 1 – Fundament
Recherchen · Webseiten sichern · Video/Podcast laden + Whisper-Transkription · Datei-Upload · Notizen · drei Datumsarten · Markierungen mit Notiz/Tags/Datum · Zeitstrahl · Volltextsuche · Status & Glaubwürdigkeit · Markdown-Dossier · Bookmarklet

## ✅ Phase 1.1 – Transkription auf CPU
Vorhandene Untertitel nutzen (VTT/SRT/TTML) · Whisper-Modell je Quelle · Fortschritt + Restzeit · Abbrechen · eigene Warteschlange für Transkriptionen · CPU-Threads begrenzen

## ✅ Phase 2a – Beweissicherung
Fassungen mit Screenshot, PDF, gerenderter Seite · SHA-256-Manifest · unabhängiger RFC-3161-Zeitstempel · Nachweis-ZIP mit Prüfanleitung · Änderungsüberwachung mit Vorher-nachher-Vergleich · Hinweis bei Löschung · ntfy/Home-Assistant-Benachrichtigung · archive.org auf Knopfdruck

## ✅ Phase 2b – Social Media
Instagram/TikTok-Accounts beobachten: Stories, Highlights, Beiträge automatisch sichern, versiegeln, transkribieren · Cookies des Recherche-Kontos · Hinweis bei abgelaufener Anmeldung

## Phase 2c – Erfassung
- [ ] Sicherungsnachweis zusätzlich als PDF-Bericht (für Redaktion/Anwalt)
- [ ] Seiten hinter Paywall/Login: Übernahme aus Browser-Erweiterung (HTML direkt aus dem eingeloggten Tab senden)
- [ ] Podcast-RSS und YouTube-Kanäle abonnieren → neue Folgen automatisch holen
- [ ] OCR für Scans, Bilder und Story-Texte (Tesseract / ocrmypdf)
- [ ] Weitere Plattformen beobachten (X, Bluesky, Telegram-Kanäle, Facebook-Seiten)
- [ ] E-Mail-Eingang (Weiterleitung an Fundus-Adresse)

## Phase 3 – Einordnen
- [ ] Entitäten: Personen, Organisationen, Orte – manuell und per automatischer Erkennung (NER); Verknüpfung mit Markierungen
- [ ] Brücke zum Geflecht-Tool (Akteure + Beziehungen exportieren/importieren)
- [ ] Sprechertrennung im Transkript (pyannote) und Sprecher umbenennen
- [ ] Mehrere Recherchen pro Quelle, Quellen zwischen Projekten verschieben
- [ ] Offene Fragen / Aufgaben je Recherche (Thesen, To-dos, Ansprechpartner)

## Phase 4 – Darstellen & Auswerten
- [ ] Karte: Orte georeferenzieren, Quellen/Markierungen auf der Karte
- [ ] Netzwerkgraph der Entitäten
- [ ] Zeitstrahl mit Zeiträumen (Balken), Zoom, Filter nach Tags
- [ ] Export als Word/PDF mit Fußnoten
- [ ] Semantische Suche (Embeddings, lokal) und „Frag die Recherche“ mit Belegstellen

## Betrieb
- [ ] Mehrbenutzer mit Rollen, Freigabe einzelner Recherchen
- [ ] Verschlüsselung sensibler Recherchen (Quellenschutz)
- [ ] Getrennter Worker-Container für Transkription (z. B. auf einem stärkeren Rechner)
- [ ] Backup/Restore als ZIP aus der Oberfläche
