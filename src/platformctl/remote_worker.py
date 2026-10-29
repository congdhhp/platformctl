"""Bash-only remote lifecycle worker streamed by the controller over SSH stdin.

No remote Python interpreter, installed platformctl, or persistent agent needed.
The controller supplies quoted positional arguments; this module is not a binary.
"""

WORKER = r'''#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

action="$1"; name="$2"; version="$3"; kind="$4"; root="$5"
managed="$6"; unit="$7"; desired="$8"; autostart="$9"
shift 9
unit_file="$1"; desktop_file="$2"; deb="$3"; package="$4"; has_source="$5"; expected_sha="$6"
current="$root/current"; previous="$root/previous"; release="$root/$version"
launcher="/usr/local/share/applications/$(basename -- "$desktop_file")"
installed_unit="/etc/systemd/system/$unit"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
require_dir() { [[ -d "$1" && ! -L "$1" ]] || die "missing/unsafe release directory: $1"; }
validate_links() {
  [[ "$managed" == 1 ]] || return 0
  local link target base
  base="$(realpath -m -- "$root")"
  for link in "$current" "$previous"; do
    if [[ -e "$link" && ! -L "$link" ]]; then die "controller link is not a symlink: $link"; fi
    if [[ -L "$link" ]]; then
      target="$(readlink -e -- "$link")" || die "broken symlink: $link"
      [[ "$(dirname -- "$target")" == "$base" ]] || die "symlink escapes root: $link"
      require_dir "$target"
    fi
  done
}
linked() {
  if [[ -L "$1" ]]; then basename -- "$(readlink -e -- "$1")"; else printf -- '-'; fi
}
package_version() {
  dpkg-query -W -f='${Status}\t${Version}' "$package" 2>/dev/null || true
}
check_ready() {
  if [[ "$managed" == 1 ]]; then
    validate_links
    require_dir "$release"
    if [[ "$has_source" == 1 ]]; then
      local found
      found="$(package_version)"
      [[ "$found" == $'install ok installed\t'"$version" ]] ||
        die "$name: expected installed package $package version $version (found: $found)"
    fi
  else [[ -d "$root" ]] || die "external installation missing: $root"; fi
}
atomic_link() {
  local dest="$1" target="$2" temporary
  temporary="$root/.platformctl-link-$$-$RANDOM"
  ln -s -- "$target" "$temporary"
  mv -Tf -- "$temporary" "$dest"
}
activate() {
  local wanted="$1" old
  require_dir "$root/$wanted"
  validate_links
  old="$(linked "$current")"
  if [[ "$old" != "$wanted" ]]; then
    if [[ "$old" != '-' ]]; then atomic_link "$previous" "$root/$old"; fi
    atomic_link "$current" "$root/$wanted"
  fi
}
restore_link() {
  local link="$1" original="$2"
  if [[ -z "$original" ]]; then rm -f -- "$link"
  else atomic_link "$link" "$original"; fi
}
check_file() {
  local dest="$1"
  [[ ! -L "$dest" ]] || die "refusing symlink overwrite: $dest"
  [[ ! -e "$dest" || -f "$dest" ]] || die "refusing nonregular overwrite: $dest"
}
file_state() {
  local src="$1" dest="$2"
  [[ -n "$src" ]] || { printf 'unmanaged'; return; }
  check_file "$dest"
  if [[ ! -e "$dest" ]]; then printf missing
  elif cmp -s -- "$src" "$dest"; then printf registered
  else printf outdated; fi
}
atomic_copy() {
  local src="$1" dest="$2" temporary
  mkdir -p -- "$(dirname -- "$dest")"
  check_file "$dest"
  temporary="$(mktemp "$(dirname -- "$dest")/.platformctl-XXXXXXXX")"
  install -m 0644 -- "$src" "$temporary"
  mv -Tf -- "$temporary" "$dest"
}
old_current=''; old_previous=''; old_active=0; old_enabled=0
backup=''; file_target=''; modified=0
restore_on_exit() {
  local failure="$?"
  [[ "$failure" != 0 && "$modified" == 1 ]] || return 0
  trap - EXIT
  if [[ "$kind" == service ]]; then systemctl stop "$unit" >/dev/null 2>&1 || true; fi
  if [[ "$managed" == 1 ]]; then
    restore_link "$current" "$old_current" || true
    restore_link "$previous" "$old_previous" || true
  fi
  if [[ -n "$file_target" ]]; then
    if [[ -n "$backup" ]]; then mv -f -- "$backup" "$file_target" || true
    else rm -f -- "$file_target" || true; fi
  fi
  if [[ "$kind" == service ]]; then
    systemctl daemon-reload || true
    if [[ "$old_enabled" == 1 ]]; then systemctl enable "$unit" || true
    else systemctl disable "$unit" || true; fi
    if [[ "$old_active" == 1 ]]; then systemctl start "$unit" || true; fi
  fi
  printf 'ERROR: %s failed; attempted local restoration\n' "$action" >&2
  exit "$failure"
}
case "$action" in
  status)
    validate_links
    if [[ "$kind" == desktop ]]; then
      printf '%s desktop desired=%s current=%s launcher=%s\n' \
        "$name" "$version" "$(linked "$current")" "$(file_state "$desktop_file" "$launcher")"
    else
      state=stopped; enabled=no
      systemctl is-active --quiet "$unit" && state=running || true
      systemctl is-enabled --quiet "$unit" && enabled=yes || true
      printf '%s service desired=%s current=%s state=%s autostart=%s\n' \
        "$name" "$version" "$(linked "$current")" "$state" "$enabled"
    fi
    ;;
  check)
    check_ready
    printf '%s READY %s\n' "$name" "$version"
    ;;
  diff)
    validate_links
    if [[ "$managed" == 1 && "$(linked "$current")" != "$version" ]]; then
      printf '%s: %s -> %s\n' "$name" "$(linked "$current")" "$version"
    fi
    if [[ "$kind" == desktop ]]; then
      [[ "$(file_state "$desktop_file" "$launcher")" == registered ]] ||
        printf '%s: desktop launcher update required\n' "$name"
    elif [[ -n "$unit_file" ]]; then
      [[ "$(file_state "$unit_file" "$installed_unit")" == registered ]] ||
        printf '%s: systemd unit update required\n' "$name"
    fi
    ;;
  list-releases)
    [[ "$managed" == 1 ]] || die "list-releases unsupported for install_path"
    [[ -d "$root" ]] || die "no managed root: $root"
    for candidate in "$root"/*; do
      [[ -d "$candidate" && ! -L "$candidate" ]] || continue
      base="$(basename -- "$candidate")"
      [[ "$base" != current && "$base" != previous && "$base" != .* ]] || continue
      printf '%s\n' "$base"
    done
    ;;
  install)
    [[ "$managed" == 1 ]] || { echo "$name: external installation"; exit 0; }
    validate_links
    if [[ -e "$release" || -L "$release" ]]; then
      check_ready
      echo "$name: already installed"
      exit 0
    fi
    [[ "$has_source" == 1 && -f "$deb" && ! -L "$deb" ]] || die "missing staged .deb"
    [[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || die "missing validated package SHA-256"
    # Never run dpkg against a package still writable by the SSH user.
    # Copy into a fresh root-owned 0600 file, then verify that exact copy.
    verified="$(mktemp /tmp/platformctl-package-XXXXXXXX)"
    trap 'rm -f -- "$verified"' EXIT
    install -m 0600 -- "$deb" "$verified"
    actual_sha="$(sha256sum -- "$verified" | cut -d' ' -f1)"
    [[ "$actual_sha" == "$expected_sha" ]] || die "package SHA-256 changed after validation"
    [[ "$(dpkg-deb -f "$verified" Package)" == "$package" ]] || die "package mismatch"
    [[ "$(dpkg-deb -f "$verified" Version)" == "$version" ]] || die "version mismatch"
    arch="$(dpkg-deb -f "$verified" Architecture)"
    [[ "$arch" == all || "$arch" == "$(dpkg --print-architecture)" ]] || die "architecture mismatch"
    [[ -z "$(package_version)" ]] || die "dpkg installed package but release missing"
    dpkg -i -- "$verified"
    rm -f -- "$verified"
    trap - EXIT
    check_ready
    printf '%s: installed %s\n' "$name" "$version"
    ;;
  apply|rollback)
    check_ready
    [[ "$managed" == 1 || "$action" == apply ]] || die "external rollback unsupported"
    if [[ "$action" == rollback ]]; then
      validate_links
      [[ -L "$previous" ]] || die "no previous release"
      wanted="$(linked "$previous")"
      require_dir "$root/$wanted"
    else wanted="$version"; fi
    if [[ "$kind" == service ]]; then
      old_current="$(readlink -- "$current" 2>/dev/null || true)"
      old_previous="$(readlink -- "$previous" 2>/dev/null || true)"
      old_active="$(systemctl is-active --quiet "$unit" && printf 1 || printf 0)"
      old_enabled="$(systemctl is-enabled --quiet "$unit" && printf 1 || printf 0)"
      target_change=0
      if [[ -n "$unit_file" && "$(file_state "$unit_file" "$installed_unit")" != registered ]]; then
        target_change=1
      fi
      needs_restart=0
      [[ "$managed" == 1 && "$(linked "$current")" != "$wanted" ]] && needs_restart=1
      [[ "$target_change" == 1 ]] && needs_restart=1
      modified=1
      trap restore_on_exit EXIT
      if [[ "$old_active" == 1 && ( "$needs_restart" == 1 || "$desired" == stopped ) ]]; then
        systemctl stop "$unit"
      fi
      if [[ "$target_change" == 1 ]]; then
        if [[ -f "$installed_unit" ]]; then
          backup="$(mktemp /etc/systemd/system/.platformctl-backup-XXXXXXXX)"
          cp -p -- "$installed_unit" "$backup"
        fi
        file_target="$installed_unit"
        atomic_copy "$unit_file" "$installed_unit"
        systemctl daemon-reload
      fi
      if [[ "$managed" == 1 ]]; then activate "$wanted"; fi
      if [[ "$autostart" == true ]]; then systemctl enable "$unit"
      else systemctl disable "$unit"; fi
      if [[ "$desired" == running ]]; then
        if [[ "$needs_restart" == 1 || "$old_active" != 1 ]]; then systemctl start "$unit"; fi
        systemctl is-active --quiet "$unit" || die "systemd not running"
      else
        if systemctl is-active --quiet "$unit"; then die "systemd not stopped"; fi
      fi
    else
      old_current="$(readlink -- "$current" 2>/dev/null || true)"
      old_previous="$(readlink -- "$previous" 2>/dev/null || true)"
      [[ -n "$desktop_file" ]] || die "desktop_file required"
      target_change=0
      if [[ "$(file_state "$desktop_file" "$launcher")" != registered ]]; then
        target_change=1
      fi
      modified=1
      trap restore_on_exit EXIT
      if [[ "$managed" == 1 ]]; then activate "$wanted"; fi
      if [[ "$target_change" == 1 ]]; then
        mkdir -p /usr/local/share/applications
        if [[ -f "$launcher" ]]; then
          backup="$(mktemp /usr/local/share/applications/.platformctl-backup-XXXXXXXX)"
          cp -p -- "$launcher" "$backup"
        fi
        file_target="$launcher"
        atomic_copy "$desktop_file" "$launcher"
      fi
    fi
    modified=0
    trap - EXIT
    [[ -z "$backup" ]] || rm -f -- "$backup"
    printf '%s: activated %s\n' "$name" "$wanted"
    ;;
  *) die "unsupported operation $action" ;;
esac
'''
