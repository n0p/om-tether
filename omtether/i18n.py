"""Catálogo de textos de la interfaz gráfica (español de España / inglés).

Sin dependencias: dos diccionarios planos, ``ES`` y ``EN``, con las mismas
claves, y una función ``tr(key, **kwargs)`` que devuelve el texto en el idioma
activo. Los parámetros se rellenan con ``str.format(**kwargs)``, así que las
cadenas pueden llevar especificadores (``{rc:#06x}``, ``{host:<15}``…).

Reglas de ``tr``:

  * si la clave no está en el idioma activo, se usa la del inglés;
  * si tampoco está en inglés, se devuelve la propia clave;
  * si el formateo falla (falta un parámetro), se devuelve la plantilla tal
    cual. Nunca lanza una excepción: un texto mal es mejor que una ventana rota.

Cómo añadir un idioma
---------------------

1. Copia el dict ``EN`` entero con otro nombre (``FR = {...}``) y traduce los
   valores. Las claves no se tocan.
2. Regístralo en ``CATALOGS`` (``"fr": FR``) y en ``LANGUAGES`` con el nombre
   que debe salir en el selector de la barra superior (``("fr", "Français")``).
3. Si quieres que se detecte solo por ``LANG``/``LC_ALL``, amplía
   ``detect_default_language``.

Las claves siguen el patrón ``grupo.nombre``: ``app.*`` ventana principal,
``tab.*`` pestañas, ``btn.*`` botones, ``label.*`` etiquetas comunes,
``state.*`` estados cortos, ``tether.*``/``remote.*``/``astro.*`` cada pestaña,
``probe.*`` barra de cámara, ``scan.*`` diálogo de búsqueda, ``onboard.*`` y
``usb.*`` el emparejamiento, ``error.*`` mensajes de error de la propia GUI.

Los mensajes que escribe la librería por ``logging`` (``omtether.*``) no pasan
por aquí: siguen en español.
"""

from __future__ import annotations

import os

# --------------------------------------------------------------------------
# Español de España
# --------------------------------------------------------------------------
ES: dict[str, str] = {
    # ventana principal y barra superior
    "app.title": "OM System — tethering de la OM-1",
    "label.camera": "Cámara:",
    "label.camera_placeholder": "IP de la cámara (192.168.0.10 si usas su AP)",
    "label.language": "Idioma:",
    "btn.probe": "Probar",
    "btn.scan": "Buscar cámara…",
    "btn.onboard": "Emparejar (USB)…",
    "probe.unchecked": "sin comprobar",
    "probe.missing_ip": "falta la IP",
    "probe.checking": "comprobando…",
    "probe.ok": "✔ PTP/IP responde en {host}",
    "probe.fail": "✘ {host} no responde",

    # pestañas
    "tab.tether": "Tether (volcado)",
    "tab.remote": "Control remoto",
    "tab.astro": "Astrofotografía",

    # etiquetas y estados comunes
    "label.directory": "Directorio:",
    "label.format": "Formato:",
    "label.log": "Registro:",
    "label.ip": "IP:",
    "label.bulb": "Bulb:",
    "dialog.choose_dir": "Directorio de destino",
    "fmt.jpeg_raw": "JPEG + RAW",
    "fmt.jpeg_only": "Sólo JPEG",
    "fmt.raw_only": "Sólo RAW",
    "state.stopped": "parado",
    "state.connecting": "conectando…",
    "state.connected": "conectado",
    "state.disconnected": "desconectado",
    "state.shooting": "disparando…",
    "state.bulb": "bulb {seconds:.0f} s…",
    "state.error": "error",
    "state.finished": "terminado",
    "log.error": "ERROR: {error}",

    # pestaña Tether
    "tether.group": "Volcado en tiempo real (tether)",
    "tether.bridge": "Puente PTP/IP (darktable, digiKam, gPhoto2)",
    "tether.bridge_port": "Puerto del puente:",
    "tether.state_bridge": "puente en 127.0.0.1:{port}",
    "tether.log_bridge": "puente PTP/IP escuchando en el puerto {port} (también por mDNS)",
    "tether.log_bridge_hint": ("en darktable: sala de tethering; en consola: gphoto2 "
                              "--camera \"PTP/IP Camera\" --port ptpip:127.0.0.1"),
    "tether.log_bridge_stopped": "puente detenido",
    "btn.open_darktable": "Abrir darktable…",
    "tether.log_darktable": "darktable abierto (pid {pid}) con el puente cargado",
    "tether.log_darktable_fail": "no se pudo abrir darktable (¿está instalado?)",
    "label.poll": "Sondeo:",
    "btn.tether_start": "Iniciar volcado",
    "btn.tether_stop": "Detener volcado",
    "tether.state_running": "volcando en {dir}",
    "tether.log_started": "volcado iniciado en {dir}",
    "tether.log_stopped": ("volcado detenido: {downloaded} descargadas, "
                           "{uploaded} subidas, {reconnects} reconexiones"),
    "tether.log_stopping": "parando volcado…",

    # pestaña Control remoto
    "remote.group": "Conexión",
    "btn.connect": "Conectar",
    "btn.disconnect": "Desconectar",
    "btn.shoot": "Disparar",
    "btn.bulb": "Bulb",
    "remote.no_liveview": "sin live view",
    "remote.log_connect_failed": "no se pudo conectar: {error}",
    "remote.log_downloaded": "descargado: {files}",
    "remote.log_shoot_error": "error al disparar: {error}",
    "remote.log_bulb_done": "bulb terminado: {files}",
    "remote.log_bulb_error": "error en bulb: {error}",
    "remote.log_connect_first": "conecta la cámara primero",

    # pestaña Astrofotografía
    "astro.group": "Serie de astrofotografía",
    "astro.shots": "Disparos:",
    "astro.interval": "Intervalo:",
    "astro.iso": "ISO:",
    "astro.exposure": "Exposición:",
    "astro.iso_untouched": "no tocar",
    "astro.exposure_camera": "la de la cámara",
    "astro.indi_group": "INDI",
    "astro.indi_enable": "Activar servicio INDI (KStars/Ekos)",
    "astro.indi_port_hint": "puerto (KStars se conecta a localhost)",
    "btn.astro_start": "Iniciar serie",
    "btn.stop": "Detener",
    "astro.state_indi": "INDI en el puerto {port}",
    "astro.log_indi_listening": ("servicio INDI escuchando en el puerto {port} "
                                 "(KStars: conexión a localhost)"),
    "astro.state_running": "serie en marcha",
    "astro.log_done": "serie terminada: {shots} disparos, {files} ficheros",
    "astro.log_indi_on": "INDI activado: la serie la manda el cliente",
    "astro.log_indi_off": "INDI desactivado",
    "astro.log_start": "serie: {shots} disparos cada {interval:.1f} s en {dir}",
    "error.not_connected": "cámara no conectada",
    "error.no_jpeg": "la cámara no devolvió JPEG",

    # diálogo de búsqueda en red
    "scan.title": "Buscar cámara en la red",
    "scan.intro": ("Se prueba mDNS (_ptp._tcp), SSDP y un barrido del puerto 15740 "
                   "en las redes del equipo."),
    "scan.hint_start": "Pulsa «Buscar» para empezar.",
    "scan.hint_choose": "elige una y pulsa «Usar esta IP»",
    "scan.ip_placeholder": "IP de la cámara (p. ej. 192.168.0.10, el AP de la cámara)",
    "btn.search": "Buscar",
    "btn.use_ip": "Usar esta IP",
    "btn.close": "Cerrar",
    "scan.searching": "buscando por mDNS, SSDP y puerto 15740…",
    "scan.failed": "la búsqueda falló: {error}",
    "scan.none_found": "no se encontró ninguna cámara",
    "scan.row": "{host:<15}  puerto {port:<5}  {mode:<6}  {detail}",

    # emparejamiento por USB (onboarding)
    "onboard.title": "Emparejar la cámara (onboarding)",
    "onboard.help": (
        "1. Conecta la cámara al ordenador por USB.\n"
        "2. En la pantalla de la cámara elige «Camera Control» (control desde el ordenador).\n"
        "3. Pulsa «Leer ranuras»: verás qué equipos recuerda.\n"
        "4. Elige una ranura libre y pulsa «Emparejar este equipo».\n"
        "5. Desconecta el USB y conecta la cámara a la misma Wi-Fi que el ordenador."
    ),
    "onboard.name": "Nombre del equipo:",
    "onboard.slot": "Ranura:",
    "onboard.macs": "MACs (una por línea):",
    "onboard.slot_n": "ranura {slot}",
    "btn.read_slots": "Leer ranuras",
    "btn.pair": "Emparejar este equipo",
    "btn.clear_slot": "Borrar ranura",
    "onboard.need_mac": "hace falta al menos una MAC",
    "onboard.slots_header": "— ranuras —",
    "onboard.slot_empty": "  ranura {slot}: vacía",
    "onboard.slot_error": "  ranura {slot}: error {error}",
    "onboard.slot_entry": "  ranura {slot}: {name}  {macs}",
    "usb.none": "no hay ninguna cámara OM por USB",
    "usb.session_warning": "  aviso: la sesión no se abrió limpiamente",
    "usb.set_result": "SetConnectPcInfo(ranura {slot}) -> {rc:#06x}",
    "usb.rejected": "la cámara rechazó el emparejamiento ({rc:#06x})",
    "usb.reread": "releído: {info}",
    "usb.paired": "emparejado en la ranura {slot}",
    "usb.clear_result": "ClearConnectPcInfo(ranura {slot}) -> {rc:#06x}",
    "usb.slots_read": "ranuras leídas",
}

# --------------------------------------------------------------------------
# English
# --------------------------------------------------------------------------
EN: dict[str, str] = {
    # main window and top bar
    "app.title": "OM System — OM-1 tethering",
    "label.camera": "Camera:",
    "label.camera_placeholder": "camera IP (192.168.0.10 with its own AP)",
    "label.language": "Language:",
    "btn.probe": "Probe",
    "btn.scan": "Find camera…",
    "btn.onboard": "Pair (USB)…",
    "probe.unchecked": "not checked",
    "probe.missing_ip": "IP address missing",
    "probe.checking": "checking…",
    "probe.ok": "✔ PTP/IP answers at {host}",
    "probe.fail": "✘ {host} does not answer",

    # tabs
    "tab.tether": "Tether (auto-download)",
    "tab.remote": "Remote control",
    "tab.astro": "Astrophotography",

    # common labels and states
    "label.directory": "Folder:",
    "label.format": "Format:",
    "label.log": "Log:",
    "label.ip": "IP:",
    "label.bulb": "Bulb:",
    "dialog.choose_dir": "Destination folder",
    "fmt.jpeg_raw": "JPEG + RAW",
    "fmt.jpeg_only": "JPEG only",
    "fmt.raw_only": "RAW only",
    "state.stopped": "stopped",
    "state.connecting": "connecting…",
    "state.connected": "connected",
    "state.disconnected": "disconnected",
    "state.shooting": "shooting…",
    "state.bulb": "bulb {seconds:.0f} s…",
    "state.error": "error",
    "state.finished": "finished",
    "log.error": "ERROR: {error}",

    # Tether tab
    "tether.group": "Real-time download (tether)",
    "tether.bridge": "PTP/IP bridge (darktable, digiKam, gPhoto2)",
    "tether.bridge_port": "Bridge port:",
    "tether.state_bridge": "bridge on 127.0.0.1:{port}",
    "tether.log_bridge": "PTP/IP bridge listening on port {port} (also over mDNS)",
    "tether.log_bridge_hint": ("darktable: tethering view; console: gphoto2 "
                              "--camera \"PTP/IP Camera\" --port ptpip:127.0.0.1"),
    "tether.log_bridge_stopped": "bridge stopped",
    "btn.open_darktable": "Open darktable…",
    "tether.log_darktable": "darktable started (pid {pid}) with the bridge loaded",
    "tether.log_darktable_fail": "could not start darktable (is it installed?)",
    "label.poll": "Polling:",
    "btn.tether_start": "Start tethering",
    "btn.tether_stop": "Stop tethering",
    "tether.state_running": "downloading to {dir}",
    "tether.log_started": "tethering started, saving to {dir}",
    "tether.log_stopped": ("tethering stopped: {downloaded} downloaded, "
                           "{uploaded} uploaded, {reconnects} reconnects"),
    "tether.log_stopping": "stopping tethering…",

    # Remote control tab
    "remote.group": "Connection",
    "btn.connect": "Connect",
    "btn.disconnect": "Disconnect",
    "btn.shoot": "Shoot",
    "btn.bulb": "Bulb",
    "remote.no_liveview": "no live view",
    "remote.log_connect_failed": "could not connect: {error}",
    "remote.log_downloaded": "downloaded: {files}",
    "remote.log_shoot_error": "shooting error: {error}",
    "remote.log_bulb_done": "bulb finished: {files}",
    "remote.log_bulb_error": "bulb error: {error}",
    "remote.log_connect_first": "connect the camera first",

    # Astrophotography tab
    "astro.group": "Astrophotography sequence",
    "astro.shots": "Shots:",
    "astro.interval": "Interval:",
    "astro.iso": "ISO:",
    "astro.exposure": "Exposure:",
    "astro.iso_untouched": "leave as is",
    "astro.exposure_camera": "camera setting",
    "astro.indi_group": "INDI",
    "astro.indi_enable": "Enable INDI service (KStars/Ekos)",
    "astro.indi_port_hint": "port (KStars connects to localhost)",
    "btn.astro_start": "Start sequence",
    "btn.stop": "Stop",
    "astro.state_indi": "INDI on port {port}",
    "astro.log_indi_listening": ("INDI service listening on port {port} "
                                 "(KStars: connect to localhost)"),
    "astro.state_running": "sequence running",
    "astro.log_done": "sequence finished: {shots} shots, {files} files",
    "astro.log_indi_on": "INDI enabled: the client drives the sequence",
    "astro.log_indi_off": "INDI disabled",
    "astro.log_start": "sequence: {shots} shots every {interval:.1f} s, saving to {dir}",
    "error.not_connected": "camera not connected",
    "error.no_jpeg": "the camera returned no JPEG",

    # network scan dialog
    "scan.title": "Find the camera on the network",
    "scan.intro": ("Tries mDNS (_ptp._tcp), SSDP and a sweep of port 15740 "
                   "on this computer's networks."),
    "scan.hint_start": "Press “Search” to start.",
    "scan.hint_choose": "pick one and press “Use this IP”",
    "scan.ip_placeholder": "camera IP (e.g. 192.168.0.10, the camera's own AP)",
    "btn.search": "Search",
    "btn.use_ip": "Use this IP",
    "btn.close": "Close",
    "scan.searching": "searching via mDNS, SSDP and port 15740…",
    "scan.failed": "search failed: {error}",
    "scan.none_found": "no camera found",
    "scan.row": "{host:<15}  port {port:<5}  {mode:<6}  {detail}",

    # USB pairing (onboarding)
    "onboard.title": "Pair the camera (onboarding)",
    "onboard.help": (
        "1. Connect the camera to the computer over USB.\n"
        "2. On the camera screen choose “Camera Control” (control from the computer).\n"
        "3. Press “Read slots”: you will see which computers it remembers.\n"
        "4. Pick a free slot and press “Pair this computer”.\n"
        "5. Unplug the USB cable and join the camera to the same Wi-Fi as the computer."
    ),
    "onboard.name": "Computer name:",
    "onboard.slot": "Slot:",
    "onboard.macs": "MACs (one per line):",
    "onboard.slot_n": "slot {slot}",
    "btn.read_slots": "Read slots",
    "btn.pair": "Pair this computer",
    "btn.clear_slot": "Clear slot",
    "onboard.need_mac": "at least one MAC is required",
    "onboard.slots_header": "— slots —",
    "onboard.slot_empty": "  slot {slot}: empty",
    "onboard.slot_error": "  slot {slot}: error {error}",
    "onboard.slot_entry": "  slot {slot}: {name}  {macs}",
    "usb.none": "no OM camera found over USB",
    "usb.session_warning": "  warning: the session did not open cleanly",
    "usb.set_result": "SetConnectPcInfo(slot {slot}) -> {rc:#06x}",
    "usb.rejected": "the camera rejected the pairing ({rc:#06x})",
    "usb.reread": "read back: {info}",
    "usb.paired": "paired in slot {slot}",
    "usb.clear_result": "ClearConnectPcInfo(slot {slot}) -> {rc:#06x}",
    "usb.slots_read": "slots read",
}

# idioma -> catálogo
CATALOGS: dict[str, dict[str, str]] = {"es": ES, "en": EN}
# (código, nombre tal como sale en el selector; el nombre no se traduce)
LANGUAGES: list[tuple[str, str]] = [("es", "Español"), ("en", "English")]
DEFAULT_LANGUAGE = "en"

_current = DEFAULT_LANGUAGE


def set_language(code: str) -> None:
    """Activa un idioma. Un código desconocido deja el inglés."""
    global _current
    code = (code or "").strip().lower()[:2]
    _current = code if code in CATALOGS else DEFAULT_LANGUAGE


def get_language() -> str:
    return _current


def detect_default_language() -> str:
    """``es`` si LANG/LC_ALL empiezan por ``es``; si no, ``en``."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value:
            return "es" if value.lower().startswith("es") else "en"
    return "en"


def tr(key: str, **kwargs) -> str:
    """Texto de ``key`` en el idioma activo, con ``kwargs`` aplicados. Nunca lanza."""
    text = CATALOGS[_current].get(key)
    if text is None:
        text = EN.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


_ = tr


def missing_keys() -> dict[str, list[str]]:
    """Claves que faltan en cada catálogo respecto a la unión de todos (para pruebas)."""
    every = set()
    for cat in CATALOGS.values():
        every.update(cat)
    return {code: sorted(every - set(cat)) for code, cat in CATALOGS.items()}
