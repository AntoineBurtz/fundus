# Fundus auf Unraid installieren

Der Weg: GitHub baut bei jeder Änderung auf `main` automatisch ein Docker-Image
(`ghcr.io/antoineburtz/fundus:latest`). Unraid installiert es über eine Vorlage
und zeigt künftig „Update verfügbar“ an, wenn es eine neue Version gibt.

## 1. Image bauen lassen

1. Alle offenen Pull Requests der Reihe nach mergen.
2. Auf GitHub im Repo unter **Actions** → „Docker-Image“ warten, bis der Lauf grün ist
   (beim ersten Mal ca. 10–15 Minuten, danach schneller).

## 2. Unraid den Zugriff auf das Image erlauben

Das Repo ist privat, deshalb ist auch das Image zunächst privat. Zwei Möglichkeiten:

**A) Image öffentlich machen (einfach).**
GitHub → Profilbild → *Your profile* → Reiter **Packages** → `fundus` →
*Package settings* → *Change visibility* → **Public**.
Sichtbar wird dann nur das Programm, niemals deine Recherchedaten – die liegen
ausschließlich auf deinem Server.

**B) Image privat lassen.**
1. GitHub → *Settings* → *Developer settings* → *Personal access tokens* →
   *Tokens (classic)* → neues Token nur mit dem Recht **`read:packages`**.
2. In Unraid das Plugin **User Scripts** installieren (Apps-Tab), neues Skript
   „ghcr-login“ anlegen, Zeitplan **At Startup of Array**, Inhalt:
   ```bash
   #!/bin/bash
   echo "DEIN_TOKEN" | docker login ghcr.io -u AntoineBurtz --password-stdin
   ```
   Einmal mit *Run Script* ausführen. (Unraid vergisst Logins beim Neustart,
   deshalb das Skript.)

## 3. Vorlage auf den Server legen

Die Datei [`unraid/my-fundus.xml`](../unraid/my-fundus.xml) muss nach
`/boot/config/plugins/dockerMan/templates-user/my-fundus.xml`.

Am einfachsten über das Unraid-Terminal (Symbol `>_` oben rechts):

```bash
nano /boot/config/plugins/dockerMan/templates-user/my-fundus.xml
```

Auf GitHub die Datei öffnen → *Raw* → alles kopieren → ins Terminal einfügen →
`Strg+O`, `Enter`, `Strg+X`.

(Alternativ über die Netzwerkfreigabe `\\TOWER\flash\config\plugins\dockerMan\templates-user\`,
falls die Flash-Freigabe aktiviert ist.)

## 4. Container anlegen

1. **Docker**-Tab → ganz unten **Add Container**.
2. Bei **Template** unter *User templates* → **Fundus** wählen.
3. Prüfen bzw. anpassen:
   - **Daten**: `/mnt/user/appdata/fundus` (Standard ist gut)
   - **Passwort**: setzen, wenn Fundus über das Heimnetz hinaus erreichbar ist
   - **Benachrichtigung**: optional ntfy-Topic oder Home-Assistant-Webhook
4. **Apply**. Unraid lädt das Image (ca. 1,5 GB, wegen Chromium und Whisper).
5. Im Docker-Tab auf das Fundus-Symbol → **WebUI**.

## Updates

Docker-Tab → **Check for Updates** → bei Fundus **apply update**.
Daten bleiben erhalten; die Datenbank wird beim Start automatisch angepasst.

## Sichern

Es genügt, den Ordner `/mnt/user/appdata/fundus` zu sichern
(z. B. mit dem Plugin *Appdata Backup*). Darin liegt alles: Datenbank,
Fassungen mit Screenshots und Zeitstempeln, Mediendateien, Whisper-Modelle.

## Wenn etwas nicht läuft

- **Logs:** Docker-Tab → Fundus-Symbol → *Logs*.
- **„pull access denied“ / „unauthorized“:** Schritt 2 fehlt oder das Token ist abgelaufen.
- **Screenshots schlagen fehl:** In der Vorlage muss unter *Extra Parameters*
  `--shm-size=512m` stehen (ist voreingestellt).
- **Transkription sehr langsam:** Im Einordnungs-Feld der Quelle ein kleineres
  Modell wählen (`base`) oder `Whisper-CPU-Threads` erhöhen.
