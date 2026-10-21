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

Frühe Version (0.1.0). Lauffähig sind:

- das Gateway ohne Oberfläche (`intercom-gateway`)
- eine Desktop-Oberfläche (`intercom-gateway-ui`, PySide6) mit Start, Stopp und Pegelanzeige
- ein Windows-Installer ist **noch nicht** vorhanden

Getestet wurde unter Windows mit Dante Virtual Soundcard. Das Gateway nutzt ausschließlich
**WASAPI**-Geräte. Ein Interface, das nur ASIO anbietet, taucht nicht auf.

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
| `in_gain_db` / `out_gain_db` | Pegelanpassung in dB. Es gibt keine automatische Regelung. |
| `relay_type` | `ssrc-rewrite` (Standard) oder `mixed`. `mixed` wird nicht empfohlen. |

Die Line überträgt Mono mit den Opus-Einstellungen des Intercom-Servers (auf Sprache ausgelegt).

## Tests

```powershell
pip install -e ".[dev]"
pytest
```

Die Skripte unter `dev/` brauchen einen lokalen Teststack mit Docker und sprechen nur mit `127.0.0.1`.

## Lizenz

MIT, siehe [LICENSE](LICENSE). Dieses Projekt ist unabhängig und steht in keiner Verbindung zu Eyevinn Technology.
