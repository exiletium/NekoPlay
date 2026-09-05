/* nekoplay-launcher.c
 *
 * Copyright 2026 Diego Povliuk
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

/* The Windows entry point.
 *
 * GTK, mpv and Python all live inside the application folder rather than
 * anywhere the system knows to look for them, so the whole job here is to
 * point each one at the bundle and then hand over to the interpreter.
 * Every path is derived from where this executable happens to be, which
 * is what lets the folder be moved, renamed or run off a USB stick.
 *
 * Note that Python is loaded by hand rather than linked against. An
 * import in this binary would be resolved by the loader before any of
 * this runs, and it only searches beside the executable - which would
 * force all 138 bundled DLLs to sit in the top folder next to the icon
 * the user actually clicks.
 */

#include <windows.h>
#include <shellapi.h>
#include <stdlib.h>
#include <wchar.h>

#define ENV_MAX 32768

typedef int(__cdecl *py_main_fn)(int argc, wchar_t **argv);

/* Folder holding this executable, with a trailing backslash. */
static wchar_t g_root[MAX_PATH];

static void fail(const wchar_t *message)
{
    MessageBoxW(NULL, message, L"NekoPlay", MB_ICONERROR | MB_OK);
}

/* _wputenv_s rather than SetEnvironmentVariableW: the latter only touches
 * the Win32 environment block, while the C runtime hands Python a copy of
 * its own made at startup. Writing through the CRT updates both, so GLib
 * and os.environ agree on what these are. */
static void set_env(const wchar_t *name, const wchar_t *value)
{
    _wputenv_s(name, value);
}

static void set_rooted(const wchar_t *name, const wchar_t *suffix)
{
    wchar_t value[MAX_PATH * 2];

    swprintf(value, ARRAYSIZE(value), L"%ls%ls", g_root, suffix);
    set_env(name, value);
}

/* Put a bundle folder ahead of whatever the machine already has, so a
 * stray copy of one of these DLLs elsewhere on PATH cannot win. */
static void prepend_to_path(const wchar_t *suffix)
{
    wchar_t *value = calloc(ENV_MAX, sizeof(wchar_t));
    const wchar_t *existing;
    int used;

    if (!value)
        return;

    used = swprintf(value, ENV_MAX, L"%ls%ls", g_root, suffix);

    existing = _wgetenv(L"PATH");
    if (used > 0 && existing && *existing)
        swprintf(value + used, ENV_MAX - used, L";%ls", existing);

    set_env(L"PATH", value);
    free(value);
}

static void find_root(void)
{
    wchar_t *slash;

    GetModuleFileNameW(NULL, g_root, ARRAYSIZE(g_root));

    slash = wcsrchr(g_root, L'\\');
    if (slash)
        slash[1] = L'\0';
}

/* Whatever libpython the bundle was built against, without this launcher
 * having to be rebuilt for each new Python release. */
static HMODULE load_python(void)
{
    WIN32_FIND_DATAW found;
    wchar_t pattern[MAX_PATH * 2];
    wchar_t path[MAX_PATH * 2];
    HANDLE search;

    swprintf(pattern, ARRAYSIZE(pattern), L"%lsbin\\libpython3.*.dll", g_root);

    search = FindFirstFileW(pattern, &found);
    if (search == INVALID_HANDLE_VALUE)
        return NULL;

    do {
        /* libpython3.dll is the stable-ABI stub and has no Py_Main. */
        if (wcscmp(found.cFileName, L"libpython3.dll") == 0)
            continue;

        swprintf(path, ARRAYSIZE(path), L"%lsbin\\%ls", g_root,
                 found.cFileName);
        FindClose(search);

        return LoadLibraryW(path);
    } while (FindNextFileW(search, &found));

    FindClose(search);
    return NULL;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR cmdline,
                    int show)
{
    LPWSTR *wargv;
    wchar_t **argv;
    wchar_t script[MAX_PATH * 2];
    wchar_t bindir[MAX_PATH * 2];
    HMODULE python;
    py_main_fn py_main;
    int argc = 0;
    int i;
    int status;

    (void)instance;
    (void)previous;
    (void)cmdline;
    (void)show;

    find_root();

    /* libmpv, the GTK stack and libpython all resolve through this. */
    prepend_to_path(L"bin");
    swprintf(bindir, ARRAYSIZE(bindir), L"%lsbin", g_root);
    SetDllDirectoryW(bindir);

    set_rooted(L"PYTHONHOME", L"");
    set_rooted(L"GI_TYPELIB_PATH", L"lib\\girepository-1.0");
    set_rooted(L"GSETTINGS_SCHEMA_DIR", L"share\\glib-2.0\\schemas");
    set_rooted(L"XDG_DATA_DIRS", L"share");
    set_rooted(L"GDK_PIXBUF_MODULE_FILE",
               L"lib\\gdk-pixbuf-2.0\\2.10.0\\loaders.cache");
    set_rooted(L"FONTCONFIG_PATH", L"etc\\fonts");

    /* The bundle may sit somewhere unwritable such as Program Files, and
     * everything is byte-compiled at packaging time anyway. */
    set_env(L"PYTHONDONTWRITEBYTECODE", L"1");

    /* mpv parses its own option values with the C locale and says so
     * loudly if it is handed anything else. */
    set_env(L"LC_NUMERIC", L"C");

    python = load_python();
    if (!python) {
        fail(L"Could not load the bundled Python runtime.\n\n"
             L"bin\\libpython3.*.dll is missing or unreadable. "
             L"Unpack the whole NekoPlay folder and try again.");
        return 1;
    }

    py_main = (py_main_fn)(void *)GetProcAddress(python, "Py_Main");
    if (!py_main) {
        fail(L"The bundled Python runtime is missing its entry point.");
        return 1;
    }

    swprintf(script, ARRAYSIZE(script), L"%lsshare\\cine\\nekoplay.py",
             g_root);

    wargv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!wargv || argc < 1) {
        static wchar_t *fallback[] = { L"nekoplay" };
        wargv = fallback;
        argc = 1;
    }

    /* Splice the script in as argv[1]; anything the shell passed us, such
     * as a video dragged onto the icon, follows it untouched. */
    argv = calloc(argc + 2, sizeof(wchar_t *));
    if (!argv)
        return 1;

    argv[0] = wargv[0];
    argv[1] = script;
    for (i = 1; i < argc; i++)
        argv[i + 1] = wargv[i];

    status = py_main(argc + 1, argv);

    free(argv);
    return status;
}
