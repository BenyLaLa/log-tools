# Build two executables in one shared directory. No user config/data is collected.
from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

if os.name == 'nt':
    # Do not accidentally collect incompatible DLLs from unrelated applications
    # on PATH (for example Poppler's ICU instead of Windows' native ICU).
    windows = Path(os.environ['WINDIR'])
    os.environ['PATH'] = os.pathsep.join([str(Path(sys.executable).parent),
                                        str(windows / 'System32'), str(windows)])

root = Path(SPECPATH).parent
source = str(root / 'src')
shared = collect_data_files('mcp') + collect_data_files('jsonschema_specifications')
shared += [(str(root / 'build/desktop-licenses'), 'licenses'),
           (str(root / 'docs/desktop.md'), 'docs')]
hidden = collect_submodules('mcp', filter=lambda name: not name.startswith('mcp.cli'))
gui = Analysis([str(root / 'src/log_tools/desktop_entry.py')], pathex=[source],
               datas=shared, hiddenimports=hidden,
               excludes=['mcp.cli', 'pytest', 'ruff', 'pyftpdlib', 'PySide6.QtQml', 'PySide6.QtQuick'])
server = Analysis([str(root / 'src/log_tools/mcp_entry.py')], pathex=[source],
                  datas=shared, hiddenimports=hidden,
                  excludes=['mcp.cli', 'PySide6', 'pytest', 'ruff', 'pyftpdlib'])

trusted_roots = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
if os.name == 'nt':
    trusted_roots.append(windows.resolve())
for analysis in (gui, server):
    for destination, origin, kind in analysis.binaries:
        if kind in ('BINARY', 'EXTENSION') and not any(
                Path(origin).resolve().is_relative_to(base) for base in trusted_roots):
            raise RuntimeError(f'Unexpected native dependency outside the build runtime: {origin}')
gui_pyz = PYZ(gui.pure)
server_pyz = PYZ(server.pure)
gui_exe = EXE(gui_pyz, gui.scripts, [], exclude_binaries=True, name='LogTools',
              console=False, disable_windowed_traceback=False, upx=False)
server_exe = EXE(server_pyz, server.scripts, [], exclude_binaries=True,
                 name='log-tools-mcp', console=True, upx=False)
collection = COLLECT(gui_exe, server_exe, gui.binaries, gui.datas, server.binaries,
                     server.datas, strip=False, upx=False, name='LogTools')
