#!/usr/bin/env bash
# =============================================================================
# env-sync.sh — Safe Doppler ↔ .env merge synchronization
#
# Usage:
#   env-sync.sh sync              Interactive per-key conflict resolution
#   env-sync.sh push [--force]    Push local vars to Doppler
#   env-sync.sh pull [--force]    Pull Doppler vars into local
#   env-sync.sh diff              Show differences
#   env-sync.sh status            Quick summary
#
# Compatible with bash 3.2+ (macOS default)
# Config: reads from .matrx.json (preferred) or .matrx-tools.conf (legacy fallback)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"

# shellcheck disable=SC1091
source "${LIB_DIR}/colors.sh"
# shellcheck disable=SC1091
source "${LIB_DIR}/utils.sh"

load_config
ensure_doppler

TMPDIR_SYNC=$(mktemp -d)
trap "rm -rf '$TMPDIR_SYNC'" EXIT

# ─── Multi-config support ───────────────────────────────────────────────────

get_configs() {
    local multi
    multi=$(conf_get "DOPPLER_MULTI" "false")
    if [[ "$multi" == "true" ]]; then
        conf_get "DOPPLER_CONFIGS" "" | tr ',' '\n'
    else
        echo "default"
    fi
}

get_config_value() {
    local config_name="$1"
    local key="$2"
    local default="${3:-}"
    if [[ "$config_name" == "default" ]]; then
        conf_get "$key" "$default"
    else
        conf_get "${key}_${config_name}" "$default"
    fi
}

# Validate that a config has all required values before making Doppler calls.
# Call this once per config at the start of any operation.
validate_config_values() {
    local config_name="$1"
    local has_errors=0
    local label=""
    if [[ "$config_name" != "default" ]]; then
        label=" [config: ${config_name}]"
    fi

    local dp dc ef
    dp=$(get_config_value "$config_name" "DOPPLER_PROJECT")
    dc=$(get_config_value "$config_name" "DOPPLER_CONFIG")
    ef=$(get_config_value "$config_name" "ENV_FILE")

    if [[ -z "$dp" ]]; then
        echo -e "${RED}Error: DOPPLER_PROJECT is not set${label}${NC}"
        has_errors=1
    fi
    if [[ -z "$dc" ]]; then
        echo -e "${RED}Error: DOPPLER_CONFIG is not set${label}${NC}"
        has_errors=1
    fi
    if [[ -z "$ef" ]]; then
        echo -e "${RED}Error: ENV_FILE is not set${label}${NC}"
        has_errors=1
    fi

    if [[ $has_errors -eq 1 ]]; then
        echo -e "${DIM}Check your .matrx.json (or .matrx-tools.conf) and ensure all required values are set.${NC}"
        return 1
    fi
    return 0
}

# ─── Local Override Keys ────────────────────────────────────────────────────
# Machine-specific keys that should NOT be synced between environments.
# On push: stored as placeholders in Doppler
# On pull: existing local values preserved; missing keys added commented out

LOCAL_OVERRIDE_PLACEHOLDER="__REPLACE_ME__"

get_local_keys_list() {
    local config_name="$1"
    get_config_value "$config_name" "ENV_LOCAL_KEYS" "" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | (grep -v '^$' || true)
}

is_local_key() {
    local key="$1"
    local config_name="$2"
    local list_file="$TMPDIR_SYNC/local_override_keys_${config_name}"
    if [[ ! -f "$list_file" ]]; then
        get_local_keys_list "$config_name" > "$list_file"
    fi
    grep -q "^${key}$" "$list_file" 2>/dev/null
}

count_local_keys() {
    local config_name="$1"
    local list_file="$TMPDIR_SYNC/local_override_keys_${config_name}"
    if [[ ! -f "$list_file" ]]; then
        get_local_keys_list "$config_name" > "$list_file"
    fi
    local count
    count=$(grep -c '.' "$list_file" 2>/dev/null) || count=0
    echo "$count"
}

# ─── Shell Variable Resolution ──────────────────────────────────────────────
# Values like DATABASE_URL=postgresql://ship:${POSTGRES_PASSWORD}@db:5432/ship
# contain shell variable references. When comparing with Doppler (which stores
# literal resolved values), we need to resolve these first.

# Create a resolved copy of a parsed env file where all ${VAR} and $VAR
# references are expanded against the same file's own values.
create_resolved_file() {
    local parsed_file="$1"
    local resolved_file="$2"
    : > "$resolved_file"
    while IFS= read -r line; do
        local key="${line%%=*}"
        local value="${line#*=}"
        if value_has_shell_refs "$value"; then
            local resolved
            resolved=$(resolve_shell_refs "$value" "$parsed_file")
            printf '%s=%s\n' "$key" "$resolved" >> "$resolved_file"
        else
            echo "$line" >> "$resolved_file"
        fi
    done < "$parsed_file"
}

# Compare two values, accounting for shell variable references.
# Returns 0 if they match (after resolution), 1 if different.
# Sets HAS_SHELL_REFS=1 if the local value contained refs that were resolved.
HAS_SHELL_REFS=0
values_match() {
    local lval="$1"
    local rval="$2"
    local local_parsed="$3"
    HAS_SHELL_REFS=0

    # Direct match — no resolution needed
    if [[ "$lval" == "$rval" ]]; then
        return 0
    fi

    # Try resolving shell refs in local value
    if value_has_shell_refs "$lval"; then
        local resolved
        resolved=$(resolve_shell_refs "$lval" "$local_parsed")
        if [[ "$resolved" == "$rval" ]]; then
            HAS_SHELL_REFS=1
            return 0
        fi
    fi

    return 1
}

# ─── Interactive Prompt Helper ──────────────────────────────────────────────

# Truncate a value for display, masking secrets if needed
truncate_value() {
    local val="$1"
    local max="${2:-70}"
    if [[ ${#val} -gt $max ]]; then
        echo "${val:0:$max}…"
    else
        echo "$val"
    fi
}

# Prompt the user for a per-key resolution.
# Usage: prompt_resolution "KEY_NAME" "local_value" "remote_value" "category"
# Returns via global: RESOLUTION (one of "local", "remote", "skip")
# category: "local_only", "remote_only", "changed"
RESOLUTION=""

prompt_resolution() {
    local key="$1"
    local lval="$2"
    local rval="$3"
    local category="$4"

    echo ""
    echo -e "${BOLD}─── ${key} ───${NC}"

    case "$category" in
        local_only)
            echo -e "  ${GREEN}Local:${NC}   $(truncate_value "$lval")"
            echo -e "  ${BLUE}Doppler:${NC} ${DIM}(does not exist)${NC}"
            echo ""
            echo -e "  ${BOLD}[1]${NC} Push to Doppler ${DIM}(add local value to remote)${NC}"
            echo -e "  ${BOLD}[2]${NC} Remove locally  ${DIM}(delete from .env)${NC}"
            echo -e "  ${BOLD}[3]${NC} Skip            ${DIM}(leave as-is, no sync)${NC}"
            ;;
        remote_only)
            echo -e "  ${GREEN}Local:${NC}   ${DIM}(does not exist)${NC}"
            echo -e "  ${BLUE}Doppler:${NC} $(truncate_value "$rval")"
            echo ""
            echo -e "  ${BOLD}[1]${NC} Pull from Doppler ${DIM}(add remote value locally)${NC}"
            echo -e "  ${BOLD}[2]${NC} Remove from Doppler ${DIM}(delete from remote)${NC}"
            echo -e "  ${BOLD}[3]${NC} Skip              ${DIM}(leave as-is, no sync)${NC}"
            ;;
        changed)
            echo -e "  ${GREEN}Local:${NC}   $(truncate_value "$lval")"
            echo -e "  ${BLUE}Doppler:${NC} $(truncate_value "$rval")"
            echo ""
            echo -e "  ${BOLD}[1]${NC} Use Remote ${DIM}(overwrite local with Doppler value)${NC}"
            echo -e "  ${BOLD}[2]${NC} Use Local  ${DIM}(overwrite Doppler with local value)${NC}"
            echo -e "  ${BOLD}[3]${NC} Skip       ${DIM}(leave as-is, no sync)${NC}"
            ;;
    esac

    while true; do
        echo -n -e "  ${CYAN}Choose [1/2/3]:${NC} "
        read -r choice </dev/tty
        case "$choice" in
            1)
                case "$category" in
                    local_only)  RESOLUTION="push" ;;
                    remote_only) RESOLUTION="pull" ;;
                    changed)     RESOLUTION="remote" ;;
                esac
                return
                ;;
            2)
                case "$category" in
                    local_only)  RESOLUTION="remove_local" ;;
                    remote_only) RESOLUTION="remove_remote" ;;
                    changed)     RESOLUTION="local" ;;
                esac
                return
                ;;
            3) RESOLUTION="skip"; return ;;
            *) echo -e "  ${RED}Invalid choice. Enter 1, 2, or 3.${NC}" ;;
        esac
    done
}

# ─── SYNC (Interactive) ────────────────────────────────────────────────────

run_sync() {
    local config_name="$1"
    local doppler_project doppler_config env_file
    doppler_project=$(get_config_value "$config_name" "DOPPLER_PROJECT")
    doppler_config=$(get_config_value "$config_name" "DOPPLER_CONFIG")
    env_file=$(get_config_value "$config_name" "ENV_FILE")

    if [[ "$config_name" != "default" ]]; then
        echo -e "${BOLD}━━━ [$config_name] ━━━${NC}"
    fi
    echo -e "${CYAN}Interactive sync: ${env_file} ↔ Doppler (${doppler_project} / ${doppler_config})${NC}"
    echo -e "${DIM}For each difference, choose how to resolve it.${NC}"

    local full_env_path="${REPO_ROOT}/${env_file}"

    if [[ ! -f "$full_env_path" ]]; then
        echo -e "${YELLOW}No ${env_file} found — pulling all from Doppler${NC}"
        run_pull_merge "$config_name"
        return
    fi

    local local_file="$TMPDIR_SYNC/local_parsed_sync_${config_name}"
    local remote_file="$TMPDIR_SYNC/remote_parsed_sync_${config_name}"
    local local_keys="$TMPDIR_SYNC/local_keys_sync_${config_name}"
    local remote_keys="$TMPDIR_SYNC/remote_keys_sync_${config_name}"

    parse_env_to_sorted_file "$full_env_path" "$local_file"
    get_doppler_secrets "$doppler_project" "$doppler_config" > "$TMPDIR_SYNC/remote_raw_sync_${config_name}"
    parse_env_to_sorted_file "$TMPDIR_SYNC/remote_raw_sync_${config_name}" "$remote_file"

    extract_keys "$local_file" > "$local_keys"
    extract_keys "$remote_file" > "$remote_keys"

    # ─── Collect all differences ────────────────────────────────────────
    # Each entry is stored as: category|key
    # We collect them all first, then present them interactively
    local diff_list="$TMPDIR_SYNC/diff_list_${config_name}"
    : > "$diff_list"
    local total_diffs=0

    # Keys only in local
    while IFS= read -r key; do
        if ! key_exists "$key" "$remote_file"; then
            if ! is_local_key "$key" "$config_name"; then
                echo "local_only|${key}" >> "$diff_list"
                total_diffs=$((total_diffs + 1))
            fi
        fi
    done < "$local_keys"

    # Keys only in remote
    while IFS= read -r key; do
        if ! key_exists "$key" "$local_file"; then
            if ! is_local_key "$key" "$config_name"; then
                echo "remote_only|${key}" >> "$diff_list"
                total_diffs=$((total_diffs + 1))
            fi
        fi
    done < "$remote_keys"

    # Keys in both but with different values (accounting for shell variable refs)
    local shell_ref_keys=0
    while IFS= read -r key; do
        if key_exists "$key" "$remote_file"; then
            local lval rval
            lval=$(lookup_value "$key" "$local_file")
            rval=$(lookup_value "$key" "$remote_file")
            if [[ "$lval" != "$rval" ]]; then
                if ! is_local_key "$key" "$config_name"; then
                    # Check if the difference is only due to shell variable references
                    if values_match "$lval" "$rval" "$local_file"; then
                        shell_ref_keys=$((shell_ref_keys + 1))
                    else
                        echo "changed|${key}" >> "$diff_list"
                        total_diffs=$((total_diffs + 1))
                    fi
                fi
            fi
        fi
    done < "$local_keys"

    if [[ $total_diffs -eq 0 ]]; then
        echo ""
        if [[ $shell_ref_keys -gt 0 ]]; then
            echo -e "${GREEN}✓ Already in sync${NC} ${DIM}($shell_ref_keys key(s) use shell variable refs that resolve to matching values)${NC}"
        else
            echo -e "${GREEN}✓ Already in sync — no differences found${NC}"
        fi
        return
    fi

    echo ""
    echo -e "${BOLD}Found ${total_diffs} difference(s) to resolve:${NC}"

    # ─── Collect resolutions ────────────────────────────────────────────
    # Store resolutions as: action|key|value
    local resolutions_file="$TMPDIR_SYNC/resolutions_${config_name}"
    : > "$resolutions_file"

    local idx=0
    while IFS='|' read -r category key; do
        idx=$((idx + 1))
        local lval rval
        lval=$(lookup_value "$key" "$local_file")
        rval=$(lookup_value "$key" "$remote_file")

        echo -e "${DIM}($idx/$total_diffs)${NC}"
        prompt_resolution "$key" "$lval" "$rval" "$category"

        echo "${RESOLUTION}|${key}" >> "$resolutions_file"
    done < "$diff_list"

    # ─── Apply resolutions ──────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}━━━ Applying changes ━━━${NC}"
    echo ""

    # Track what needs to change
    local local_updates="$TMPDIR_SYNC/local_updates_${config_name}"     # key=value pairs to set locally
    local local_removes="$TMPDIR_SYNC/local_removes_${config_name}"     # keys to remove locally
    local remote_updates="$TMPDIR_SYNC/remote_updates_${config_name}"   # key=value pairs to set in Doppler
    local remote_removes="$TMPDIR_SYNC/remote_removes_${config_name}"   # keys to remove from Doppler
    : > "$local_updates"
    : > "$local_removes"
    : > "$remote_updates"
    : > "$remote_removes"

    local pull_count=0 push_count=0 remove_local_count=0 remove_remote_count=0 skip_count=0

    while IFS='|' read -r action key; do
        local lval rval
        lval=$(lookup_value "$key" "$local_file")
        rval=$(lookup_value "$key" "$remote_file")

        # When pushing to Doppler, resolve shell variable references
        local push_val="$lval"
        if value_has_shell_refs "$lval"; then
            push_val=$(resolve_shell_refs "$lval" "$local_file")
        fi

        case "$action" in
            remote)
                # Use remote value → update local
                printf '%s=%s\n' "$key" "$rval" >> "$local_updates"
                echo -e "  ${BLUE}↓${NC} $key ${DIM}← Doppler${NC}"
                pull_count=$((pull_count + 1))
                ;;
            local)
                # Use local value → update Doppler (resolved)
                printf '%s=%s\n' "$key" "$push_val" >> "$remote_updates"
                echo -e "  ${GREEN}↑${NC} $key ${DIM}→ Doppler${NC}"
                if [[ "$push_val" != "$lval" ]]; then
                    echo -e "       ${DIM}(resolved: $(truncate_value "$push_val" 55))${NC}"
                fi
                push_count=$((push_count + 1))
                ;;
            pull)
                # Remote-only key → pull to local
                printf '%s=%s\n' "$key" "$rval" >> "$local_updates"
                echo -e "  ${BLUE}↓${NC} $key ${DIM}← Doppler (new locally)${NC}"
                pull_count=$((pull_count + 1))
                ;;
            push)
                # Local-only key → push to Doppler (resolved)
                printf '%s=%s\n' "$key" "$push_val" >> "$remote_updates"
                echo -e "  ${GREEN}↑${NC} $key ${DIM}→ Doppler (new remotely)${NC}"
                if [[ "$push_val" != "$lval" ]]; then
                    echo -e "       ${DIM}(resolved: $(truncate_value "$push_val" 55))${NC}"
                fi
                push_count=$((push_count + 1))
                ;;
            remove_local)
                echo "$key" >> "$local_removes"
                echo -e "  ${RED}✕${NC} $key ${DIM}(removed locally)${NC}"
                remove_local_count=$((remove_local_count + 1))
                ;;
            remove_remote)
                echo "$key" >> "$remote_removes"
                echo -e "  ${RED}✕${NC} $key ${DIM}(removed from Doppler)${NC}"
                remove_remote_count=$((remove_remote_count + 1))
                ;;
            skip)
                echo -e "  ${DIM}—${NC} $key ${DIM}(skipped)${NC}"
                skip_count=$((skip_count + 1))
                ;;
        esac
    done < "$resolutions_file"

    local has_local_changes=0
    local has_remote_changes=0

    if [[ -s "$local_updates" ]] || [[ -s "$local_removes" ]]; then
        has_local_changes=1
    fi
    if [[ -s "$remote_updates" ]] || [[ -s "$remote_removes" ]]; then
        has_remote_changes=1
    fi

    # ─── Apply local .env changes ───────────────────────────────────────
    if [[ $has_local_changes -eq 1 ]]; then
        backup_file "$full_env_path" "${REPO_ROOT}/.env-backups"

        local timestamp
        timestamp=$(date '+%Y-%m-%d %H:%M')
        local tmpout="$TMPDIR_SYNC/sync_output_${config_name}"
        touch "$tmpout"

        # Process existing lines: update changed values, remove deleted keys
        while IFS= read -r line; do
            # Pass through comments and blank lines
            if echo "$line" | grep -q '^\s*#' || echo "$line" | grep -q '^\s*$'; then
                echo "$line" >> "$tmpout"
                continue
            fi

            local key="${line%%=*}"

            # Check if this key should be removed
            if grep -q "^${key}$" "$local_removes" 2>/dev/null; then
                echo "# [env-sync $timestamp] Removed during sync:" >> "$tmpout"
                echo "# ${line}" >> "$tmpout"
                continue
            fi

            # Check if this key has an updated value
            if key_exists "$key" "$local_updates"; then
                local new_val
                new_val=$(lookup_value "$key" "$local_updates")
                printf '%s=%s\n' "$key" "$new_val" >> "$tmpout"
            else
                echo "$line" >> "$tmpout"
            fi
        done < "$full_env_path"

        # Append new keys (from remote_only → pull)
        local has_new_section=0
        while IFS= read -r update_line; do
            local key="${update_line%%=*}"
            # Only add if key doesn't already exist in the file
            if ! grep -q "^${key}=" "$full_env_path" 2>/dev/null; then
                if [[ $has_new_section -eq 0 ]]; then
                    echo "" >> "$tmpout"
                    echo "# [env-sync $timestamp] Added during sync:" >> "$tmpout"
                    has_new_section=1
                fi
                echo "$update_line" >> "$tmpout"
            fi
        done < "$local_updates"

        mv "$tmpout" "$full_env_path"
    fi

    # ─── Apply Doppler changes ──────────────────────────────────────────
    if [[ $has_remote_changes -eq 1 ]]; then
        # Build the full Doppler state: start from current remote, apply changes
        local doppler_merged="$TMPDIR_SYNC/doppler_merged_${config_name}"
        : > "$doppler_merged"

        # Start with all current remote keys
        while IFS= read -r key; do
            # Skip keys being removed from remote
            if grep -q "^${key}$" "$remote_removes" 2>/dev/null; then
                continue
            fi
            # Check if we have an updated value for this key
            if key_exists "$key" "$remote_updates"; then
                local new_val
                new_val=$(lookup_value "$key" "$remote_updates")
                printf '%s=%s\n' "$key" "$new_val" >> "$doppler_merged"
            else
                local rval
                rval=$(lookup_value "$key" "$remote_file")
                printf '%s=%s\n' "$key" "$rval" >> "$doppler_merged"
            fi
        done < "$remote_keys"

        # Add new keys (local_only → push)
        while IFS= read -r update_line; do
            local key="${update_line%%=*}"
            if ! key_exists "$key" "$remote_file"; then
                echo "$update_line" >> "$doppler_merged"
            fi
        done < "$remote_updates"

        # Handle local override keys: ensure they're placeholders in Doppler
        while IFS= read -r key; do
            if is_local_key "$key" "$config_name" && ! key_exists "$key" "$doppler_merged"; then
                printf '%s=%s\n' "$key" "$LOCAL_OVERRIDE_PLACEHOLDER" >> "$doppler_merged"
            fi
        done < "$local_keys"

        local err_file="$TMPDIR_SYNC/doppler_upload_err"
        if ! doppler secrets upload \
            --project "$doppler_project" \
            --config "$doppler_config" \
            "$doppler_merged" 2>"$err_file"; then
            echo -e "${RED}Error: Doppler upload failed${NC}"
            [[ -s "$err_file" ]] && cat "$err_file" >&2
            return 1
        fi
    fi

    # ─── Handle remote-only removals (when no other remote changes) ─────
    if [[ $has_remote_changes -eq 0 ]] && [[ -s "$remote_removes" ]]; then
        # We need to do a targeted removal: download current state, remove keys, re-upload
        local doppler_current="$TMPDIR_SYNC/doppler_current_${config_name}"
        : > "$doppler_current"

        while IFS= read -r key; do
            if ! grep -q "^${key}$" "$remote_removes" 2>/dev/null; then
                local rval
                rval=$(lookup_value "$key" "$remote_file")
                printf '%s=%s\n' "$key" "$rval" >> "$doppler_current"
            fi
        done < "$remote_keys"

        local err_file="$TMPDIR_SYNC/doppler_upload_err"
        if ! doppler secrets upload \
            --project "$doppler_project" \
            --config "$doppler_config" \
            "$doppler_current" 2>"$err_file"; then
            echo -e "${RED}Error: Doppler upload failed${NC}"
            [[ -s "$err_file" ]] && cat "$err_file" >&2
            return 1
        fi
    fi

    # ─── Summary ────────────────────────────────────────────────────────
    echo ""
    echo -e "${DIM}────────────────────────────────────${NC}"
    if [[ $pull_count -gt 0 ]]; then
        echo -e "  ${BLUE}Pulled:${NC}           $pull_count ${DIM}(Doppler → local)${NC}"
    fi
    if [[ $push_count -gt 0 ]]; then
        echo -e "  ${GREEN}Pushed:${NC}           $push_count ${DIM}(local → Doppler)${NC}"
    fi
    if [[ $remove_local_count -gt 0 ]]; then
        echo -e "  ${RED}Removed locally:${NC}  $remove_local_count"
    fi
    if [[ $remove_remote_count -gt 0 ]]; then
        echo -e "  ${RED}Removed remote:${NC}   $remove_remote_count"
    fi
    if [[ $skip_count -gt 0 ]]; then
        echo -e "  ${DIM}Skipped:          $skip_count${NC}"
    fi
    echo ""

    if [[ $has_local_changes -eq 1 ]] || [[ $has_remote_changes -eq 1 ]] || [[ -s "$remote_removes" ]]; then
        echo -e "${GREEN}✓ Sync complete${NC}"
    else
        echo -e "${DIM}No changes applied${NC}"
    fi
}

cmd_sync() {
    local configs
    configs=$(get_configs)
    while IFS= read -r config_name; do
        [[ -z "$config_name" ]] && continue
        validate_config_values "$config_name" || continue
        run_sync "$config_name"
    done <<< "$configs"
}

# ─── STATUS ──────────────────────────────────────────────────────────────────

run_status() {
    local config_name="$1"
    local doppler_project doppler_config env_file
    doppler_project=$(get_config_value "$config_name" "DOPPLER_PROJECT")
    doppler_config=$(get_config_value "$config_name" "DOPPLER_CONFIG")
    env_file=$(get_config_value "$config_name" "ENV_FILE")

    if [[ "$config_name" != "default" ]]; then
        echo -e "${BOLD}[$config_name]${NC}"
    fi
    echo -e "${CYAN}Env sync status${NC}"

    if [[ ! -f "${REPO_ROOT}/${env_file}" ]]; then
        echo -e "  ${RED}No ${env_file} found${NC}"
        return
    fi

    local local_file="$TMPDIR_SYNC/local_parsed_${config_name}"
    local remote_file="$TMPDIR_SYNC/remote_parsed_${config_name}"

    parse_env_to_sorted_file "${REPO_ROOT}/${env_file}" "$local_file"
    get_doppler_secrets "$doppler_project" "$doppler_config" > "$TMPDIR_SYNC/remote_raw_${config_name}"
    parse_env_to_sorted_file "$TMPDIR_SYNC/remote_raw_${config_name}" "$remote_file"

    local local_count remote_count
    local_count=$(wc -l < "$local_file" | tr -d ' ')
    remote_count=$(wc -l < "$remote_file" | tr -d ' ')

    local override_count
    override_count=$(count_local_keys "$config_name")

    echo -e "  Local (${env_file}):  ${GREEN}${local_count}${NC} variables"
    echo -e "  Doppler (${doppler_config}):     ${BLUE}${remote_count}${NC} variables"
    if [[ "$override_count" -gt 0 ]]; then
    echo -e "  Local overrides:     ${CYAN}${override_count}${NC} keys ${DIM}(machine-specific, not synced)${NC}"
    fi
    echo ""
}

cmd_status() {
    local configs
    configs=$(get_configs)
    while IFS= read -r config_name; do
        [[ -z "$config_name" ]] && continue
        validate_config_values "$config_name" || continue
        run_status "$config_name"
    done <<< "$configs"
    echo -e "  Run ${CYAN}env:sync${NC} to interactively resolve differences"
    echo -e "  Run ${CYAN}env:diff${NC} for a detailed comparison"
}

# ─── DIFF ────────────────────────────────────────────────────────────────────

run_diff() {
    local config_name="$1"
    local doppler_project doppler_config env_file
    doppler_project=$(get_config_value "$config_name" "DOPPLER_PROJECT")
    doppler_config=$(get_config_value "$config_name" "DOPPLER_CONFIG")
    env_file=$(get_config_value "$config_name" "ENV_FILE")

    if [[ "$config_name" != "default" ]]; then
        echo -e "${BOLD}━━━ [$config_name] ━━━${NC}"
    fi
    echo -e "${CYAN}Comparing ${env_file} ↔ Doppler (${doppler_project} / ${doppler_config})${NC}"
    echo ""

    if [[ ! -f "${REPO_ROOT}/${env_file}" ]]; then
        echo -e "${RED}Error: ${env_file} not found${NC}"
        return
    fi

    local local_file="$TMPDIR_SYNC/local_parsed_${config_name}"
    local remote_file="$TMPDIR_SYNC/remote_parsed_${config_name}"
    local local_keys="$TMPDIR_SYNC/local_keys_${config_name}"
    local remote_keys="$TMPDIR_SYNC/remote_keys_${config_name}"

    parse_env_to_sorted_file "${REPO_ROOT}/${env_file}" "$local_file"
    get_doppler_secrets "$doppler_project" "$doppler_config" > "$TMPDIR_SYNC/remote_raw_${config_name}"
    parse_env_to_sorted_file "$TMPDIR_SYNC/remote_raw_${config_name}" "$remote_file"

    extract_keys "$local_file" > "$local_keys"
    extract_keys "$remote_file" > "$remote_keys"

    local local_only=0 remote_only=0 changed=0 same=0 local_overrides=0 shell_ref_matches=0

    while IFS= read -r key; do
        if ! key_exists "$key" "$remote_file"; then
            echo -e "${GREEN}+ LOCAL ONLY:${NC}  $key"
            local lval
            lval=$(lookup_value "$key" "$local_file")
            echo -e "    ${DIM}value: $(truncate_value "$lval" 60)${NC}"
            local_only=$((local_only + 1))
        fi
    done < "$local_keys"

    while IFS= read -r key; do
        if ! key_exists "$key" "$local_file"; then
            if is_local_key "$key" "$config_name"; then
                echo -e "${CYAN}⚙ LOCAL OVERRIDE:${NC} $key ${DIM}(missing locally — will be commented out on pull)${NC}"
                local_overrides=$((local_overrides + 1))
            else
                echo -e "${BLUE}+ DOPPLER ONLY:${NC} $key"
                local rval
                rval=$(lookup_value "$key" "$remote_file")
                echo -e "    ${DIM}value: $(truncate_value "$rval" 60)${NC}"
                remote_only=$((remote_only + 1))
            fi
        fi
    done < "$remote_keys"

    while IFS= read -r key; do
        if key_exists "$key" "$remote_file"; then
            local lval rval
            lval=$(lookup_value "$key" "$local_file")
            rval=$(lookup_value "$key" "$remote_file")
            if [[ "$lval" != "$rval" ]]; then
                if is_local_key "$key" "$config_name"; then
                    echo -e "${CYAN}⚙ LOCAL OVERRIDE:${NC} $key ${DIM}(machine-specific — kept local)${NC}"
                    local_overrides=$((local_overrides + 1))
                elif values_match "$lval" "$rval" "$local_file"; then
                    # Shell variable reference resolves to the same value
                    echo -e "${DIM}≈ SHELL REF:${NC}    $key ${DIM}(local uses \${VAR} reference, resolves to same value)${NC}"
                    same=$((same + 1))
                    shell_ref_matches=$((shell_ref_matches + 1))
                else
                    echo -e "${YELLOW}~ CHANGED:${NC}      $key"
                    echo -e "    ${GREEN}local:${NC}   $(truncate_value "$lval" 60)"
                    if value_has_shell_refs "$lval"; then
                        local resolved
                        resolved=$(resolve_shell_refs "$lval" "$local_file")
                        echo -e "    ${GREEN}resolved:${NC} ${DIM}$(truncate_value "$resolved" 57)${NC}"
                    fi
                    echo -e "    ${BLUE}doppler:${NC} $(truncate_value "$rval" 60)"
                    changed=$((changed + 1))
                fi
            else
                same=$((same + 1))
            fi
        fi
    done < "$local_keys"

    echo ""
    echo -e "${DIM}────────────────────────────────────${NC}"
    echo -e "  ${GREEN}Local only:${NC}       $local_only"
    echo -e "  ${BLUE}Doppler only:${NC}     $remote_only"
    echo -e "  ${YELLOW}Changed:${NC}          $changed"
    if [[ $local_overrides -gt 0 ]]; then
    echo -e "  ${CYAN}Local overrides:${NC}  $local_overrides ${DIM}(machine-specific, not synced)${NC}"
    fi
    if [[ $shell_ref_matches -gt 0 ]]; then
    echo -e "  ${DIM}Shell ref match:  $shell_ref_matches (local uses \${VAR}, resolves identically)${NC}"
    fi
    echo -e "  ${DIM}Identical:        $same${NC}"

    local total_actionable=$((local_only + remote_only + changed))
    if [[ $total_actionable -gt 0 ]]; then
        echo ""
        echo -e "  Run ${CYAN}env:sync${NC} to interactively resolve these differences"
    fi
    echo ""
}

cmd_diff() {
    local configs
    configs=$(get_configs)
    while IFS= read -r config_name; do
        [[ -z "$config_name" ]] && continue
        validate_config_values "$config_name" || continue
        run_diff "$config_name"
    done <<< "$configs"
}

# ─── PUSH ────────────────────────────────────────────────────────────────────

run_push_force() {
    local config_name="$1"
    local doppler_project doppler_config env_file
    doppler_project=$(get_config_value "$config_name" "DOPPLER_PROJECT")
    doppler_config=$(get_config_value "$config_name" "DOPPLER_CONFIG")
    env_file=$(get_config_value "$config_name" "ENV_FILE")

    if [[ "$config_name" != "default" ]]; then echo -e "${BOLD}[$config_name]${NC}"; fi

    if [[ ! -f "${REPO_ROOT}/${env_file}" ]]; then
        echo -e "${RED}Error: ${env_file} not found${NC}"; return
    fi

    local override_count
    override_count=$(count_local_keys "$config_name")

    # Pre-parse the local file for shell variable resolution
    local local_parsed_force="$TMPDIR_SYNC/local_parsed_force_${config_name}"
    parse_env_to_sorted_file "${REPO_ROOT}/${env_file}" "$local_parsed_force"

    local upload_file="$TMPDIR_SYNC/upload_force_${config_name}"
    : > "$upload_file"

    while IFS= read -r line; do
        if echo "$line" | grep -q '^\s*#' || echo "$line" | grep -q '^\s*$'; then
            if [[ "$override_count" -eq 0 ]]; then
                continue
            fi
            continue
        fi
        local key="${line%%=*}"
        local value="${line#*=}"
        value="${value#\"}"
        value="${value%\"}"

        if [[ "$override_count" -gt 0 ]] && is_local_key "$key" "$config_name"; then
            printf '%s=%s\n' "$key" "$LOCAL_OVERRIDE_PLACEHOLDER" >> "$upload_file"
        else
            # Resolve shell variable references before pushing
            if value_has_shell_refs "$value"; then
                local resolved
                resolved=$(resolve_shell_refs "$value" "$local_parsed_force")
                printf '%s=%s\n' "$key" "$resolved" >> "$upload_file"
            else
                printf '%s=%s\n' "$key" "$value" >> "$upload_file"
            fi
        fi
    done < "${REPO_ROOT}/${env_file}"

    local err_file="$TMPDIR_SYNC/doppler_upload_err"
    if ! doppler secrets upload \
        --project "$doppler_project" \
        --config "$doppler_config" \
        "$upload_file" 2>"$err_file"; then
        echo -e "${RED}Error: Doppler upload failed${NC}"
        [[ -s "$err_file" ]] && cat "$err_file" >&2
        return 1
    fi

    if [[ "$override_count" -gt 0 ]]; then
        echo -e "${GREEN}✓ ${env_file} force-pushed to Doppler (${override_count} local overrides stored as placeholders)${NC}"
    else
        echo -e "${GREEN}✓ ${env_file} force-pushed to Doppler (full replace)${NC}"
    fi
}

run_push_merge() {
    local config_name="$1"
    local doppler_project doppler_config env_file
    doppler_project=$(get_config_value "$config_name" "DOPPLER_PROJECT")
    doppler_config=$(get_config_value "$config_name" "DOPPLER_CONFIG")
    env_file=$(get_config_value "$config_name" "ENV_FILE")

    if [[ "$config_name" != "default" ]]; then echo -e "${BOLD}━━━ [$config_name] ━━━${NC}"; fi
    echo -e "${CYAN}Pushing ${env_file} → Doppler (${doppler_project} / ${doppler_config})${NC}"
    echo -e "${DIM}Mode: merge (add + update, never delete)${NC}"
    echo ""

    if [[ ! -f "${REPO_ROOT}/${env_file}" ]]; then
        echo -e "${RED}Error: ${env_file} not found${NC}"; return
    fi

    local local_file="$TMPDIR_SYNC/local_parsed_${config_name}"
    local remote_file="$TMPDIR_SYNC/remote_parsed_${config_name}"
    local merged_file="$TMPDIR_SYNC/merged_${config_name}"
    local local_keys="$TMPDIR_SYNC/local_keys_${config_name}"
    local remote_keys="$TMPDIR_SYNC/remote_keys_${config_name}"

    parse_env_to_sorted_file "${REPO_ROOT}/${env_file}" "$local_file"
    get_doppler_secrets "$doppler_project" "$doppler_config" > "$TMPDIR_SYNC/remote_raw_${config_name}"
    parse_env_to_sorted_file "$TMPDIR_SYNC/remote_raw_${config_name}" "$remote_file"

    extract_keys "$local_file" > "$local_keys"
    extract_keys "$remote_file" > "$remote_keys"
    touch "$merged_file"

    local added=0 updated=0 kept=0 local_overrides=0

    while IFS= read -r key; do
        local rval
        rval=$(lookup_value "$key" "$remote_file")
        if key_exists "$key" "$local_file"; then
            local lval
            lval=$(lookup_value "$key" "$local_file")
            if is_local_key "$key" "$config_name"; then
                printf '%s=%s\n' "$key" "$LOCAL_OVERRIDE_PLACEHOLDER" >> "$merged_file"
                if [[ "$rval" != "$LOCAL_OVERRIDE_PLACEHOLDER" ]]; then
                    echo -e "  ${CYAN}⚙${NC} $key ${DIM}(local override → placeholder in Doppler)${NC}"
                    local_overrides=$((local_overrides + 1))
                else
                    kept=$((kept + 1))
                fi
            elif values_match "$lval" "$rval" "$local_file"; then
                # Values match (possibly after resolving shell variable refs)
                printf '%s=%s\n' "$key" "$rval" >> "$merged_file"
                kept=$((kept + 1))
            else
                # Resolve shell variable references before pushing to Doppler
                local push_val="$lval"
                if value_has_shell_refs "$lval"; then
                    push_val=$(resolve_shell_refs "$lval" "$local_file")
                fi
                echo -e "  ${YELLOW}~${NC} $key ${DIM}(updated)${NC}"
                if [[ "$push_val" != "$lval" ]]; then
                    echo -e "       ${DIM}(resolved \${VAR} refs: $(truncate_value "$push_val" 50))${NC}"
                fi
                printf '%s=%s\n' "$key" "$push_val" >> "$merged_file"
                updated=$((updated + 1))
            fi
        else
            printf '%s=%s\n' "$key" "$rval" >> "$merged_file"
            kept=$((kept + 1))
        fi
    done < "$remote_keys"

    while IFS= read -r key; do
        if ! key_exists "$key" "$remote_file"; then
            if is_local_key "$key" "$config_name"; then
                echo -e "  ${CYAN}⚙${NC} $key ${DIM}(local override → placeholder in Doppler)${NC}"
                printf '%s=%s\n' "$key" "$LOCAL_OVERRIDE_PLACEHOLDER" >> "$merged_file"
                local_overrides=$((local_overrides + 1))
            else
                local lval
                lval=$(lookup_value "$key" "$local_file")
                # Resolve shell variable references before pushing to Doppler
                local push_val="$lval"
                if value_has_shell_refs "$lval"; then
                    push_val=$(resolve_shell_refs "$lval" "$local_file")
                fi
                echo -e "  ${GREEN}+${NC} $key ${DIM}(new)${NC}"
                if [[ "$push_val" != "$lval" ]]; then
                    echo -e "       ${DIM}(resolved \${VAR} refs: $(truncate_value "$push_val" 50))${NC}"
                fi
                printf '%s=%s\n' "$key" "$push_val" >> "$merged_file"
                added=$((added + 1))
            fi
        fi
    done < "$local_keys"

    if [[ $added -eq 0 && $updated -eq 0 && $local_overrides -eq 0 ]]; then
        echo -e "${GREEN}Already in sync — nothing to push${NC}"; return
    fi

    echo ""
    echo -e "  ${GREEN}Adding:${NC}           $added new keys"
    echo -e "  ${YELLOW}Updating:${NC}         $updated changed keys"
    if [[ $local_overrides -gt 0 ]]; then
    echo -e "  ${CYAN}Local overrides:${NC}  $local_overrides ${DIM}(stored as placeholders)${NC}"
    fi
    echo -e "  ${DIM}Keeping:          $kept unchanged keys${NC}"
    echo ""

    local err_file="$TMPDIR_SYNC/doppler_upload_err"
    if ! doppler secrets upload \
        --project "$doppler_project" \
        --config "$doppler_config" \
        "$merged_file" 2>"$err_file"; then
        echo -e "${RED}Error: Doppler upload failed${NC}"
        [[ -s "$err_file" ]] && cat "$err_file" >&2
        return 1
    fi

    echo -e "${GREEN}✓ Doppler updated successfully${NC}"
}

cmd_push() {
    local force="${1:-}"
    local configs
    configs=$(get_configs)
    while IFS= read -r config_name; do
        [[ -z "$config_name" ]] && continue
        validate_config_values "$config_name" || continue
        if [[ "$force" == "--force" ]]; then run_push_force "$config_name"
        else run_push_merge "$config_name"; fi
    done <<< "$configs"
}

# ─── PULL ────────────────────────────────────────────────────────────────────

run_pull_force() {
    local config_name="$1"
    local doppler_project doppler_config env_file
    doppler_project=$(get_config_value "$config_name" "DOPPLER_PROJECT")
    doppler_config=$(get_config_value "$config_name" "DOPPLER_CONFIG")
    env_file=$(get_config_value "$config_name" "ENV_FILE")

    if [[ "$config_name" != "default" ]]; then echo -e "${BOLD}[$config_name]${NC}"; fi

    local full_path="${REPO_ROOT}/${env_file}"
    local env_dir
    env_dir=$(dirname "$full_path")
    mkdir -p "$env_dir"

    local override_count
    override_count=$(count_local_keys "$config_name")

    if [[ "$override_count" -eq 0 ]]; then
        get_doppler_secrets "$doppler_project" "$doppler_config" > "$full_path"
        echo -e "${GREEN}✓ ${env_file} force-pulled from Doppler (full replace)${NC}"
        return
    fi

    local remote_file="$TMPDIR_SYNC/remote_parsed_force_${config_name}"
    local remote_keys="$TMPDIR_SYNC/remote_keys_force_${config_name}"
    get_doppler_secrets "$doppler_project" "$doppler_config" > "$TMPDIR_SYNC/remote_raw_force_${config_name}"
    parse_env_to_sorted_file "$TMPDIR_SYNC/remote_raw_force_${config_name}" "$remote_file"
    extract_keys "$remote_file" > "$remote_keys"

    local existing_overrides="$TMPDIR_SYNC/existing_overrides_${config_name}"
    touch "$existing_overrides"
    if [[ -f "$full_path" ]]; then
        local existing_parsed="$TMPDIR_SYNC/existing_parsed_force_${config_name}"
        parse_env_to_sorted_file "$full_path" "$existing_parsed"
        while IFS= read -r key; do
            if is_local_key "$key" "$config_name" && key_exists "$key" "$existing_parsed"; then
                local val
                val=$(lookup_value "$key" "$existing_parsed")
                printf '%s=%s\n' "$key" "$val" >> "$existing_overrides"
            fi
        done < "$remote_keys"
    fi

    backup_file "$full_path" "${REPO_ROOT}/.env-backups"

    local tmpout="$TMPDIR_SYNC/force_output_${config_name}"
    {
        echo "# Force-pulled from Doppler (${doppler_project} / ${doppler_config})"
        echo "# Generated: $(date '+%Y-%m-%d %H:%M:%S')"
        echo ""
        local has_override_section=0
        while IFS= read -r key; do
            if is_local_key "$key" "$config_name"; then
                if key_exists "$key" "$existing_overrides"; then
                    local val
                    val=$(lookup_value "$key" "$existing_overrides")
                    printf '%s="%s"\n' "$key" "$val"
                else
                    if [[ $has_override_section -eq 0 ]]; then
                        echo ""
                        echo "# [env-sync] Local override variables — set these for your environment:"
                        has_override_section=1
                    fi
                    local rval
                    rval=$(lookup_value "$key" "$remote_file")
                    printf '# %s="%s"\n' "$key" "$rval"
                fi
            else
                local rval
                rval=$(lookup_value "$key" "$remote_file")
                printf '%s="%s"\n' "$key" "$rval"
            fi
        done < "$remote_keys"
    } > "$tmpout"

    mv "$tmpout" "$full_path"
    echo -e "${GREEN}✓ ${env_file} force-pulled from Doppler (local overrides preserved)${NC}"
}

run_pull_merge() {
    local config_name="$1"
    local doppler_project doppler_config env_file
    doppler_project=$(get_config_value "$config_name" "DOPPLER_PROJECT")
    doppler_config=$(get_config_value "$config_name" "DOPPLER_CONFIG")
    env_file=$(get_config_value "$config_name" "ENV_FILE")

    if [[ "$config_name" != "default" ]]; then echo -e "${BOLD}━━━ [$config_name] ━━━${NC}"; fi
    echo -e "${CYAN}Pulling Doppler (${doppler_project} / ${doppler_config}) → ${env_file}${NC}"
    echo -e "${DIM}Mode: merge (add + update with conflict comments, never delete)${NC}"
    echo ""

    local remote_file="$TMPDIR_SYNC/remote_parsed_${config_name}"
    get_doppler_secrets "$doppler_project" "$doppler_config" > "$TMPDIR_SYNC/remote_raw_${config_name}"
    parse_env_to_sorted_file "$TMPDIR_SYNC/remote_raw_${config_name}" "$remote_file"

    local remote_keys="$TMPDIR_SYNC/remote_keys_${config_name}"
    extract_keys "$remote_file" > "$remote_keys"

    local full_env_path="${REPO_ROOT}/${env_file}"

    if [[ ! -f "$full_env_path" ]]; then
        echo -e "${YELLOW}No ${env_file} found — creating from Doppler${NC}"
        local env_dir
        env_dir=$(dirname "$full_env_path")
        mkdir -p "$env_dir"
        {
            echo "# Auto-generated from Doppler (${doppler_project} / ${doppler_config})"
            echo "# Generated: $(date '+%Y-%m-%d %H:%M:%S')"
            echo ""
            local has_overrides=0
            while IFS= read -r key; do
                local val
                val=$(lookup_value "$key" "$remote_file")
                if is_local_key "$key" "$config_name"; then
                    if [[ $has_overrides -eq 0 ]]; then
                        echo ""
                        echo "# [env-sync] Local override variables — set these for your environment:"
                        has_overrides=1
                    fi
                    printf '# %s="%s"\n' "$key" "$val"
                else
                    printf '%s="%s"\n' "$key" "$val"
                fi
            done < "$remote_keys"
        } > "$full_env_path"
        local count
        count=$(wc -l < "$remote_keys" | tr -d ' ')
        echo -e "${GREEN}✓ Created ${env_file} with $count variables${NC}"
        local lk_count
        lk_count=$(count_local_keys "$config_name")
        if [[ "$lk_count" -gt 0 ]]; then
            echo -e "${CYAN}  ⚙ ${lk_count} local override keys are commented out — update them for your environment${NC}"
        fi
        return
    fi

    local local_file="$TMPDIR_SYNC/local_parsed_${config_name}"
    parse_env_to_sorted_file "$full_env_path" "$local_file"

    local added=0 updated=0 kept=0 local_overrides=0
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M')

    backup_file "$full_env_path" "${REPO_ROOT}/.env-backups"

    local tmpout="$TMPDIR_SYNC/output_${config_name}"
    touch "$tmpout"
    local handled_file="$TMPDIR_SYNC/handled_keys_${config_name}"
    touch "$handled_file"

    while IFS= read -r line; do
        if echo "$line" | grep -q '^\s*#' || echo "$line" | grep -q '^\s*$'; then
            echo "$line" >> "$tmpout"
            continue
        fi

        local key="${line%%=*}"
        local local_val="${line#*=}"
        local_val="${local_val#\"}"
        local_val="${local_val%\"}"

        if key_exists "$key" "$remote_file"; then
            echo "$key" >> "$handled_file"
            if is_local_key "$key" "$config_name"; then
                echo "$line" >> "$tmpout"
                kept=$((kept + 1))
                local_overrides=$((local_overrides + 1))
                echo -e "  ${CYAN}⚙${NC} $key ${DIM}(local override — kept local value)${NC}"
            else
                local rval
                rval=$(lookup_value "$key" "$remote_file")
                if [[ "$local_val" != "$rval" ]]; then
                    echo "# [env-sync $timestamp] Previous value:" >> "$tmpout"
                    echo "# ${line}" >> "$tmpout"
                    printf '%s="%s"\n' "$key" "$rval" >> "$tmpout"
                    echo -e "  ${YELLOW}~${NC} $key ${DIM}(updated, old value preserved as comment)${NC}"
                    updated=$((updated + 1))
                else
                    echo "$line" >> "$tmpout"
                    kept=$((kept + 1))
                fi
            fi
        else
            echo "$line" >> "$tmpout"
            kept=$((kept + 1))
        fi
    done < "$full_env_path"

    local has_new=0 has_new_local=0
    while IFS= read -r key; do
        if ! grep -q "^${key}$" "$handled_file" 2>/dev/null && ! key_exists "$key" "$local_file"; then
            if is_local_key "$key" "$config_name"; then
                if [[ $has_new_local -eq 0 ]]; then
                    echo "" >> "$tmpout"
                    echo "# [env-sync $timestamp] Local override variables — set these for your environment:" >> "$tmpout"
                    has_new_local=1
                fi
                local rval
                rval=$(lookup_value "$key" "$remote_file")
                printf '# %s="%s"\n' "$key" "$rval" >> "$tmpout"
                echo -e "  ${CYAN}!${NC} $key ${DIM}(local override — added commented out)${NC}"
                local_overrides=$((local_overrides + 1))
            else
                if [[ $has_new -eq 0 ]]; then
                    echo "" >> "$tmpout"
                    echo "# [env-sync $timestamp] New variables from Doppler:" >> "$tmpout"
                    has_new=1
                fi
                local rval
                rval=$(lookup_value "$key" "$remote_file")
                printf '%s="%s"\n' "$key" "$rval" >> "$tmpout"
                echo -e "  ${GREEN}+${NC} $key ${DIM}(new from Doppler)${NC}"
                added=$((added + 1))
            fi
        fi
    done < "$remote_keys"

    mv "$tmpout" "$full_env_path"

    echo ""
    if [[ $added -eq 0 && $updated -eq 0 && $local_overrides -eq 0 ]]; then
        echo -e "${GREEN}Already in sync — no changes needed${NC}"
    else
        echo -e "  ${GREEN}Added:${NC}            $added new keys"
        echo -e "  ${YELLOW}Updated:${NC}          $updated changed keys (old values preserved as comments)"
        if [[ $local_overrides -gt 0 ]]; then
        echo -e "  ${CYAN}Local overrides:${NC}  $local_overrides ${DIM}(machine-specific, kept/commented)${NC}"
        fi
        echo -e "  ${DIM}Kept:             $kept unchanged${NC}"
        echo ""
        echo -e "${GREEN}✓ ${env_file} updated successfully${NC}"
    fi
}

cmd_pull() {
    local force="${1:-}"
    local configs
    configs=$(get_configs)
    while IFS= read -r config_name; do
        [[ -z "$config_name" ]] && continue
        validate_config_values "$config_name" || continue
        if [[ "$force" == "--force" ]]; then run_pull_force "$config_name"
        else run_pull_merge "$config_name"; fi
    done <<< "$configs"
}

# ─── Main ────────────────────────────────────────────────────────────────────

COMMAND="${1:-}"
EXTRA="${2:-}"

case "$COMMAND" in
    sync)   cmd_sync ;;
    push)   cmd_push "$EXTRA" ;;
    pull)   cmd_pull "$EXTRA" ;;
    diff)   cmd_diff ;;
    status) cmd_status ;;
    *)
        echo -e "Usage: $(basename "$0") {sync|push|pull|diff|status} [--force]"
        echo ""
        echo -e "  ${BOLD}sync${NC}             Interactive per-key conflict resolution (recommended)"
        echo -e "  push             Merge local vars into Doppler (add + update, never delete)"
        echo -e "  push --force     Full replace: upload local file to Doppler"
        echo -e "  pull             Merge Doppler vars into local (add + update, never delete)"
        echo -e "  pull --force     Full replace: download from Doppler"
        echo -e "  diff             Show differences between local and Doppler"
        echo -e "  status           Quick count summary"
        exit 1
        ;;
esac
