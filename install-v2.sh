#!/usr/bin/env bash
set -euo pipefail

# CY-CLI Installer v2 (self-updating)
# Repo: https://github.com/SYMBIOTYC/CY-CLI-releases

REPO="SYMBIOTYC/CY-CLI-releases"
INSTALL_DIR="${CY_INSTALL_DIR:-$HOME/.local/bin}"
CY_STORE_DIR="${CY_STORE_DIR:-$HOME/.local/share/cy}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err() { echo -e "${RED}[ERROR]${NC} $*"; }

detect_os() {
  case "$(uname -s)" in
    Linux*)  echo "linux";;
    Darwin*) echo "macos";;
    *)       err "Unsupported OS: $(uname -s)"; exit 1;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    x86_64)    echo "x86_64";;
    aarch64|arm64) echo "aarch64";;
    *)         err "Unsupported architecture: $(uname -m)"; exit 1;;
  esac
}

target_triple() {
  local os="$1" arch="$2"
  case "$os-$arch" in
    linux-x86_64)    echo "x86_64-unknown-linux-gnu";;
    linux-aarch64)   echo "aarch64-unknown-linux-gnu";;
    macos-x86_64)    echo "x86_64-apple-darwin";;
    macos-aarch64)   echo "aarch64-apple-darwin";;
    *)               err "Unsupported target: $os-$arch"; exit 1;;
  esac
}

download() {
  local url="$1" output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --progress-bar -o "$output" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$output" "$url"
  else
    err "Neither curl nor wget found. Please install one of them."
    exit 1
  fi
}

get_latest_tag() {
  curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep '"tag_name"' \
    | sed -E 's/.*"([^"]+)".*/\1/' \
    | head -1
}

install_wrapper() {
  info "Installing self-updating wrapper to ${INSTALL_DIR}/cy"
  mkdir -p "$INSTALL_DIR"
  cat > "$INSTALL_DIR/cy" << 'WRAPPER_EOF'
#!/usr/bin/env bash
set -euo pipefail

# CY-CLI Self-Updating Wrapper (embedded)
CY_REPO="${CY_REPO:-SYMBIOTYC/CY-CLI-releases}"
CY_INSTALL_DIR="${CY_INSTALL_DIR:-${HOME}/.local/share/cy}"
CY_VERSION_FILE="${CY_INSTALL_DIR}/VERSION"
CY_BIN_DIR="${CY_INSTALL_DIR}/bin"

mkdir -p "${CY_BIN_DIR}"

detect_target() {
  case "$(uname -s)" in
    Linux*)
      case "$(uname -m)" in
        aarch64)  echo "aarch64-unknown-linux-gnu" ;;
        *)        echo "x86_64-unknown-linux-gnu" ;;
      esac
      ;;
    Darwin*)
      case "$(uname -m)" in
        arm64)    echo "aarch64-apple-darwin" ;;
        *)        echo "x86_64-apple-darwin" ;;
      esac
      ;;
    *)       echo "x86_64-unknown-linux-gnu" ;;
  esac
}

get_arch() {
  case "$(uname -m)" in
    x86_64)   echo "x86_64" ;;
    aarch64)  echo "aarch64" ;;
    arm64)    echo "aarch64" ;;
    *)        echo "x86_64" ;;
  esac
}

get_os() {
  case "$(uname -s)" in
    Linux*)  echo "linux" ;;
    Darwin*) echo "macos" ;;
    *)       echo "linux" ;;
  esac
}

get_latest_tag() {
  curl -fsSL "https://api.github.com/repos/${CY_REPO}/releases/latest" \
    | grep '"tag_name"' \
    | sed -E 's/.*"([^"]+)".*/\1/' \
    | head -1
}

get_local_version() {
  if [ -f "${CY_VERSION_FILE}" ]; then
    cat "${CY_VERSION_FILE}"
  else
    echo ""
  fi
}

download_and_install() {
  local tag="$1"
  local target="$2"
  local arch="$3"
  local os="$4"
  local tmpdir
  tmpdir=$(mktemp -d)
  trap "rm -rf ${tmpdir}" EXIT

  local asset_name="cy-${target}"
  if [ "${os}" = "linux" ] || [ "${os}" = "macos" ]; then
    asset_name="cy-${target}.tar.gz"
  else
    asset_name="cy-${target}.zip"
  fi

  local url="https://github.com/${CY_REPO}/releases/download/${tag}/${asset_name}"
  local output="${tmpdir}/${asset_name}"

  echo "[cy-wrapper] Downloading ${asset_name}..." >&2
  curl -fL --progress-bar -o "${output}" "${url}"

  if [ "${os}" = "linux" ] || [ "${os}" = "macos" ]; then
    tar xzf "${output}" -C "${tmpdir}"
    cp "${tmpdir}/cy" "${CY_BIN_DIR}/cy"
    chmod +x "${CY_BIN_DIR}/cy"
  else
    unzip -q "${output}" -d "${tmpdir}"
    cp "${tmpdir}/cy.exe" "${CY_BIN_DIR}/cy.exe"
  fi

  echo "${tag#v}" > "${CY_VERSION_FILE}"
  echo "[cy-wrapper] Installed ${tag}" >&2
}

update_if_needed() {
  local target
  target=$(detect_target)
  local arch
  arch=$(get_arch)
  local os
  os=$(get_os)

  local latest_tag
  latest_tag=$(get_latest_tag) || {
    echo "[cy-wrapper] Failed to fetch latest release, using cached version" >&2
    return 0
  }

  if [ -z "${latest_tag}" ]; then
    echo "[cy-wrapper] No releases found" >&2
    return 0
  fi

  local local_version
  local_version=$(get_local_version)

  if [ "${local_version}" != "${latest_tag#v}" ]; then
    echo "[cy-wrapper] Update available: ${local_version:-none} -> ${latest_tag}" >&2
    download_and_install "${latest_tag}" "${target}" "${arch}" "${os}"
  fi
}

find_fallback_cy() {
  if [ -x "${CY_BIN_DIR}/cy" ]; then
    echo "${CY_BIN_DIR}/cy"
    return 0
  fi
  
  local fallback
  fallback=$(command -v cy 2>/dev/null || true)
  if [ -n "$fallback" ] && [ "$fallback" != "$0" ]; then
    if file "$fallback" 2>/dev/null | grep -q "ELF|Mach-O"; then
      echo "$fallback"
      return 0
    fi
  fi
  
  return 1
}

main() {
  update_if_needed

  local cy_binary
  cy_binary=$(find_fallback_cy) || {
    echo "[cy-wrapper] No cy binary found. Please run install script first." >&2
    exit 1
  }

  exec "$cy_binary" "$@"
}

main "$@"
WRAPPER_EOF
  chmod +x "$INSTALL_DIR/cy"
}

install_initial_binary() {
  local os="$1" arch="$2" triple="$3"
  local tag="$4"
  local asset_file="cy-${triple}.tar.gz"
  
  local base_url="https://github.com/${REPO}/releases/download/${tag}"
  local asset_url="$base_url/$asset_file"
  
  local tmpdir
  tmpdir=$(mktemp -d)
  trap "rm -rf $tmpdir" EXIT
  
  info "Downloading initial binary ${tag}..."
  download "$asset_url" "$tmpdir/$asset_file"
  
  info "Extracting to ${CY_STORE_DIR}/bin..."
  mkdir -p "${CY_STORE_DIR}/bin"
  tar xzf "$tmpdir/$asset_file" -C "$tmpdir"
  cp "$tmpdir/cy" "${CY_STORE_DIR}/bin/cy"
  chmod +x "${CY_STORE_DIR}/bin/cy"
  
  echo "${tag#v}" > "${CY_STORE_DIR}/VERSION"
}

main() {
  info "CY-CLI Installer v2 (self-updating)"
  
  local os arch triple tag
  
  os=$(detect_os)
  arch=$(detect_arch)
  triple=$(target_triple "$os" "$arch")
  
  if [ -n "${CY_VERSION:-}" ]; then
    tag="v${CY_VERSION#v}"
  else
    tag=$(get_latest_tag)
  fi
  
  if [ -z "$tag" ]; then
    err "Could not determine release tag. Is the repo public yet?"
    exit 1
  fi
  
  info "Target: $os/$arch ($triple)"
  info "Version: $tag"
  
  install_wrapper
  install_initial_binary "$os" "$arch" "$triple" "$tag"
  
  info "Installed cy to ${INSTALL_DIR}/cy"
  info "Binary stored at ${CY_STORE_DIR}/bin/cy"
  
  if ! echo "$PATH" | grep -q "$INSTALL_DIR"; then
    warn "$INSTALL_DIR is not in your PATH."
    warn "Add it: export PATH=\"$INSTALL_DIR:\$PATH\""
  fi
  
  info "Verifying..."
  "$INSTALL_DIR/cy" --version || true
  
  info "Installation complete! cy will auto-update on each launch."
}

main "$@"
