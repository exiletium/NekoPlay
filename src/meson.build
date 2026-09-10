pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir = pkgdatadir / 'cine'
gnome = import('gnome')

blueprints = custom_target('blueprints',
  input: files(
    'history.blp',
    'playlist.blp',
    'preferences.blp',
    'options.blp',
    'shortcuts-dialog.blp',
    'window.blp',
  ),
  output: '.',
  command: [find_program('blueprint-compiler'), 'batch-compile', '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@'],
)

gnome.compile_resources('cine',
  'cine.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: blueprints,
)

python = import('python')
py_installation = python.find_installation('python3')

conf = configuration_data()
conf.set('PYTHON', py_installation.full_path())
conf.set('VERSION', meson.project_version())
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)

if is_windows
  # A shebang means nothing here, so the entry point ships as an ordinary
  # module beside the rest of the app and the launcher below runs it.
  configure_file(
    input: 'nekoplay.in',
    output: 'nekoplay.py',
    configuration: conf,
    install: true,
    install_dir: pkgdatadir,
  )

  add_languages('c', native: false, required: true)
  cc = meson.get_compiler('c')

  windows_mod = import('windows')
  launcher_rc = windows_mod.compile_resources(
    'nekoplay.rc',
    depend_files: files('nekoplay.ico'),
  )

  # win_subsystem 'windows' keeps a console from appearing behind the video;
  # -municode picks the startup code that calls wWinMain rather than
  # WinMain, so paths reach us as UTF-16 and non-ASCII filenames survive.
  #
  # There is deliberately no link against libpython: the launcher loads it
  # by hand so that the bundled DLLs can live in bin/ instead of being
  # strewn beside the icon the user clicks. See nekoplay-launcher.c.
  executable('nekoplay',
    'nekoplay-launcher.c',
    launcher_rc,
    dependencies: [cc.find_library('shell32')],
    c_args: ['-municode', '-DUNICODE', '-D_UNICODE'],
    link_args: ['-municode'],
    win_subsystem: 'windows',
    install: true,
  )
else
  configure_file(
    input: 'nekoplay.in',
    output: 'nekoplay',
    configuration: conf,
    install: true,
    install_dir: get_option('bindir'),
    install_mode: 'rwxr-xr-x'
  )
endif

cine_sources = [
  'shaders/flip-h.glsl',
  'shaders/flip-v.glsl',
  '__init__.py',
  'anime4k.py',
  'history.py',
  'main.py',
  'mpv_gl_area.py',
  'mpris.py',
  'options.py',
  'platform_compat.py',
  'playlist.py',
  'preferences.py',
  'probe.py',
  'save_session.py',
  'shortcuts.py',
  'utils.py',
  'window.py',
]

install_data(cine_sources, install_dir: moduledir)
