# Estado del proyecto (sesión de pruebas con hardware)

[🇬🇧 English](STATUS.md) · 🇪🇸 Español

Cámara: OM System OM-1 Mark II, firmware 1.00. PC: Arch Linux, Python 3.14.
Todo lo que sigue está medido contra la cámara real, no deducido.

## Lo que funciona

| función | comando | notas |
|---|---|---|
| descubrir la cámara | `discover` | mDNS `_ptp._tcp`, SSDP, escaneo 15740 |
| conectar por Wi-Fi | `info --host <ip>` | el GUID debe llevar la MAC emparejada **invertida** en los bytes 10..15 |
| live view | `liveview --host <ip>` | JPEG 1024×768 por `0x9484` |
| volcado en tiempo real | `daemon --host <ip> --out DIR` | eventos + sondeo, descarga atómica, estado persistente, subida opcional |
| disparo remoto | `remote --host <ip>` | requiere modo control por PC; ver limitación |
| bulb | `remote` → `b <segundos>` | abre con `0x9481(3)`, cierra con `0x9481(6)` |
| API de la app móvil | `app shoot` / `app bulb` / `app live` | HTTP CGI en modo AP (`192.168.0.10`) |

## Limitaciones reales de la cámara

1. **Modo control por PC (`0xD052=1`)**: la cámara lo concede en la primera
   sesión tras un arranque limpio. En sesiones posteriores responde `0x2019`,
   que en el firmware es el **código de fallo por defecto** del setter (no
   significa "ocupada"); reintentar no sirve. Dentro de una sesión ya concedida
   los disparos son ilimitados.
2. **`0x9481` necesita el par 3 → 6**: el 3 baja el obturador y el 6 lo suelta.
   Sin el 6 la cámara queda con el disparo pulsado y rechaza el siguiente.
   El parámetro 0 (que usa libgphoto2 para "release") aquí da `0x201D`.
3. **Nunca escribir `0xD06D = 0` si ya vale 0**: cuelga el servicio PTP/IP y
   saca la cámara de la Wi-Fi durante minutos. Leer primero y escribir sólo si
   está encendido.
4. **La enumeración debe ser ligera**: pedir `GetObjectInfo` de todos los
   objetos (50+) mientras se dispara tumba el servicio. Se usan dos llamadas por
   carpeta (todos los handles + filtro de formato `0x3001`).
5. **Los objetos nuevos se anuncian con el evento `0xC102`** (param1 = handle),
   no con `0xC002`.
6. La cámara se cae de la Wi-Fi con facilidad y **no vuelve sola**: hay que
   seleccionar la red en el menú (o reiniciarla).

## Procedimientos recomendados

### Volcar fotos hechas con el cuerpo (lo más estable)

```bash
python3 -m omtether daemon --host <ip> --out ~/Pictures/omtether
```

No toca el modo de la cámara, no bloquea los controles y reconecta solo.

### Astrofotografía con disparo desde el PC

```bash
# 1) arrancar la cámara y seleccionar la Wi-Fi
# 2) primera conexión tras el arranque, una sola sesión para toda la noche:
python3 -m omtether remote --host <ip> --out ~/Pictures/astro
```

Dentro de la consola: `s` dispara y descarga, `b 300` bulb de 300 s,
`p/g` escriben y leen propiedades, `?` ayuda. No cerrar la sesión: al
reconectar se pierde el permiso del modo.

### Vía de la app móvil (modo punto de acceso)

La cámara crea su propia Wi-Fi y el PC se une; todo va por HTTP a
`192.168.0.10`. Es el camino que usa OI.Share y no depende del permiso de
control por PC.

```bash
python3 -m omtether app info
python3 -m omtether app mode rec
python3 -m omtether app shoot
python3 -m omtether app bulb 300
python3 -m omtether app live --count 20 --out frames
python3 -m omtether app get /DCIM/100OMSYS/PA300001.JPG --out ~/Pictures/astro
```

## Herramientas de diagnóstico

- `tools/sniff.py` — captura UDP 1900/5353 y TCP 15740/80 con nombres de paquete.
- `tools/retry_connect.py` — reintentos de handshake con el motivo del `InitFail`.
- `tools/pair_and_start.py` — emparejamiento + transición de modo + espera.
- `tools/wake_and_shoot.py` — espera a que haya live view y dispara.
- Notas internas de análisis (no se distribuyen): desensamblado del firmware y
  del APK de OI.Share; aquí sólo quedan los hechos que salieron de ellas.

## Receta de stacking (verificada: 10 disparos seguidos)

```bash
python3 -m omtether remote --host <ip> --shots 50 --interval 5 \
    --skip-raw --out ~/Pictures/astro
```

- `--skip-raw` baja sólo el JPG (la mitad de tráfico; para apilar sobra).
- Dispara, espera el evento `0xC102` de la cámara y descarga al final de la
  serie, más un barrido final para los RAW que se anuncian tarde.
- Los disparos son ilimitados **dentro de la misma sesión**; si la cámara se cae
  el cliente reconecta y reintenta el disparo pendiente.

Claves que costaron encontrar y están implementadas:

1. El obturador se suelta con `0x9481(6)`; sin eso el siguiente disparo se
   rechaza (parecía "una foto por arranque").
2. **No sondear la tarjeta mientras la cámara escribe el RAW**: basta con
   escuchar el evento y esperar unos segundos. El sondeo la tumbaba.
3. No fiarse de `0xD052`: disparar y comprobar si aparece fichero.
4. Nunca escribir `0xD06D = 0` si ya vale 0 (cuelga la cámara varios minutos).

## Aplicación de escritorio (PyQt6)

```bash
/usr/bin/python3 -m omtether gui      # o: python3 -m omtether gui (se relanza solo)
```

Tres pestañas:

- **Tether (volcado)**: volcado continuo de lo que se dispare en el cuerpo.
  Directorio de destino y formato: `JPEG + RAW`, `Sólo JPEG` o `Sólo RAW`.
- **Control remoto**: live view con vista previa, botón de disparo y bulb con
  segundos. Los ficheros van al directorio elegido.
- **Astrofotografía**: serie de N disparos con intervalo, formato y directorio.
  Casilla **«Activar servicio INDI»** con puerto (7624 por defecto).

La interfaz habla con la cámara en hilos aparte: no se congela aunque la cámara
tarde o se caiga. Al cerrar, detiene los trabajos en curso.

La interfaz está en **español e inglés**: los textos viven en
`omtether/i18n.py` (dicts `ES` y `EN`, mismas claves) y pasan por `tr()`. El
selector de idioma de la barra superior cambia al momento reconstruyendo la
ventana (misma IP, misma pestaña) y guarda la elección en QSettings
(`language`); la primera vez el idioma sale de `LANG`/`LC_ALL`. Los mensajes
que escribe la librería por `logging` siguen en español.

## Servicio INDI (KStars / Ekos)

Dos formas de arrancarlo, con el mismo código:

```bash
# desde la app: pestaña Astrofotografía -> tick "Activar servicio INDI"
# desde la consola:
python3 -m omtether remote --host <ip> --indi 7624
```

KStars se conecta como servidor INDI a `localhost:7624`. Se publica un único
dispositivo `OMTether` con:

| propiedad | tipo | uso |
|---|---|---|
| `CONNECTION` | switch | conectar/desconectar |
| `DRIVER_INFO` | texto | nombre y versión |
| `CCD_EXPOSURE` | número | escribir segundos dispara una toma |
| `CCD_ABORT_EXPOSURE` | switch | abortar |
| `CCD1` | BLOB | JPEG de la última toma (base64) |

La conexión con la cámara es perezosa: se abre en el primer disparo de KStars.
El tiempo de exposición real es el que tenga configurado la cámara en el cuerpo
(no se toca `0xD01C`); el número de INDI se usa para el estado y el timeout.

Probado sin cámara: definiciones, `CONNECTION`, `CCD_EXPOSURE` → `Busy` →
`Alert` cuando la cámara no responde. El camino feliz (JPEG en el BLOB) está
probado con un disparador simulado: 92 KB de JPEG válido.

## Barra de cámara: IP, búsqueda y onboarding

Arriba de la ventana hay una barra única (la IP se comparte entre las tres
pestañas y se recuerda entre sesiones con QSettings):

- **Cámara: [IP]** — se puede escribir a mano. Botón **Probar**: comprueba que
  el puerto 15740 responde y lo dice en verde/rojo.
- **Buscar cámara…** — busca en la red local por tres vías a la vez: mDNS
  (`_ptp._tcp`), SSDP y barrido del puerto 15740 en las redes del equipo.
  Lista lo encontrado con su modo (`ptpip` o `http`) y permite usar esa IP.
- **Emparejar (USB)…** — el onboarding: leer las cuatro ranuras
  (`GetConnectPcInfo`), emparejar este equipo en la elegida
  (`SetConnectPcInfo`) o borrar una ranura. Muestra el nombre del equipo y las
  MACs que se van a registrar (las de `primary_macs()`, sin docker/veth).
- **Idioma** — selector (Español / English); se aplica al momento y se
  recuerda.

El onboarding recuerda los pasos en pantalla: USB → «Camera Control» en la
cámara → leer ranuras → emparejar → desconectar USB y unir la cámara a la Wi-Fi.

## ISO y exposición

Verificado contra la cámara:

- **ISO**: propiedad `0xD1C0` (uint32) en la OM-1; el valor es el número de ISO
  tal cual y `0xFFFFFFFF` es Auto. En cuerpos anteriores es `0xD007` (uint16).
  El rango se lee del descriptor: 80…102400, doce valores.
- **Velocidad**: propiedad `0xD01C` (uint32). El valor es una **fracción**:
  `(numerador << 16) | denominador`, en segundos. Ejemplos comprobados:
  `1/250` → `0x000100FA`, `2 s` → `0x0014000A`, `30 s` → `0x012C000A`,
  `0,5 s` → `0x000A0014`. Rango 1/32000…60 s.
- **Exposición cronometrada**: si el tiempo pedido no está en la tabla, o se
  quiere un tiempo largo, `expose()` abre el obturador (`0x9481(3)`), espera y
  lo cierra (`0x9481(6)`). Con el dial en M/Bulb eso da el tiempo real.

En INDI se publican `CCD_EXPOSURE` (segundos: fija la velocidad o hace bulb) y
`CCD_ISO` (número, rango leído de la cámara). En la pestaña de astrofotografía
hay campos de ISO y exposición; 0 significa «no tocar» / «la de la cámara».
