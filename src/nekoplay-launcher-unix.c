/* nekoplay-launcher-unix.c
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

/* The entry point everywhere but Windows.
 *
 * While NekoPlay runs, in the background or with a window open, a launch
 * only has to hand its files to it. Done from Python that waited ~12 ms
 * for the interpreter to start before the running instance heard a thing;
 * done from here it is a few, so the window is up sooner by the same.
 * It is the same D-Bus call GApplication makes for a remote launch: files
 * become an Open, a bare --new-window an Activate.
 *
 * Anything else - nothing running, or a launch that has to be the app
 * itself - runs the app's script with Python, as this file used to be.
 */

#include <errno.h>
#include <gio/gio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "launcher-config.h"

#define APP_ID "moe.nyarchlinux.nekoplay"
#define OBJECT_PATH "/moe/nyarchlinux/nekoplay"

/* Whether *arg* starts with a URI scheme, as GLib decides when it turns a
 * command-line argument into a file. */
static gboolean
has_uri_scheme (const char *arg)
{
  const char *p = arg;

  if (!g_ascii_isalpha (*p))
    return FALSE;
  while (g_ascii_isalnum (*p) || *p == '+' || *p == '.' || *p == '-')
    p++;
  return *p == ':';
}

static void
add_platform_data (GVariantBuilder *builder, const char *key, const char *env)
{
  const char *value = g_getenv (env);

  /* Lets the running instance raise its window over ours, on Wayland and
   * on X11; GApplication passes these on the same way. */
  if (value != NULL && *value != '\0' && g_utf8_validate (value, -1, NULL))
    g_variant_builder_add (builder, "{sv}", key, g_variant_new_string (value));
}

/* Hand the launch to a running instance. TRUE if it was handed over;
 * FALSE when the launch is not one to hand over - it has an option this
 * does not know, or neither files nor --new-window, which GApplication
 * answers itself - or when nothing is running. */
static gboolean
forward (int argc, char **argv)
{
  g_autoptr (GPtrArray) uris = g_ptr_array_new_with_free_func (g_free);
  g_autoptr (GDBusConnection) bus = NULL;
  g_autoptr (GVariant) reply = NULL;
  GVariantBuilder platform_data;
  GVariant *parameters;
  gboolean new_window = FALSE;
  gboolean has_files;

  for (int i = 1; i < argc; i++)
    {
      const char *arg = argv[i];

      if (strcmp (arg, "-n") == 0 || strcmp (arg, "--new-window") == 0)
        new_window = TRUE;
      else if (*arg == '\0' || *arg == '-')
        return FALSE;
      else if (has_uri_scheme (arg))
        {
          if (!g_utf8_validate (arg, -1, NULL))
            return FALSE;
          g_ptr_array_add (uris, g_strdup (arg));
        }
      else
        {
          g_autofree char *path = g_canonicalize_filename (arg, NULL);
          char *uri = g_filename_to_uri (path, NULL, NULL);

          if (uri == NULL)
            return FALSE;
          g_ptr_array_add (uris, uri);
        }
    }
  if (!new_window)
    return FALSE;

  bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, NULL);
  if (bus == NULL)
    return FALSE;

  g_variant_builder_init (&platform_data, G_VARIANT_TYPE_VARDICT);
  add_platform_data (&platform_data, "activation-token", "XDG_ACTIVATION_TOKEN");
  add_platform_data (&platform_data, "desktop-startup-id", "DESKTOP_STARTUP_ID");

  has_files = uris->len > 0;
  if (has_files)
    {
      g_ptr_array_add (uris, NULL);
      parameters = g_variant_new ("(^as@a{sv})", (const char *const *) uris->pdata,
                                  g_variant_builder_end (&platform_data));
    }
  else
    parameters = g_variant_new ("(@a{sv})", g_variant_builder_end (&platform_data));

  /* Not started for us if it is not running: this launch should then
   * become the instance itself, as it would without this. */
  reply = g_dbus_connection_call_sync (bus, APP_ID, OBJECT_PATH,
                                       "org.freedesktop.Application",
                                       has_files ? "Open" : "Activate",
                                       parameters, NULL,
                                       G_DBUS_CALL_FLAGS_NO_AUTO_START,
                                       -1, NULL, NULL);
  return reply != NULL;
}

int
main (int argc, char **argv)
{
  char **args;

  if (forward (argc, argv))
    return EXIT_SUCCESS;

  /* The script runs as if it were this command, which is what the
   * autostart entry, for one, has to name. */
  g_setenv ("NEKOPLAY_LAUNCHER", LAUNCHER_PATH, TRUE);

  args = g_new0 (char *, argc + 2);
  args[0] = (char *) PYTHON;
  args[1] = (char *) SCRIPT;
  for (int i = 1; i < argc; i++)
    args[i + 1] = argv[i];
  execv (PYTHON, args);

  fprintf (stderr, "nekoplay: could not run %s: %s\n", PYTHON, g_strerror (errno));
  return 127;
}
