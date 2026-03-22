#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/build_macos_local.sh [--version VERSION] [--build-number NUMBER] [--skip-sign]

Build the macOS UniPaste release package locally.

Options:
  --version VERSION       CFBundleShortVersionString value. Default: local
  --build-number NUMBER   CFBundleVersion value. Default: 1
  --skip-sign             Skip ad-hoc codesign
  -h, --help              Show this help message

Prerequisites:
  - Run on macOS
  - Install dependencies first:
      python3 -m pip install -r requirements.txt pyinstaller
EOF
}

VERSION="local"
BUILD_NUMBER="1"
SKIP_SIGN="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="${2:-}"
      shift 2
      ;;
    --build-number)
      BUILD_NUMBER="${2:-}"
      shift 2
      ;;
    --skip-sign)
      SKIP_SIGN="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must be run on macOS." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found in PATH." >&2
  exit 1
fi

if ! python3 - <<'PY' >/dev/null 2>&1
import importlib.util
import sys
sys.exit(0 if importlib.util.find_spec("PyInstaller") else 1)
PY
then
  echo "PyInstaller is not installed for python3. Run: python3 -m pip install -r requirements.txt pyinstaller" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

SPEC_PATH="$REPO_ROOT/unipaste.spec"
trap 'rm -f "$SPEC_PATH"' EXIT

mkdir -p assets dist build package release

cat > "$SPEC_PATH" <<PYI
# -*- mode: python -*-
block_cipher = None

a = Analysis(
    ['mac_clip_check.py'],
    pathex=[],
    binaries=[],
    datas=[('LICENSE', '.'), ('utils', 'utils'), ('handlers', 'handlers'), ('config.py', '.'), ('assets', 'assets')],
    hiddenimports=[
        'AppKit',
        'tkinter',
        'tkinter.ttk',
        'websockets',
        'cryptography',
        'pyperclip',
        'zeroconf',
        'netifaces',
        'rumps',
        'utils.security.crypto',
        'utils.security.auth',
        'utils.security.pairing',
        'utils.network.discovery',
        'utils.message_format',
        'utils.platform_config',
        'utils.clipboard_utils',
        'utils.control_panel',
        'utils.service_host',
        'utils.autostart',
        'utils.windows_tray',
        'handlers.file_handler',
        'config',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='UniPaste-Mac',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
app = BUNDLE(
    exe,
    name='UniPaste-Mac.app',
    bundle_identifier='com.kookiejarz.unipaste',
    info_plist={
        'CFBundleName': 'UniPaste',
        'CFBundleDisplayName': 'UniPaste',
        'CFBundleShortVersionString': '${VERSION}',
        'CFBundleVersion': '${BUILD_NUMBER}',
        'LSUIElement': True,
        'NSLocalNetworkUsageDescription': 'UniPaste needs local network access to discover and connect to nearby devices.',
        'NSBonjourServices': ['_clipshare._tcp'],
    },
)
PYI

echo "Building UniPaste-Mac.app..."
python3 -m PyInstaller "$SPEC_PATH" --noconfirm

APP_PATH="$REPO_ROOT/dist/UniPaste-Mac.app"
PACKAGE_ROOT="$REPO_ROOT/package/UniPaste-macos"
ARCHIVE_PATH="$REPO_ROOT/release/UniPaste-macos.zip"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Build did not produce $APP_PATH" >&2
  exit 1
fi

if [[ "$SKIP_SIGN" != "1" ]]; then
  echo "Applying ad-hoc codesign..."
  codesign --force --deep --sign - "$APP_PATH"
fi

rm -rf "$PACKAGE_ROOT"
mkdir -p "$PACKAGE_ROOT"
cp -R "$APP_PATH" "$PACKAGE_ROOT/"
cp README.md "$PACKAGE_ROOT/" || true
cp LICENSE "$PACKAGE_ROOT/" || true

rm -f "$ARCHIVE_PATH"
ditto -c -k --sequesterRsrc --keepParent "$PACKAGE_ROOT" "$ARCHIVE_PATH"

echo "Verifying bundle..."
codesign --verify --deep --strict -v "$APP_PATH"

echo
echo "Build complete."
echo "App: $APP_PATH"
echo "Zip: $ARCHIVE_PATH"
echo
echo "Suggested manual checks:"
echo "  open \"$APP_PATH\""
echo "  \"$APP_PATH/Contents/MacOS/UniPaste-Mac\" --headless"
