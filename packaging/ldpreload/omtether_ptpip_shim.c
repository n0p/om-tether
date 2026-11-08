/* omtether — shim para que libgphoto2 vea el puente por PTP/IP.
 *
 * En Linux el puerto "ptpip:" de libgphoto2 no lleva host: el descubrimiento
 * mDNS (_ptp._tcp) sólo se compila con Bonjour, así que gp_abilities_list_detect
 * no encuentra nada y darktable —que sólo lista lo que devuelve detect— no
 * ofrece ninguna cámara por Wi-Fi.
 *
 * Interceptamos gp_port_info_list_load() y, una vez cargada la lista real,
 * cambiamos la ruta del puerto "ptpip:" por "ptpip:<host>:<puerto>". Con eso
 * detect sí prueba ese puerto y aparece "PTP/IP Camera".
 *
 * Uso:
 *   LD_PRELOAD=/usr/lib/omtether/omtether_ptpip_shim.so darktable
 *   OMTETHER_BRIDGE_HOST=127.0.0.1 OMTETHER_BRIDGE_PORT=15740 ...
 *
 * No se toca ninguna librería del sistema.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <gphoto2/gphoto2-port-info-list.h>

int
gp_port_info_list_load (GPPortInfoList *list)
{
	static int (*real_load) (GPPortInfoList *) = NULL;
	const char *host, *port;
	char path[256];
	int n;

	if (!real_load) {
		real_load = (int (*) (GPPortInfoList *)) dlsym (RTLD_NEXT, "gp_port_info_list_load");
		if (!real_load)
			return -1;
	}

	int rc = real_load (list);
	if (rc < 0)
		return rc;

	host = getenv ("OMTETHER_BRIDGE_HOST");
	port = getenv ("OMTETHER_BRIDGE_PORT");
	if (!host || !*host)
		host = "127.0.0.1";
	if (!port || !*port)
		port = "15740";
	snprintf (path, sizeof (path), "ptpip:%s:%s", host, port);

	n = gp_port_info_list_count (list);
	for (int i = 0; i < n; i++) {
		GPPortInfo info;
		GPPortType type;
		char *current = NULL;

		if (gp_port_info_list_get_info (list, i, &info) < 0)
			continue;
		gp_port_info_get_type (info, &type);
		gp_port_info_get_path (info, &current);
		if (type == GP_PORT_PTPIP && current && strcmp (current, "ptpip:") == 0) {
			gp_port_info_set_path (info, path);
			fprintf (stderr, "omtether: puerto PTP/IP -> %s\n", path);
		}
	}
	return rc;
}
