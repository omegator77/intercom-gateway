# intercom-gateway

Brücke zwischen Soundkarten-Kanälen und Leitungen (Lines) eines
[Eyevinn Open Intercom](https://github.com/Eyevinn/intercom-manager).

Das Gateway verbindet sich pro Line wie ein Browser-Teilnehmer mit dem Intercom, holt das Audio
einer Soundkarte (zum Beispiel Dante Virtual Soundcard oder ein MADI-Interface) und spielt den
Mix der Line wieder auf der Soundkarte aus. Das Signal wird dabei nicht verarbeitet: es gibt
keine Rauschunterdrückung, keine Echo-Cancellation und keine automatische Pegelregelung wie im Browser.

Eine Line wird als eigener Teilnehmer geführt. Pro Line wird deshalb ein **eigener Account** mit
der Rolle `participant` in der Production gebraucht. Dieser Account darf nicht zusätzlich in einem
Browser angemeldet sein.

## Status

Frühe Version (0.3.0). Lauffähig sind:

- das Gateway ohne Oberfläche (`intercom-gateway`)
- eine Desktop-Oberfläche (`intercom-gateway-ui`, PySide6) mit Start, Stopp, Pegelanzeige und der Anzeige, wer sonst noch in der
  Leitung ist (aktiv/inaktiv, WHIP; wird alle 5 Sekunden vom Server abgefragt und beeinflusst den Ton nie)
- ein Windows-Installer, der mit `build.ps1` gebaut wird (siehe unten, noch nicht als fertiger Download veröffentlicht)

Getestet wurde unter Windows mit Dante Virtual Soundcard. Das Gateway nutzt ausschließlich
**WASAPI**-Geräte. Ein Interface, das nur ASIO anbietet, taucht nicht auf.

## Intercom-Client (für Menschen, Entwicklungsstand)

Neben dem Gateway gibt es einen Client für eine Person mit Headset: Er tritt mehreren Leitungen bei und hat
**Push-to-Talk** (Halten), **Dauer-Sprechen** (Latch), **Lautstärke und Stumm pro Leitung** und **globale Tasten**,
die auch gelten, wenn ein anderes Programm im Vordergrund ist. Ein Mikrofon wird auf alle Leitungen verteilt (nur in
die Leitung, in die gerade gesprochen wird, geht Ton), alle Leitungen werden zu einem Ausgang gemischt.

```powershell
pip install -e ".[client]"
intercom-client            # Oberfläche; beim ersten Start öffnen sich die Einstellungen
```

- „Trennen“ oben beendet alles; auf jeder Leitungskarte gibt es zusätzlich „Trennen“/„Verbinden“ nur für diese Leitung.
  **Was du auf den Karten änderst, wird gemerkt:** eine getrennte Leitung bleibt beim nächsten Start getrennt
  (`autoconnect = false` in der Konfiguration, auch einstellbar in der Spalte „Beim Start verbinden"), und die
  Lautstärke jeder Leitung bleibt erhalten. „Trennen“ oben ändert daran nichts.
- **Karten:** Jede Leitung ist eine Zeile mit auffälligen Knöpfen (blaues „Sprechen", „Dauer", „Stumm"), Lautstärke und
  Pegel. Während du sendest, wird die Karte rot (halten) bzw. gold (Dauer) umrahmt. Der Pfeil links klappt die Details
  (wer ist da, Sendepegel, Meldungen) ein oder aus, „Alle einklappen" oben für alle. Wie eine Karte eingeklappt ist,
  wird gemerkt (`collapsed = true`).
- Einstellungen: Server und Konto, Leitungen hinzufügen (nach dem Login aus der Liste), pro Leitung Sprechen- und
  Dauer-Taste, Mikrofon und Kopfhörer, Taste für „Alles stumm". Beispiel: `client.example.toml`.
- **Stream Deck über Bitfocus Companion** (Einstellungen → Stream Deck): Der Client spricht dasselbe Protokoll wie die
  Web-App mit dem Companion-Modul „Eyevinn: Open Intercom" (WebSocket, Standard-Port 12345). Dieselben Tasten und
  Rückmeldungen funktionieren: Sprechen (halten), Mikrofon stumm/an, Ausgang stumm, Lautstärke ±5 %, „Alles stumm"
  und die Anzeige „spricht gerade". Die Nummer der Taste ist die Position der Leitung in der Liste (1 = erste Karte).
  Das Modul nimmt nur **einen** Client an: entweder die Web-App oder dieser Client. Wird die Verbindung getrennt,
  während eine Sprechtaste gehalten wird, lässt der Client sie los.
- Die anderen sehen dich unter dem Namen deines Kontos (Alias in den User Settings der Web-App).
- **Im Hauptfenster** lassen sich Mikrofon-Pegel, Echo-Unterdrückung, Rauschunterdrückung und Auto-Pegel **während
  der Verbindung** ändern, ohne dass eine Leitung neu verbindet. Die Werte werden für den nächsten Start gespeichert.
  Der **Auto-Pegel** ist ein eigener Sprach-Pegelausgleich (nicht der WebRTC-AGC, der nur einen festen Zuschlag gibt):
  Er misst die Sprachlautstärke und regelt sie auf einen Ziel-Pegel (Standard −20 dBFS), leise Stimmen werden
  angehoben (höchstens 25 dB), laute abgesenkt. In Sprechpausen bleibt die Verstärkung stehen, damit Rauschen nicht
  hochgezogen wird; Spitzen werden sofort begrenzt. Er arbeitet nach Echo-/Rauschunterdrückung und nach dem
  Mikrofon-Pegel-Regler (der als Grundeinstellung davor wirkt). Ziel und Obergrenze stehen in den Einstellungen.
- Die **Programm-Leitung** ist nur zum Hören: Sie hat keine Sprechen-/Dauer-Tasten, und weder Tasten noch Companion
  sprechen dort hinein.
- **Mikrofon-Aufbereitung** (Einstellungen → Mikrofon-Aufbereitung): Echo-Unterdrückung, Rauschunterdrückung (4 Stufen)
  und Auto-Pegel, mit den Voreinstellungen „Headset" (alles aus, beste Qualität) und „Lautsprecher"
  (Echo + Rauschen). Das ist das WebRTC-Audiomodul (Paket `aec-audio-processing`), dieselbe Algorithmen-Familie wie im
  Browser. Mit Kopfhörer alles aus lassen; bei Lautsprechern muss die Echo-Unterdrückung an sein.
- **Programm-Leitung dimmen** (Einstellungen → Programm-Leitung dimmen): wie in der Web-App wird die Programm-Leitung
  abgesenkt, wenn dich jemand in einer anderen Leitung anspricht und/oder wenn du dort sprichst (Standard −14 dB,
  1 s Reaktionszeit, beides einstellbar). Welche Leitung die Programm-Leitung ist, erkennt der Client an der
  Markierung am Server; die Karte zeigt „Programm" bzw. „Programm · gedimmt".
- Test ohne Soundkarte: `backend = "synthetic"` in `[audio]`; `dev/e2e_client.py` prüft zwei Clients gegen den
  lokalen Teststack.

### Installer für den Client

```powershell
pip install -e ".[client,dev,build]"
.\build-client.ps1          # Tests -> PyInstaller -> Selbsttest des gebauten Programms -> Installer
```

Ergebnis: `dist\installer\intercom-client-<Version>-setup.exe`. Er installiert pro Benutzer ohne Administratorrechte
(Desktop-Verknüpfung und „beim Anmelden öffnen“ sind wählbar); Einstellungen und Passwort bleiben beim Deinstallieren
erhalten. Im Ordner liegen zwei Programme: `IntercomClient.exe` (das Fenster) und `intercom-client.exe` (dasselbe mit
Konsole: `--version`, `--selftest` prüft, ob alle Bibliotheken im gebauten Programm geladen werden, und gibt bei
Problemen Meldungen aus). Der Gateway hat seinen eigenen Installer (`.\build.ps1`), beide lassen sich nebeneinander
installieren.

## SIP-Brücke (`intercom-sip`, Entwicklungsstand)

Die SIP-Brücke bringt einen **SIP-/RTP-Anruf** in Leitungen des Intercoms, zum Beispiel die Zuspielung eines
Übertragungswagens zur Sendeabwicklung über ein Rundfunk-Codec-Gerät (EBU Tech 3326 / N/ACIP). Sie ist ein eigener
Dienst im selben Paket (`intercom_sip`), nutzt dieselben Bausteine wie das Gateway (Anmeldung, Leitungsverbindung,
Jitter-Puffer mit Taktausgleich) und läuft auch als Container auf einem Server (`Dockerfile.sip`).

Ein Anruf kann **zwei Kanäle** tragen und ordnet jedem Kanal je Richtung eine Leitung zu:

| Kanal | wir senden zur Gegenstelle | wir empfangen von der Gegenstelle |
|---|---|---|
| links | Leitung „Regie-SAW“ | wird in „Regie-SAW“ gesprochen (normale Leitung, beide Richtungen) |
| rechts | Leitung „Kommentar“ (nur abgehört) | wird in „N-1“ gesprochen (nur hineingesprochen) |

Die Leitungen werden **nur solange der Anruf steht** betreten (`keep_lines = true` hält sie dauerhaft). Die
SIP-Teilnehmer erscheinen dann wie alle anderen mit dem Namen ihres Accounts in der Teilnehmerliste.

```powershell
copy sip.example.toml sip.toml        # anpassen; Passwörter nur über *_env
intercom-sip check -c sip.toml        # Konfiguration, Erreichbarkeit der Gegenstelle (OPTIONS), Logins
intercom-sip run -c sip.toml
```

- **Anrufen** (`mode = "dial"`): wir bieten genau ein Format an (Codec, Bitrate, Abtastrate, Kanalmodus). Geräte auf „Auto“
  folgen dem. **Angerufen werden** (`mode = "answer"`): nur Adressen aus `accept_from` dürfen anrufen; wir nehmen den ersten
  Codec ihres Angebots, den wir können.
- **Codecs:** MPEG Layer II (32/44,1/48 kHz, 32 bis 384 kbit/s, mono, stereo oder **dual-mono**), G.722, G.711 (µ-law/A-law)
  und lineares 16 Bit. **Dual-mono** ist bei Layer II derselbe Strom wie Stereo mit anderem Kanalmodus im Frame-Kopf; den
  Kopf schreiben wir um (libav kann das nicht selbst). AAC (LD/ELD, HE) ist **noch nicht** enthalten: dafür fehlt der
  FDK-AAC-Encoder in den üblichen libav-Paketen.
- **Überwachung (standardmäßig aus):** Kommandoleitungen sind PTT und bleiben minutenlang still, mancher Codec sendet dann
  gar keine Pakete. Deshalb wird nicht von selbst aufgelegt. Wer es will, setzt `rtp_timeout_s` (Sekunden ohne Pakete
  von der Gegenstelle; dann wird aufgelegt und neu angerufen). Aufgelegt wird immer, wenn die Gegenstelle ein BYE sendet.
  Das Log zeigt bei jedem Anruf das SDP der Gegenstelle in einer Zeile, der erste Blick bei Problemen mit fremden Geräten.
- **Sicherheit:** Audio wird nur von der Adresse angenommen, die das SDP nennt (hinter NAT `rtp_latch = true`). Angerufen wird
  nur von `accept_from`. SIP ist unverschlüsselt (UDP); die Ports (SIP und der RTP-Bereich) gehören in eine Firewall mit
  Freigabe nur für die Gegenstellen.
- **Konten (`[[account]]`) wie bei einem Hardware-Codec:** Die Brücke kann an **mehreren SIP-Servern gleichzeitig** mit je
  eigenen Zugangsdaten angemeldet sein. Solange sie angemeldet ist, ist sie dort unter ihrer Nummer anrufbar und kann über den
  Server anrufen. Ein Anruf-Profil wählt sein Konto mit `account = "..."`: ausgehend wählt es `target = "12345"` in der Domain
  des Servers, ankommend nimmt es Anrufe an, die für dieses Konto eintreffen (nur vom Server des Kontos, weitere Adressen
  über `accept_from`). Ohne Konto geht ein Anruf direkt an eine Adresse. Die Anmeldung wird rechtzeitig erneuert und beim
  Beenden entfernt; `intercom-sip check` probiert jede Anmeldung aus.
- **Statusseite mit Anrufen und Auflegen:** Die Brücke bringt eine eigene Seite mit (`http://127.0.0.1:8088`, Abschnitt `[web]`).
  Sie zeigt die Konten mit Anmeldestatus und pro Anruf-Profil: Zustand, Gegenstelle und Richtung, Dauer, Codec, Pegel je Kanal
  (gesendet und empfangen), Pakete, Verluste, Puffer, die verbundenen Leitungen und wer sonst darin ist. Mit **Anrufen**
  (Rufnummer eintippen oder leer lassen für das Ziel des Profils) und **Auflegen** wird der Anruf von Hand gesteuert.
  Standardmäßig erreicht nur dieser Rechner die Seite; für andere Adressen ist ein Passwort Pflicht (`password_env`,
  Browser-Abfrage, danach ein Cookie). Schaltflächen von fremden Webseiten aus werden abgewiesen.
- **Anruf-Profile (`mode`):** `dial` ruft von selbst an und wählt neu (`autostart = false`: erst auf Knopfdruck). Nach
  **Auflegen** wählt es nicht mehr von selbst, bis jemand wieder auf Anrufen drückt. `answer` wartet nur auf Anrufe.
  `both` wartet angemeldet auf Anrufe und ruft nur auf Knopfdruck an, so wie ein Hardware-Codec; dieselben Leitungen
  dienen dann in beide Richtungen.
- **Bewusst nicht enthalten (Stand jetzt):** TCP/TLS/SRTP, RTCP, DTMF, Weiterleitung.

**Geprüft:** Alles unter `tests/test_sip_*.py` (Codecs, RTP, SDP, SIP-Nachrichten, Anruf gegen sich selbst, Kanalzuordnung wie
oben, Konfiguration). Dazu gegen den lokalen Intercom-Teststack (`dev/e2e_sip.py`: echte Leitungen über den Media-Bridge) und
gegen **baresip** als unabhängige SIP-/RTP-Implementierung mit Layer-II-Modul (`dev/e2e_sip_baresip.py`, `dev/sip_dial_probe.py`):
Anrufe in beide Richtungen mit Layer II (dual-mono kommt mit sauber getrennten Kanälen an), G.722 und G.711. **Nicht geprüft** ist
der Betrieb mit echten Rundfunk-Codecs (Luci, Mayah, Prontonet u. a.): deren SDP-Eigenheiten (z. B. Parameter hinter `a=fmtp`)
zeigt erst ein echter Anruf.

### Lokaler SIP-Testserver (Kamailio)

Unter `dev/sipserver/` liegt ein Kamailio als Anmelde- und Vermittlungsserver für Tests, dazu eine kleine Verwaltungsseite.
Er vermittelt nur die Anrufe, der Ton läuft direkt zwischen den Geräten. Er ist **nicht für das Internet gedacht** (keine
Flutbegrenzung, kein TLS) und nur an `127.0.0.1` erreichbar.

```powershell
cd dev\sipserver
mkdir data; copy users.example.cfg data\users.cfg
docker compose up -d --build
```

Für Versuche mit mehreren Konten gibt es einen zweiten, unabhängigen Server (Realm `sip.other`, Benutzer in `data2\users.cfg`): `docker compose --profile second up -d` (SIP auf `127.0.0.1:5061`). Dafür dienen `dev/sip_multi_probe.py` und baresip-Clients.

- Verwaltungsseite: <http://127.0.0.1:8099> (wer angemeldet ist, laufende Gespräche, Benutzer anlegen und löschen)
- Kamailios eigene Befehlsseite: <http://127.0.0.1:18080/RPC>
- SIP: `127.0.0.1:5060` (UDP), Realm und Domain `sip.local`, Anmeldung mit Passwort. Beispielbenutzer stehen in `users.example.cfg`.
- Tote Gespräche (ein Gerät verschwindet ohne Auflegen) erkennt der Server selbst an Rückfragen alle 30 Sekunden.
- Aufräumen: `docker compose down`

## Installation (Entwicklungsstand)

Voraussetzung: Python 3.11 oder neuer, Windows.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[ui]"      # ohne Oberfläche: pip install -e .
```

## Einrichtung

1. Verfügbare Geräte anzeigen:

   ```powershell
   intercom-gateway devices
   ```

2. `gateway.example.toml` nach `gateway.toml` kopieren und anpassen: Server-Adresse, Production,
   Line, Account und die Gerätenamen aus Schritt 1.

3. Passwörter nicht in die Datei schreiben. Entweder in der Oberfläche eintragen (sie werden im
   Windows-Credential-Manager gespeichert) oder pro Line `password_env` setzen und die
   Umgebungsvariable beim Start bereitstellen.

4. Konfiguration, Geräte und Logins prüfen, ohne Audio zu starten:

   ```powershell
   intercom-gateway check -c gateway.toml
   ```

### Leitungen in der Oberfläche auswählen

In den Einstellungen der Oberfläche trägt man pro Leitung Benutzer und Passwort ein und klickt auf
„Anmelden und Leitungen laden“. Danach zeigt das Dropdown alle Leitungen, die dieser Account sehen darf
(Production › Leitung). Production- und Leitungsnummern müssen nicht mehr abgetippt werden. Normale Accounts sehen nur
Productions, in denen sie Mitglied sind, Super-Admins alle. Der Name der Leitung wird übernommen und kann
überschrieben werden. Unter „Manuell eintragen“ lassen sich Link oder Nummern weiterhin von Hand angeben.

## Betrieb

```powershell
intercom-gateway run -c gateway.toml     # verbindet alle Lines und hält sie verbunden
intercom-gateway status                  # Zustand und Pegel eines laufenden Gateways
intercom-gateway stop                    # laufendes Gateway beenden
intercom-gateway-ui                      # Oberfläche
```

Zustandsdateien und Log liegen unter `%LOCALAPPDATA%\intercom-gateway`
(abweichend mit `INTERCOM_GATEWAY_HOME`).

## Einstellungen pro Line

| Schlüssel | Bedeutung |
|---|---|
| `input_device` / `input_channels` | Aufnahmegerät und Kanäle. Mehrere Kanäle werden zu Mono gemischt. |
| `output_device` / `output_channels` | Wiedergabegerät. Die Line ist mono und wird auf diesen Kanälen ausgegeben. |
| `stereo` | `true` sendet genau zwei Eingangskanäle als Stereo statt als Mono-Mischung (Standard `false`). |
| `opus_mode` | `"voip"` (Standard, auf Sprache getrimmt) oder `"audio"` (für Musik / Programmton). |
| `opus_bitrate_kbps` | Datenrate des gesendeten Tons, 16–256 (Standard 96). Für Programmton z. B. 128. |
| `in_gain_db` / `out_gain_db` | Pegelanpassung in dB. Es gibt keine automatische Regelung. |
| `relay_type` | `ssrc-rewrite` (Standard) oder `mixed`. `mixed` wird nicht empfohlen. |

Die Line überträgt Mono mit den Opus-Einstellungen des Intercom-Servers (auf Sprache ausgelegt).

## Installer bauen

Der Installer packt die App mit PyInstaller und Inno Setup. Auf dem Zielrechner wird kein Python gebraucht.
Er installiert pro Benutzer, ohne Administratorrechte. Einstellungen und Passwörter bleiben beim Deinstallieren erhalten.

Voraussetzungen auf dem Build-Rechner: Windows, Python 3.11 oder neuer und
[Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`).

```powershell
pip install -e ".[ui,dev,build]"
.\build.ps1                  # Tests, PyInstaller, Smoke-Test, Installer
.\build.ps1 -NoInstaller     # nur der App-Ordner dist\IntercomGateway
```

Ergebnis: `dist\installer\intercom-gateway-<version>-setup.exe`. Die Version kommt aus `gateway/__init__.py`.

Der App-Ordner enthält zwei Programme:

| Datei | Zweck |
|---|---|
| `IntercomGateway.exe` | Oberfläche ohne Konsolenfenster. Sie startet sich für den Hintergrundbetrieb selbst mit `--gateway-run`. |
| `intercom-gateway.exe` | Kommandozeile: `devices`, `check`, `run`, `status`, `stop`. |

Bei der Installation kann man optional eine Desktop-Verknüpfung und den Start der Oberfläche beim Anmelden wählen.
Das Gateway selbst startet man weiterhin in der Oberfläche.

## Tests

```powershell
pip install -e ".[dev]"
pytest
```

Die Skripte unter `dev/` brauchen einen lokalen Teststack mit Docker und sprechen nur mit `127.0.0.1`. Für die
SIP-Skripte gegen baresip gilt dasselbe (ein Container `bs-test` mit Debian und baresip, Konfiguration siehe Kopf von `dev/e2e_sip_baresip.py`).

## Lizenz

MIT, siehe [LICENSE](LICENSE). Dieses Projekt ist unabhängig und steht in keiner Verbindung zu Eyevinn Technology.
