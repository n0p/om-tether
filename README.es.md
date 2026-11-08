[🇬🇧 English](README.md) · 🇪🇸 Español

# omtether — tethering de cámaras OM System / Olympus en Linux

Reimplementación libre del protocolo que usa **OM Capture** (OM Digital
Solutions) para Windows, para poder disparar y descargar fotos desde Linux,
tanto por USB como por **Wi-Fi**.

## Instalación y uso rápido

No hay paquete: se usa desde el propio directorio del repo.

```bash
cd ~/src/om-tether

# 1) descubrir la cámara y emparejarla una vez (por USB)
python3 -m omtether discover
python3 -m omtether pair --list          # ver las 4 ranuras
python3 -m omtether pair --slot 2        # emparejar este ordenador
python3 -m omtether info --host <ip>     # comprobar PTP/IP

# 2) volcar según se dispara en el cuerpo
python3 -m omtether daemon --host <ip> --out ~/Pictures/omtether

# 3) control remoto / astrofotografía
python3 -m omtether remote --host <ip> --shots 20 --interval 5 --skip-raw
python3 -m omtether remote --host <ip> --shots 20 --interval 5 --exposure 2 --iso 1600
python3 -m omtether remote --host <ip> --indi        # servicio INDI en :7624

# 4) aplicación de escritorio (PyQt6)
python3 -m omtether gui
```

### Paquetes

En `packaging/` hay paquetes listos:

- **Arch / AUR** — `packaging/arch/PKGBUILD` instala el módulo Python, el
  lanzador `omtether`, la entrada de menú, el icono y la unidad de systemd de
  usuario. Se compila con `makepkg -si` dentro de ese directorio (o se instala
  el paquete `omtether` de AUR cuando esté publicado).
- **Debian / Ubuntu** — `python3 packaging/debian/build_deb.py` genera
  `dist/omtether_<versión>_all.deb`, que se instala con
  `sudo apt install ./omtether_<versión>_all.deb`. Sólo depende de `python3`;
  la GUI necesita `python3-pyqt6` y el emparejamiento por USB `python3-usb`
  (Recommends).

Los dos paquetes añaden **omtether al menú de aplicaciones**
(Gráficos/Fotografía), así que la GUI se abre sin tocar la consola. La entrada
de menú lanza `omtether gui`; el daemon es un servicio de usuario:
`systemctl --user enable --now omtether-daemon` (antes, pon la IP de tu cámara
en la unidad).

### La aplicación de escritorio

**Cómo se lanza**: `python3 -m omtether gui` desde el directorio del repo. Si el
`python3` de tu `PATH` no tiene PyQt6, el comando se relanza solo con
`/usr/bin/python3`, que es donde está instalado (Python 3.14 + Qt 6.12). También
vale directamente `/usr/bin/python3 -m omtether gui`.

**Barra superior** (común a las tres pestañas): campo con la **IP de la cámara**
(se recuerda entre sesiones), botón **Probar** (comprueba el puerto 15740),
**Buscar cámara…** (mDNS `_ptp._tcp`, SSDP y barrido de 15740) y
**Emparejar (USB)…** (onboarding: leer/escribir/borrar las ranuras
`ConnectPcInfo`). A la derecha hay un selector de **idioma** (Español /
English): al cambiarlo se reconstruye la ventana en el idioma nuevo al momento,
conservando la IP de la cámara y la pestaña activa, y la elección se recuerda
entre sesiones. La primera vez el idioma sale de `LANG`/`LC_ALL` (español si
empieza por `es`, inglés en otro caso).

**Pestañas**:

| Pestaña | Qué hace |
|---|---|
| **Tether (volcado)** | Volcado continuo de lo que dispares en el cuerpo. Directorio de destino y formato: `JPEG + RAW`, `Sólo JPEG` o `Sólo RAW`. |
| **Control remoto** | **Vista en vivo** con previsualización (JPEG 1024×768, ~1 fps) en el panel grande, botón **Disparar** y **Bulb** con segundos. |
| **Astrofotografía** | Serie de N disparos con intervalo, ISO y exposición (0 = no tocar / la de la cámara), formato `JPEG + RAW` / `Sólo JPEG` / `Sólo RAW`, más la casilla **Activar servicio INDI** para KStars/Ekos. |

La vista en vivo se arranca con **Conectar** en la pestaña de control remoto; el
panel muestra `sin live view` hasta que llega el primer fotograma. Todo el
tráfico con la cámara va en hilos aparte, así que la ventana no se bloquea
aunque la cámara tarde o se caiga, y al cerrar se detienen los trabajos.

Los textos de la interfaz viven en `omtether/i18n.py` (dos diccionarios, `ES` y
`EN`). Los mensajes que llegan de la librería por `logging` siguen en español.

## Qué hay dentro del instalador original

`OCSetup.exe` es un bootstrapper con un CAB embebido:

```
OCSetup.exe
└── outer.cab
    ├── OCSetup.exe          (stub)
    ├── setup.ini
    ├── MSI/OMDS Install.msi (acciones custom en C++)
    ├── MSI/OC_Pack_Framework.cab
    │   └── application.cab  ← la aplicación (129 MB)
    └── Data/VC_redist.*.exe
```

Dentro de `application.cab` está la aplicación MFC (`OM Capture.exe`,
`OlyCameraLib.dll`, `OlyComSvc.exe`, ...). Lo relevante para el protocolo:

* `OlyCameraLib.dll` es el "SDK" de cámara: habla con la pila **WPD**
  (Windows Portable Devices) e importa `iphlpapi.dll` para leer las IPs locales.
  Expone `IOlyCamera::*` y contiene los nombres de las operaciones propietarias
  (`OLYSetConnectPCInfo`, `OLYTransferModeStartStop`, `OLYGetUntransferObjectList`,
  `StartStationMode`, ...), los códigos de evento `OLY_EVCMD_*` (0xC001–0xC114)
  y los VID `33A2` (OM Digital) / `07B4` (Olympus).
* No hay ni un solo `ws2_32.dll` importado en toda la aplicación: **toda la red
  la pone Windows**. Microsoft documenta que su transporte "MTP/IP" para WPD es
  el binding **PTP/IP de CIPA** más **UPnP** para descubrimiento y emparejamiento
  ("we used the PTP/IP binding, then used UPnP for pairing and discovery",
  blog del equipo WPD). Es decir: la cámara habla PTP/IP estándar (TCP 15740).
* Las operaciones propietarias de Olympus/OMD ya están documentadas en
  libgphoto2 (`camlibs/ptp2/ptp.h`), que las obtuvo de trazas de OM Capture:
  `0x9481` disparo, `0x9484` live view, `0x9486`/`0x948A` sondeo de
  propiedades, `0x94BA`–`0x94C4` modo transferencia/estación/ConnectPcInfo.
  Este proyecto usa esos códigos (son hechos, no código copiado).

## Estado

| Pieza | Estado |
|---|---|
| Transporte PTP/IP (handshake, canal de eventos, fases de datos) | implementado y probado contra un servidor simulado (`tests/`) |
| Sesión PTP (DeviceInfo, propiedades, objetos) | implementado |
| Descubrimiento (SSDP + escaneo del puerto 15740) | implementado |
| Live view (`0x9484`) | implementado |
| Disparo + descarga (`0x9481`, eventos, `GetObject`) | implementado |
| Volcado en tiempo real (`watch`) | implementado |
| Emparejamiento Wi-Fi (`0x94BF` SetConnectPcInfo por USB) | layout recuperado del binario; falta validar en hardware |
| Probado contra hardware | **pendiente** |

## Uso

```bash
python3 -m omtether discover                 # busca la cámara en la LAN
python3 -m omtether info --host 192.168.1.50 -v
python3 -m omtether liveview --host 192.168.1.50 --http 8080     # ver en el navegador
python3 -m omtether liveview --host 192.168.1.50 --out /tmp/lv   # guardar fotogramas
python3 -m omtether capture --host 192.168.1.50 --count 3 --out ~/Pictures/sesion
python3 -m omtether watch --host 192.168.1.50 --out ~/Pictures/sesion
python3 -m omtether watch --host 192.168.1.50 --out ~/Pictures/sesion --keepalive 5
python3 -m omtether raw --host 192.168.1.50 --op 0x9486   # sondear el protocolo
```

`watch` es el volcado en tiempo real: se queda escuchando los eventos de la
cámara y descarga cada foto en cuanto aparece, con un sondeo de
`GetObjectHandles` como red de seguridad.

`discover` prueba en este orden: **mDNS `_ptp._tcp`** (lo que anuncia la propia
cámara), SSDP, escaneo del 15740, y la IP del AP de la cámara (`192.168.0.10`).
Cada resultado lleva su modo: `ptpip` (tethering) o `http` (OI.Share).

`liveview --http 8080` levanta un servidor MJPEG: abres
`http://localhost:8080/` en el navegador y ahí tienes el visor en vivo.

## Pruebas sin cámara

`tests/mock_camera.py` es una cámara PTP/IP falsa (handshake, DeviceInfo,
propiedades, live view, disparo con evento y descarga). Con ella se validan
transporte, sesión, captura, MJPEG y `watch`:

```bash
python3 tests/run_all.py     # 8/8
```

`raw` sirve para explorar la cámara: manda cualquier opcode con parámetros y
muestra respuesta y datos. Es la herramienta para terminar de mapear lo que
falte.

## Modo Wi-Fi: cómo se empareja según OM Capture

Los textos de la interfaz (extraídos de `i18n/ENU/app.loc`) describen:

1. Conectar la cámara al ordenador **por USB** y elegir *Camera Control* en la
   cámara.
2. Quitar el USB y conectar la cámara al **punto de acceso Wi-Fi**.
3. El ordenador abre el cortafuegos para el perfil de red correspondiente
   (*Domain/Private/Public*), lo que implica que hay tráfico entrante hacia el
   ordenador.

O sea: el ordenador registra sus datos en la cámara por USB (operación `0x94BF`)
y después, ya por Wi-Fi, se habla PTP/IP. El registro es
**{nombre del ordenador + MACs de las tarjetas Ethernet/Wi-Fi}**, en una de las
4 ranuras que guarda la cámara:

```
u8   nameLen           longitud del nombre (0..15)
u8   name[nameLen]     ANSI, sin NUL
u32  count             nº de adaptadores (0..5)
u8   entries[count][8] MAC big-endian + dos bytes a cero
```

Un parámetro PTP: `ranura - 1`. `omtether pair` construye ese registro con el
nombre del equipo y las MACs reales, lo escribe y lo relee con `0x94C0` para
comprobar que la cámara devuelve lo mismo:

```bash
python3 -m omtether pair --dry-run          # ver el payload sin tocar la cámara
python3 -m omtether pair --slot 1           # escribir (cámara por USB en Camera Control)
python3 -m omtether pair --list             # volcar las 4 ranuras
```

## Los dos modos de Wi-Fi (no son el mismo camino)

La cámara distingue por firmware varios tipos de enlace (`WIFI_LINK_KIND_PRIVATE 0`,
`AUTO 1`, `EASY_TRANS 2`, `STA_MODE`) y un modo de aplicación
(`WIFI_APP_MODE_CAMERA_CONTROL`). En la práctica hay dos formas de conectarla:

| | AP propio de la cámara ("private") | Infraestructura (`STA_MODE`) |
|---|---|---|
| Quién es el AP | la cámara | tu router |
| IP de la cámara | `192.168.0.10`, con DHCP propio | la que dé el router |
| Descubrimiento | unirse a su SSID; UPnP y CGI en el puerto 80 | SSDP + escaneo; PTP/IP en 15740 |
| API | **HTTP CGI de OI.Share** (62 CGI: `get_caminfo`, `get_imglist`, `exec_takemotion`, `exec_takemisc?com=startliveview&port=…`, `OLYSetConnectPCInfo`…) | **PTP/IP + extensiones 0x94xx** |
| Para qué sirve aquí | identificar la cámara y, si hiciera falta, emparejar sin USB (`omtether http-info`) | tethering completo: disparo, live view y volcado en tiempo real |

El **tethering tal cual lo hace OM Capture es el segundo**: la cámara se une al
AP, entra en *Camera Control* y el ordenador habla PTP/IP. Según OM Digital, la
transferencia por punto de acceso está soportada en **OM-1, OM-1 Mark II, E-M1X
y E-M1 Mark III**; el resto de cuerpos Wi-Fi se quedan en el modo AP/privado de
OI.Share (HTTP). El firmware lo confirma por su lado: el servidor HTTP se
autolimita con `[httpd]not_CONN_MODE_TRANSFER`, así que en Camera Control no
hay CGI que valga.

`omtether discover` mira **los dos**: si encuentra el 15740 lo marca `ptpip`
(tethering) y si encuentra el 80 con CGI de OI.Share lo marca `http`. Cuando
sólo sale `http`, la cámara está en modo AP/privado y el aviso te lo dice.
Queda por verificar en hardware si la cámara expone además el 15740 en modo AP;
el propio `discover` lo responderá en cuanto la tengas delante.

## Prueba de campo

Con la cámara en modo tether Wi-Fi y en la misma red que el ordenador:

```bash
sudo tcpdump -ni any 'port 15740 or port 1900' &   # ver qué emite la cámara
python3 -m omtether discover -v
python3 -m omtether info --host <ip> -vv
```

Si `info` responde, el resto (live view, disparo, descarga) se puede validar ya
con los comandos de arriba.

## Estructura

```
omtether/
  ptpip.py      transporte PTP/IP (CIPA DC-005)
  ptp.py        sesión PTP (operaciones, propiedades, objetos)
  olympus.py    extensiones OM System/Olympus (0x94xx, propiedades D0xx)
  discovery.py  SSDP + escaneo de puerto
  cli.py        interfaz de línea de órdenes
  pairing.py    emparejamiento Wi-Fi (USB)
  gui.py        aplicación de escritorio (PyQt6)
  gui_setup.py  diálogos de búsqueda en red y emparejamiento USB
  i18n.py       textos de la interfaz (español / inglés)
tests/          cámara simulada + test del transporte
docs/           notas de protocolo (docs/ESTADO.md, docs/STATUS.md)
```

## Estado real (medido en una OM-1 Mark II, fw 1.00)

Lo que funciona hoy, por Wi-Fi, contra la cámara:

| función | estado | notas |
|---|---|---|
| handshake PTP/IP | ✅ | el GUID debe llevar la MAC emparejada **invertida** en los bytes 10..15 |
| `info` (DeviceInfo) | ✅ | 48 operaciones por Wi-Fi (60 por USB) |
| live view | ✅ | `0x9484`, JPEG 1024×768 |
| volcado en tiempo real | ✅ | `daemon`: eventos + sondeo, descarga atómica, estado persistente |
| disparo remoto | ⚠️ | `0x9481(3)` **sólo en modo control desde el ordenador** (`0xD052=1`) |
| control desde el ordenador | ⚠️ | la cámara lo concede **una vez por arranque**; después `DeviceBusy` |

### La limitación del disparo remoto

`0xD052 = 1` se acepta una vez por encendido de la cámara (con el live view
apagado: con `0xD06D != 0` la escritura devuelve `0x2019`). Tras el primer
disparo la propiedad vuelve a 2 y no hay manera de recuperarla sin reiniciar.
Con el modo en 2, `0x9481` responde `0x2001` o `0x201D` pero **no genera
fichero**: un ACK no es una foto.

### Receta para astrofotografía (la que funciona hoy)

Como el intervalo lo puede hacer la propia cámara, el reparto natural es:

1. Configura en la cámara el **disparo por intervalos** (o dispara a mano).
2. Deja el daemon volcando:
   ```bash
   python3 -m omtether daemon --host <ip-de-la-cámara> --out ~/Pictures/astro \
       --upload-dir ~/sync/astro
   ```
   Cada foto aparece en el ordenador en segundos, sin tocar el modo de la cámara
   y sin que se bloquee ningún control.

Para una foto suelta controlada desde el ordenador, `remote --shots 1` funciona
en la primera sesión tras encender la cámara. El modo interactivo (`remote` sin
`--shots`) mantiene la sesión abierta con keep-alive para una noche entera.

## Marcas

OM System, Olympus, OM-1, OM Capture y OM Share son marcas comerciales o marcas
registradas de OM Digital Solutions Corporation. Este proyecto no está afiliado,
respaldado ni patrocinado por OM Digital Solutions. El resto de marcas son
propiedad de sus respectivos titulares.

## Licencia

MIT — ver [LICENSE](LICENSE). Puedes usarlo, modificarlo y redistribuirlo,
incluso dentro de software cerrado, conservando el aviso de copyright.
