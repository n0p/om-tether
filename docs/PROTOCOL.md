# Protocolo de OM Capture / OM System (tethering)

## 1. Transporte: PTP/IP (CIPA DC-005), no un protocolo propio

La aplicación de Windows no abre ni un socket: no importa `ws2_32.dll` en
ningún módulo. Todo el tráfico lo pone la pila **WPD** de Windows, y Microsoft
documenta que su transporte "MTP/IP" es el **binding PTP/IP de CIPA** más
**UPnP** para descubrimiento y emparejamiento. Por tanto la cámara habla PTP/IP
estándar y se puede hablar con ella desde Linux.

Formato de paquete (little endian):

```
uint32 longitud (incluye la cabecera de 8)
uint32 tipo
bytes  carga útil
```

| tipo | nombre | dirección |
|---|---|---|
| 1 | Init_Command_Request | PC → cámara |
| 2 | Init_Command_Ack | cámara → PC |
| 3 | Init_Event_Request | PC → cámara |
| 4 | Init_Event_Ack | cámara → PC |
| 5 | Init_Fail | cámara → PC |
| 6 | Operation_Request | PC → cámara |
| 7 | Operation_Response | cámara → PC |
| 8 | Event | cámara → PC |
| 9 | Start_Data | ambos |
| 10 | Data | ambos |
| 11 | Cancel_Transaction | PC → cámara |
| 12 | End_Data | ambos |
| 13/14 | Ping/Pong | ambos |

* `Init_Command_Request`: GUID (16) + nombre en UTF-16LE + `\0\0` + versión
  (uint32, 0x00010000).
* `Init_Command_Ack`: número de conexión (uint32) + GUID (16) + nombre +
  versión.
* `Init_Event_Request`: número de conexión (uint32).
* `Operation_Request`: fase de datos (uint32: 1 = sin datos o datos de entrada,
  2 = datos de salida) + opcode (uint16) + id de transacción (uint32) +
  parámetros (uint32 cada uno).
* `Operation_Response`: código de respuesta (uint16) + id de transacción +
  parámetros.
* `Event`: código (uint16) + id de transacción + parámetros.
* `Start_Data`: id de transacción + longitud total (uint64).

Puerto TCP 15740 (el número ISO de PTP), canal de órdenes y canal de eventos
(dos conexiones).

## 2. Descubrimiento

El firmware del OM-1 II implementa **mDNS/DNS-SD** (`wifi_mdns.c`) y anuncia el
servicio **`_ptp._tcp`** en el puerto 15740 con un registro TXT (`vid=…`); eso
es lo que recomienda CIPA para localizar un respondedor PTP/IP y es la primera
vía que prueba `omtether discover`. Después, SSDP/UPnP (que en la cámara sirve
al lado OI.Share, vía `miniupnpd`), luego escaneo del 15740 en la LAN y, por
último, la sonda a `192.168.0.10` (IP del AP propio de la cámara).

## 3. Emparejamiento Wi-Fi (según la UI de OM Capture)

`i18n/ENU/app.loc`:

```
80771  1. Connect camera to PC with a USB cable.
       2. Select Camera Control (tethering icon) on the camera monitor.
       3. Press OK on the camera.
80774  Remove the USB cable and connect camera to access point via Wi-Fi.
80776  Link already exists. Remove the USB cable, and connect camera to access
       point via Wi-Fi.
80791  Select network in which to allow camera-PC linkage.
```

Es decir: primero USB (la cámara entra en modo control), luego la cámara se une
al AP y el PC abre el cortafuegos para el perfil de red. Las operaciones
propietarias implicadas (tabla de abajo) son `0x94BF` SetConnectPcInfo,
`0x94C0` GetConnectPcInfo(index), `0x94C1` ClearConnectPcInfo(index) y
`0x94C3` StationMode (entrar/salir del AP).

### Formato del registro ConnectPcInfo

Recuperado del parser de respuesta de `GetConnectPcInfo` (VA `0x1002AB50` de
`OlyCameraLib.dll`) y reproducido byte a byte por `omtether pair --dry-run`:

```
off 0            u8   nameLen            0..15
off 1            u8   name[nameLen]      ANSI (CP_ACP), sin terminador
off 1+nameLen    u32  count              0..5 adaptadores
off 5+nameLen    u8   entries[count][8]  MAC big-endian + 00 00
```

* Un parámetro PTP: `ranura - 1` (la cámara guarda 4 PCs).
* `name` = nombre del equipo (`GetComputerNameW` en Windows; en Linux,
  `hostname` sin dominio, 15 caracteres).
* Las MACs salen de adaptadores con `IfType == 6` (Ethernet) o `0x47` (802.11);
  el empaquetado son dos `u32` big-endian (para 6 bytes: `AA BB CC DD | EE FF
  00 00`).
* La respuesta se lee igual y las entradas a cero se descartan.

Ejemplo (`OM-PC`, una MAC, ranura 1): parámetro `0x00000000` y datos
`05 4F4D2D5043 01000000 AABBCCDDEEFF0000` (18 bytes).

## 4. Operaciones propietarias OM System / Olympus (0x94xx)

Están documentadas en libgphoto2 (`camlibs/ptp2/ptp.h`, obtenidas de trazas de
OM Capture; entre paréntesis la anotación "Capture UI").

| opcode | nombre | notas |
|---|---|---|
| 0x9481 | OMD_Capture | param 3 = disparo, 6 = fin (bulb) |
| 0x9482 | GetDateTime | |
| 0x9483 | MagnifyLiveViewPoint | UI 0x03 |
| 0x9484 | GetLiveViewImage | param 1 = 1; devuelve JPEG |
| 0x9485 | OMD_GetImage | JPEG desde SDRAM |
| 0x9486 | ChangedProperties | polling "pesado" (~42 propdescs) |
| 0x9487 | MFDrive | enfoque manual |
| 0x9488 | MagnifyLiveViewArea | UI 0x08 |
| 0x9489 | SetProperties | lista de propiedades uint16 |
| 0x948A | PollProperties | polling ligero (~15) |
| 0x948B | SetProperties2 | segunda lista |
| 0x948D | OpticalZoomDrive | UI 0x0E |
| 0x94A1 | DetectOneTouchWB | UI 0x18 |
| 0x94BA | TransferModeStartStop | modo transferencia inalámbrica |
| 0x94BB | GetUntransferList | objetos pendientes de transferir |
| 0x94BC | GetLocalObjectInfo | |
| 0x94BD | GetLocalObject | |
| 0x94BE | DeleteLocalObject | |
| 0x94BF | SetConnectPcInfo | UI 0x1A — emparejamiento |
| 0x94C0 | GetConnectPcInfo | UI 0x1B — por índice |
| 0x94C1 | ClearConnectPcInfo | UI 0x1C — por índice |
| 0x94C2 | GetOTWBKelvin | UI 0x1D |
| 0x94C3 | StationMode | entrar/salir del AP |
| 0x94C4 | GetAFTargetFrames | UI 0x1E |

## 5. Propiedades relevantes (0xD0xx)

| código | nombre | notas |
|---|---|---|
| 0xD002 | Apertura | |
| 0xD003 | Modo de enfoque | |
| 0xD004 | Medición | |
| 0xD006 | Modo de exposición | |
| 0xD007 | ISO (modelos antiguos) | |
| 0xD009 | Modo de avance | |
| 0xD00D | Formato de imagen | 0x020 RAW, 0x101 LF JPEG, ... |
| 0xD01C | Velocidad de obturación | |
| 0xD01E | Balance de blancos | |
| 0xD052 | CameraControlMode | 1 = control por PC, 2 = autónomo |
| 0xD06D | LiveViewModeOM | 0x04000300 activa live view en OM-1 |
| 0xD0DC | CaptureTarget | 1 = SDRAM, 2 = tarjeta |
| 0xD1C0 | ISO (OM-1 y posteriores) | sustituye a 0xD007 |

## 6. Eventos

| código | nombre |
|---|---|
| 0x4002 | ObjectAdded (estándar) |
| 0x400D | CaptureComplete |
| 0xC001 | OLY_CreateRecView |
| 0xC002 | OLY_ObjectAdded |
| 0xC003 | OLY_AF_Frame |
| 0xC004 | OLY_DirectStoreImage |
| 0xC005 | OLY_CompleteCameraControlOff |
| 0xC006 | OLY_AF_FrameOverInfo |
| 0xC008 | OLY_DevicePropChanged |
| 0xC00C | OLY_ImageTransferModeFinish |
| 0xC00D | OLY_ImageRecordFinish |
| 0xC00E | OLY_SlotStatusChange |
| 0xC00F | OLY_PrioritizeRecord |
| 0xC010 | OLY_FailCombiningAfterShooting |
| 0xC011 | OLY_NotifyAFTargetFrame |

## 7. Secuencias

**Entrar en control por PC**: leer `0xD052`, escribir 1, esperar evento.

**Disparo** (lo que hace OM Capture / libgphoto2):

1. `0x9481` param 3 (obturador abajo)
2. `0x9481` param 6 (obturador arriba)
3. `0x9486` para recoger propiedades cambiadas
4. esperar evento `0xC002`/`0xC00D`/`0x4002`; el parámetro 1 es el handle del
   objeto nuevo
5. `0x1008` GetObjectInfo + `0x1009` GetObject para descargarlo

**Live view**: escribir `0xD06D` = 0x04000300, luego `0x9484` param 1 en bucle;
cada respuesta es un JPEG (puede venir con cabecera: se busca el marcador
`FFD8FF`).

**Volcado en tiempo real**: escuchar `0xC002`/`0x4002` y descargar el handle
anunciado; como red de seguridad, sondear `0x1007` GetObjectHandles. Para el
modo inalámbrico existen además `0x94BA` (activar modo transferencia) y `0x94BB`
(lista de objetos aún no transferidos).

## 8. Lo que dice el firmware de la cámara (OM-1 Mark II 1.3)

Fuente: firmwares oficiales desempaquetados (ver `docs/PROTOCOL.md` §1)
(módulos `s0_mod00` y `s2_mod06`). Es la propia cámara, no la app de PC.

**Confirma el transporte**: hay una implementación PTP/IP interna con los tres
estados `ptpip` / `cdc` (canal de órdenes y datos) / `evc` (canal de eventos),
la tabla de tipos de paquete y la fase de datos con nombre explícito
`atpc_olptpl_data_phase_I2R` (Initiator → Responder). Nada propietario encima.

**Tipos de paquete 13 y 14**: el firmware los llama
`PTPIP_PACK_TYPE_PROBE_REQUEST_PACKET` y `..._PROBE_RESPONSE_PACKET` (no
Ping/Pong). Mismo formato sin carga útil; responder al 13 con un 14 es correcto.

**Modos de enlace** (`LINK_PROTOCOL_*`): `STORAGE`, `MTP`, `PC_LINK`,
`CAMERA_CONTROL`, `IC_RECORDER`, `RAW_EDIT`, `PC_APP`, `WEB_CAMERA`. El
tethering de OM Capture es `CAMERA_CONTROL`; OI.Share es `PC_APP`. No se negocia
en el handshake de PTP/IP: es el modo en el que está la cámara.

**Modo estación (Wi-Fi de infraestructura)**: tareas `tpc_sta_mode*` con
`connect_pc_state`, `pic_trans_state`, `untrans pic num`, `alive time`,
`alive timer` y una tarea llamada `PC_CHECK_ALIVE`. La cámara espera que el PC
se mantenga vivo: de ahí la operación `0x94BA` (que OM Capture llama
`OLYTransferModeAlive`). `omtether watch --keepalive 5` la envía periódicamente.

**Infraestructura de red**: la cámara corre su propio `upnpd` (de ahí el
descubrimiento SSDP), un servidor DHCP (`dhcpcd`/`udhcpc`) y usa `192.168.0.10`
cuando hace de AP. Chip Wi-Fi **CYW43455** (BCM4345C0, SDIO, `brcmfmac`) y BLE
con la pila BSA de Broadcom.

**Emparejamiento por PIN**: es **Bluetooth**, no PTP/IP. Las cadenas
`acceptPairing`/`requestPinCode`/`PinCodeOK` pertenecen a
`Bluetooth::`/`CBC_BLUETOOTH_*` (pila BSA, mando RM-WR1 y OI.Share por BLE).
No hay que contestar ningún PIN para hablar PTP/IP.

**Superficie HTTP (OI.Share)**: el firmware lleva embebido el descriptor
completo de la API, versión 5.20, con 62 CGI — incluidos
`OLYSetConnectPCInfo` (POST), `OLYGetConnectPCInfo` y `OLYClearConnectPCInfo`,
`exec_takemotion` (disparo), `exec_takemisc` (`startliveview&port=`),
`get_imglist`, `get_thumbnail`, `set_camprop`… Copia en
`docs/firmware/oishare_cgi_descriptor.xml`. El propio servidor se autolimita:
`[httpd]not_CONN_MODE_TRANSFER` — solo atiende en modo transferencia
(`PC_APP`), no en `CAMERA_CONTROL`. Es decir: el HTTP no sustituye al PTP/IP
para el tethering, pero sí permite emparejar (`OLYSetConnectPCInfo`) sin USB si
la cámara está en modo OI.Share.

**Operaciones de radio** `adj_p15740_op_WifiRadioExamin` /
`BLERadioExamin`: son de modo ajuste/fábrica (`ADJ_*`, junto a
`MDN_WIFI_RADIO_EXAMIN`), no del firmware de usuario. No cuentan como API.

## 9. Los dos modos de conexión Wi-Fi

Vocabulario del propio firmware (OM-1 II, módulo `s0_mod00`):

```
WIFI_LINK_KIND_PRIVATE   0     ; la cámara hace de AP
WIFI_LINK_KIND_AUTO      1
WIFI_LINK_KIND_EASY_TRANS 2    ; transferencia fácil (móvil)
WIFI_LINK_KIND_STA_MODE        ; infraestructura: la cámara se une a un AP
WIFI_APP_MODE_CAMERA_CONTROL   ; modo aplicación: control por PC
LINK_PROTOCOL_CAMERA_CONTROL   ; modo de enlace del tethering
```

**Modo AP propio ("private").** La cámara levanta su AP, corre DHCP y queda en
`192.168.0.10`. Expone UPnP y el servidor HTTP con la API de OI.Share
(descriptor v5.20, 62 CGI; ver `docs/firmware/oishare_cgi_descriptor.xml`).
Es el camino que usa la app del móvil y el que soporta prácticamente toda la
gama Wi-Fi. Aquí el control y la descarga se hacen por HTTP:
`get_caminfo`, `get_imglist`, `get_thumbnail`, `get_screennail`,
`exec_takemotion` (disparo), `exec_takemisc?com=startliveview&port=…` (live
view en streaming), `set_camprop`/`get_camprop`. `omtether http-info` sirve
para identificarla en este modo.

**Modo infraestructura (`STA_MODE`).** La cámara se une a tu red y entra en
*Camera Control*. Es el único camino del tethering de OM Capture, y el que usa
este proyecto: PTP/IP en 15740 con las operaciones `0x94xx`. Según OM Digital,
la transferencia por punto de acceso está disponible en OM-1, OM-1 Mark II,
E-M1X y E-M1 Mark III. En el firmware, el modo estación tiene su propia tarea
(`tpc_sta_mode*`) con `connect_pc_state`, `pic_trans_state`, `untrans pic num`,
`alive timer` y `PC_CHECK_ALIVE`.

**No son intercambiables.** El servidor HTTP se autolimita:
`[httpd]not_CONN_MODE_TRANSFER` — en Camera Control no atiende CGI. Y el modo
AP no es el que OM Capture usa para tetherizar. Lo que sí comparten es el
emparejamiento: `OLYSetConnectPCInfo` existe como operación PTP (`0x94BF`, por
USB) y como CGI (`OLYSetConnectPCInfo.cgi`, por HTTP en modo OI.Share).

**Pendiente de verificar en hardware:** si en modo AP la cámara expone además
el puerto 15740. `omtether discover` lo comprueba (marca `ptpip`, `http` o
ambos) en cuanto haya una cámara en la red.


## 10. La condición real de aceptación del PTP/IP (verificada en hardware)

El servidor PTP/IP de la OM-1 II sólo compara **seis bytes**: `GUID[10..15]` del
`Init_Command_Request`. No mira el nombre, ni el GUID completo, ni la versión
(`0x00010000`). Esos seis bytes se comparan contra la tabla de MACs que escribe
`SetConnectPcInfo` (`0x94BF`), y el escritor del registro **invierte cada MAC**
(`dst[5-i] = src[i]`, `0x189FFCD`), así que el GUID que acepta es:

```
00 00 00 00 00 00 00 00 00 00  +  MAC[5] MAC[4] MAC[3] MAC[2] MAC[1] MAC[0]
```

Ejemplo: ethernet `aa:bb:cc:dd:ee:ff` → GUID `00000000-0000-0000-0000-ffeeddccbbaa`.
Vale cualquiera de las 5 MACs del registro, no sólo la de la interfaz por la que
llega la conexión. `omtether/ptpip.py:default_guid()` lo deriva así.

Cadena en el firmware (imagen µITRON, `addr = 0x40000000 + offset`):
`0x17EC962` parsea el paquete y llama a `0x17EF14A`, que exige
`g_pc_number (0xA320F97C) >= 0`, hace `memcmp(GUID+10, mac[i], 6)` sobre el
registro `base + pc*30` (4 slots × 5 MACs × 6 B) y, si no hay coincidencia,
emite `Init_Fail` con reason 1 fijado a mano (`mov 1,d1` en `0x17ECA9E`).
El ACK es de 34 B: tipo 2, conn 15740, nombre vacío, versión `00 00 01 00`.

Con esto la cámara acepta en modo estación normal, sin tocar `0xD052`: 48
operaciones por PTP/IP (frente a 60 por USB), incluidas `0x9481` Capture,
`0x9484` LiveView y `0x94BA` keep-alive. Detalle completo del desensamblado en
las notas internas de análisis (no se distribuyen).

**Captura**: `0x9481` con parámetro **3** (disparo) → esperar evento `0xC103`
(`CaptureComplete`) → `0x9481` con parámetro **0** (liberar) → esperar
`0xC002`/`0xC004`/`0x4002` con el handle → `0x1008` + `0x1009`. El parámetro 6
NO es el disparo: ambas operaciones responden OK y no se genera ningún fichero.
