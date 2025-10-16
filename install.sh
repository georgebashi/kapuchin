#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

KLIPPER_PATH="${HOME}/klipper"
KLIPPER_SERVICE_NAME="klipper"
SYSTEMDDIR="/etc/systemd/system"
MOONRAKER_CONFIG_DIR="${HOME}/printer_data/config"
KLIPPER_VENV_PATH="${KLIPPER_VENV:-${HOME}/klippy-env}"

UNINSTALL=0
FORCE=0

if [ ! -d "${MOONRAKER_CONFIG_DIR}" ]; then
  echo "\"${MOONRAKER_CONFIG_DIR}\" does not exist. Falling back to \"${HOME}/klipper_config\" as default."
  MOONRAKER_CONFIG_DIR="${HOME}/klipper_config"
fi

usage(){ cat <<USAGE
Usage: install-kapuchin.sh [-k <klipper path>] [-s <klipper service name>] [-c <configuration path>] [-e <klipper venv path>] [-u] [-f] [-h]
  -k  Path to Klipper installation (default: ${HOME}/klipper)
  -s  Klipper systemd service name (default: klipper)
  -c  Path to Moonraker configuration directory (default: ${HOME}/printer_data/config or ${HOME}/klipper_config)
  -e  Path to Klipper Python virtualenv (default: ${KLIPPER_VENV:-${HOME}/klippy-env})
  -u  Uninstall (remove Kapuchin links)
  -f  Force relink (overwrite existing links/files where safe)
  -h  Show this help
USAGE
}

while getopts "k:s:c:e:ufh" arg; do
  case $arg in
    k) KLIPPER_PATH=$OPTARG;;
    s) KLIPPER_SERVICE_NAME=$OPTARG;;
    c) MOONRAKER_CONFIG_DIR=$OPTARG;;
    e) KLIPPER_VENV_PATH=$OPTARG;;
    u) UNINSTALL=1;;
    f) FORCE=1;;
    h) usage; exit 0;;
    *) usage; exit 1;;
  esac
done

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJDIR="$SCRIPT_DIR"
EXTRA_FILE="${PROJDIR}/extras/kapuchin.py"
PATCHES_DIR="${PROJDIR}/patches"

err_trap() { echo "[ERROR] $(basename "$0"): line ${1}: ${2}"; exit 1; }
trap 'err_trap ${LINENO} "Command exited with non-zero status."' ERR

command_exists() { command -v "$1" >/dev/null 2>&1; }

verify_ready() {
  if [ "$EUID" -eq 0 ]; then
    echo "[ERROR] This script must not run as root. Exiting."
    exit 1
  fi
  if ! command_exists python3; then
    echo "[ERROR] python3 is required but not found in PATH."
    exit 1
  fi
  if [ ! -f "$EXTRA_FILE" ]; then
    echo "[ERROR] Missing extras file at: $EXTRA_FILE"
    exit 1
  fi
  if [ ! -d "$PATCHES_DIR" ]; then
    echo "[ERROR] Missing patches directory at: $PATCHES_DIR"
    exit 1
  fi
}

check_folders() {
  if [ ! -d "${KLIPPER_PATH}/klippy/extras" ]; then
    echo "[ERROR] Klipper installation not found in directory \"$KLIPPER_PATH\"."
    exit 1
  fi
  echo "Klipper installation found at $KLIPPER_PATH"
  if [ ! -f "${MOONRAKER_CONFIG_DIR}/moonraker.conf" ]; then
    echo "[ERROR] Moonraker configuration not found in \"$MOONRAKER_CONFIG_DIR\"."
    exit 1
  fi
  echo "Moonraker configuration found at $MOONRAKER_CONFIG_DIR"
}

link_extension() {
  local target="${KLIPPER_PATH}/klippy/extras/kapuchin.py"
  echo -n "Linking extras file to Klipper... "
  if [ -e "$target" ] && [ "$FORCE" -ne 1 ] && [ ! -L "$target" ]; then
    echo "[FAILED]"
    echo "[ERROR] $target exists and is not a symlink. Re-run with -f to overwrite."
    exit 1
  fi
  ln -sfn "$EXTRA_FILE" "$target"
  echo "[OK]"
}

link_patches() {
  local target_dir="${KLIPPER_PATH}/klippy/patches"
  echo -n "Linking patches into Klipper... "
  if [ -L "$target_dir" ]; then
    local dest
    dest="$(readlink -f "$target_dir" 2>/dev/null || readlink "$target_dir")"
    if [ "$dest" = "$PATCHES_DIR" ]; then
      echo "[OK] (already linked)"
      return 0
    fi
    if [ "$FORCE" -eq 1 ]; then
      rm -f "$target_dir"
    else
      echo
      echo "[INFO] Existing symlink at $target_dir points to $dest. Will link individual files instead."
    fi
  fi
  if [ ! -e "$target_dir" ] || [ "$FORCE" -eq 1 ]; then
    ln -sfn "$PATCHES_DIR" "$target_dir" && { echo "[OK]"; return 0; } || true
  fi
  [ -d "$target_dir" ] || mkdir -p "$target_dir"
  if [ ! -f "$target_dir/__init__.py" ]; then
    if [ -f "$PATCHES_DIR/__init__.py" ]; then
      ln -sfn "$PATCHES_DIR/__init__.py" "$target_dir/__init__.py" || touch "$target_dir/__init__.py"
    else
      touch "$target_dir/__init__.py"
    fi
  fi
  local linked_any=0
  for f in "$PATCHES_DIR"/*.py; do
    [ -e "$f" ] || continue
    local base
    base="$(basename "$f")"
    if [ -e "$target_dir/$base" ] && [ "$FORCE" -ne 1 ] && [ ! -L "$target_dir/$base" ]; then
      echo
      echo "[WARN] Skipping $base: a non-symlink already exists at target."
      continue
    fi
    ln -sfn "$f" "$target_dir/$base"
    linked_any=1
  done
  if [ "$linked_any" -eq 1 ]; then
    echo "[OK]"
  else
    echo "[FAILED]"
    echo "[ERROR] No patch files were linked."
    exit 1
  fi
}

add_updater() {
  local conf="${MOONRAKER_CONFIG_DIR}/moonraker.conf"
  echo -n "Adding update manager to moonraker.conf... "
  local count
  count=$(grep -c '^\[update_manager kapuchin\]' "$conf" 2>/dev/null || true)
  if [ "${count:-0}" -gt 0 ]; then
    echo "[SKIPPED] ([update_manager kapuchin] exists)"
    return 0
  fi
  local repo_dir branch remote_url
  repo_dir=$(git -C "$PROJDIR" rev-parse --show-toplevel 2>/dev/null || echo "$PROJDIR")
  branch=$(git -C "$repo_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "master")
  remote_url=$(git -C "$repo_dir" remote get-url origin 2>/dev/null || echo "")
  {
    echo ""
    echo "[update_manager kapuchin]"
    echo "type: git_repo"
    echo "path: $repo_dir"
    [ -n "$remote_url" ] && echo "origin: $remote_url"
    echo "primary_branch: $branch"
    echo "virtualenv: $KLIPPER_VENV_PATH"
    echo "managed_services: $KLIPPER_SERVICE_NAME"
    if [ -f "$repo_dir/requirements.txt" ]; then
      echo "requirements: requirements.txt"
    fi
  } >> "$conf"
  echo "[OK]"
}

uninstall() {
  echo "Uninstalling Kapuchin..."
  local extra_target="${KLIPPER_PATH}/klippy/extras/kapuchin.py"
  if [ -e "$extra_target" ]; then
    rm -f "$extra_target" && echo "Removed $extra_target"
  else
    echo "Extras file not found at $extra_target"
  fi
  local patches_target="${KLIPPER_PATH}/klippy/patches"
  if [ -L "$patches_target" ]; then
    local dest
    dest="$(readlink -f "$patches_target" 2>/dev/null || readlink "$patches_target")"
    if [ "$dest" = "$PATCHES_DIR" ]; then
      rm -f "$patches_target" && echo "Removed symlink $patches_target"
    else
      echo "Patches symlink points elsewhere ($dest), not removing."
    fi
  elif [ -d "$patches_target" ]; then
    for f in "$PATCHES_DIR"/*.py; do
      [ -e "$f" ] || continue
      local base
      base="$(basename "$f")"
      if [ -L "$patches_target/$base" ]; then
        rm -f "$patches_target/$base" && echo "Removed $patches_target/$base"
      fi
    done
  fi
  echo "You may remove the [update_manager kapuchin] section from moonraker.conf if present."
}

main() {
  verify_ready
  check_folders
  if [ "$UNINSTALL" -eq 0 ]; then
    link_extension
    link_patches
    add_updater
  else
    uninstall
  fi
}

main "$@"