# SIP-Brücke auf dem Intercom-Server

Die Brücke läuft als **eigenes Compose-Projekt** neben dem Intercom-Stack (`~/eyevinn-intercom`). Sie hängt sich nur in
dessen Docker-Netzwerk (`eyevinn-intercom_intercom`), um den Manager zu erreichen. Starten, Anhalten und Entfernen der Brücke
berühren Mongo, Media-Server, Manager, Frontend und Proxy nicht, und nichts am Intercom-Stack muss dafür geändert oder neu
gestartet werden.

Was sie tut und wie man sie bedient: siehe [README.md](../../README.md), Abschnitt „SIP-Brücke“.

## Schritt 1: nur die Brücke und ihre Seite (keine Ports nach außen)

Auf dem Server, in einem neuen Ordner neben dem Intercom-Stack:

```bash
mkdir -p ~/eyevinn-intercom-sip && cd ~/eyevinn-intercom-sip
# die drei Dateien aus diesem Ordner hierher kopieren: docker-compose.yml, sip.example.toml, .env.sample
cp .env.sample .env       # SIP_WEB_PASSWORD eintragen (openssl rand -base64 18), SIP_REF auf einen Tag oder Commit
cp sip.example.toml sip.toml   # local_ip auf die öffentliche Adresse des Servers setzen
chmod 644 sip.toml        # enthält keine Geheimnisse; der Container läuft als "nobody" und muss sie lesen können (.env bleibt 600)
docker compose up -d --build
docker compose ps         # "healthy" nach etwa einer halben Minute
docker compose logs sip   # sollte "SIP listening ..." und "status page ..." zeigen
```

Die Seite ist **nur auf dem Server selbst** erreichbar (`127.0.0.1:8088`). Von deinem PC aus per SSH-Tunnel:

```bash
ssh -L 8088:127.0.0.1:8088 noicebard@<server>
# dann im Browser:  http://127.0.0.1:8088   (Benutzer admin, Passwort aus .env)
```

Auf der Seite: Konten, Leitungen und Anruf-Profile anlegen. Jede Leitung braucht einen **eigenen Intercom-Account** (Rolle
Teilnehmer in der Produktion), der vorher im Manager angelegt wurde; „Leitungen laden“ fragt den Manager, welche Leitungen er
betreten darf.

Prüfen, dass es am Intercom hängt: ein Profil mit `keep_lines = true` anlegen (Leitungen bleiben verbunden), die Leitung erscheint
in der Teilnehmerliste des Intercoms mit dem Namen des Kontos.

## Schritt 2: SIP und Ton nach außen (erst mit Firewall)

Damit andere anrufen können und der Ton ankommt, müssen zwei Dinge aus dem Internet erreichbar sein:

- SIP: `5060/udp`
- Ton (RTP): `5004–5100/udp` (der Bereich aus `rtp_port_min`/`rtp_port_max` in `sip.toml`)

Dazu in `docker-compose.yml` die beiden auskommentierten `ports`-Zeilen einschalten und `docker compose up -d` ausführen. **Vorher** die
Firewall (ufw und/oder die des Providers) so einstellen, dass diese Ports **nur von den Adressen der Gegenstellen** erreichbar
sind. Was die Brücke selbst schon tut: Anrufe nimmt sie nur von erlaubten Adressen an (`accept_from` oder der Server des Kontos),
alle anderen bekommen „403“; Ton nimmt sie nur von der Adresse, die im Gespräch ausgehandelt wurde. Das ersetzt keine Firewall.

## Betrieb

| Was | Befehl (im Ordner `~/eyevinn-intercom-sip`) |
|---|---|
| Log ansehen | `docker compose logs -f sip` |
| Neue Version bauen | `SIP_REF` in `.env` ändern, dann `docker compose up -d --build` |
| Anhalten, Einstellungen bleiben | `docker compose stop sip` |
| Ganz entfernen (Einstellungen bleiben im Volume) | `docker compose rm -sf sip` |
| Einstellungen sichern | `docker run --rm -v intercom-sip_sip_data:/d -v $PWD:/b alpine cp /d/sip-data.json /b/` |

Die Einstellungsdatei `sip-data.json` enthält die Passwörter der Konten und Leitungen im Klartext; sie liegt im Volume
`intercom-sip_sip_data` (nur für den Dienst lesbar). Sicherungskopien genauso behandeln.

Der Container läuft ohne Rechte (`nobody`, schreibgeschütztes Dateisystem, keine Capabilities, 512 MB und 1,5 Kerne als Grenze).
Stürzt er ab oder wird er entfernt, merkt das Intercom nichts davon außer, dass die Teilnehmer der Brücke aus den Leitungen verschwinden.
