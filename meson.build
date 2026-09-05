project('cine',
          version: '1.8.5',
    meson_version: '>= 1.0.0',
  default_options: [ 'warning_level=2', 'werror=false', ],
)

i18n = import('i18n')
gnome = import('gnome')

is_windows = host_machine.system() == 'windows'

subdir('data')
subdir('src')
subdir('po')

if is_windows
  # Windows has no desktop-entry or icon-theme databases to refresh, but
  # GSettings still reads a compiled schema - from the registry-backed
  # store rather than dconf.
  gnome.post_install(
    glib_compile_schemas: true,
  )
else
  gnome.post_install(
       glib_compile_schemas: true,
      gtk_update_icon_cache: true,
    update_desktop_database: true,
  )
endif
