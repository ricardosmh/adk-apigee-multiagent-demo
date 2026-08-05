#!/usr/bin/env bash
# Deploy the Apigee X proxy bundles (apigee/proxies/) to an org/environment.
#
# Imports each proxy's extracted apiproxy/ as a new revision, then deploys that
# revision to APIGEE_ENV (override=true). Uses the Apigee management API
# directly (curl + a gcloud access token) — no apigeecli dependency.
#
# Usage:
#   ./deploy_proxies.sh --all               # deploy every bundle in apigee/proxies/
#   ./deploy_proxies.sh gemini-llm-apiproxy mcp-server-apiproxy   # just these
#   (no args prints usage + the available bundle names)
#
# Config: everything defaults from the repo's canonical files —
# demo-environment.yaml (org == projects.apigee, env == apigee_env) and
# provision/manifest.yaml (meta.deploy_sa, meta.ecommerce_deploy_sa).
# apigee/.env is an OPTIONAL override (gitignored) — see .env.example.
#
# Requires: curl, jq, zip, and either APIGEE_TOKEN or a gcloud login.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/proxies"   # the 4 proxy bundles live here

# ── Proxy selection (before any env/tooling checks, so usage always prints) ──
# Explicit only: --all, or proxy names space-separated. --check = read-only
# deployment status (no import/deploy); with no names it checks every bundle.
CHECK=0
if [ "${1:-}" = "--check" ]; then
    CHECK=1; shift
    [ "$#" -eq 0 ] && set -- --all
fi
if [ "$#" -eq 0 ]; then
    echo "usage: ./deploy_proxies.sh [--check] --all | <proxy-name> [<proxy-name> ...]" >&2
    echo "  --check   show current deployments (read-only); no names = all bundles" >&2
    echo "bundles available:" >&2
    for d in "$PROXY_DIR"/*/apiproxy; do
        [ -d "$d" ] && echo "  $(basename "$(dirname "$d")")" >&2
    done
    exit 2
fi
if [ "$1" = "--all" ]; then
    PROXIES=()
    for d in "$PROXY_DIR"/*/apiproxy; do
        [ -d "$d" ] && PROXIES+=("$(basename "$(dirname "$d")")")
    done
    [ "${#PROXIES[@]}" -gt 0 ] || { echo "ERROR: no proxies found under apigee/proxies/." >&2; exit 1; }
else
    PROXIES=("$@")
    # Fail fast on typos BEFORE deploying anything.
    for proxy in "${PROXIES[@]}"; do
        [ -d "$PROXY_DIR/$proxy/apiproxy" ] \
            || { echo "ERROR: unknown proxy '$proxy' — no apigee/proxies/$proxy/apiproxy/." >&2; exit 1; }
    done
fi

# ── Config ───────────────────────────────────────────────────────────────────
# Defaults come from the canonical files; apigee/.env (or exported vars) is a
# pure OVERRIDE — same contract as the agents' .env files, never required.
ENV_FILE="$SCRIPT_DIR/../demo-environment.yaml"
[ -f "$ENV_FILE" ] || { echo "ERROR: demo-environment.yaml not found at repo root." >&2; exit 1; }
APIGEE_PROJECT="$(sed -n 's/^[[:space:]]*apigee:[[:space:]]*//p' "$ENV_FILE" | head -1 | awk '{print $1}')"
AI_PROJECT="$(sed -n 's/^[[:space:]]*ai:[[:space:]]*//p' "$ENV_FILE" | head -1 | awk '{print $1}')"
# regions.ai is the SECOND 'ai:' key in the file (projects.ai comes first).
AI_REGION="$(sed -n 's/^[[:space:]]*ai:[[:space:]]*//p' "$ENV_FILE" | sed -n '2p' | awk '{print $1}')"
APIGEE_ENV_DEFAULT="$(sed -n 's/^apigee_env:[[:space:]]*//p' "$ENV_FILE" | head -1 | awk '{print $1}')"
# meta.deploy_sa (the SA token-minting proxies run as) — anchored so it does
# NOT match meta.ecommerce_deploy_sa; ${projects.apigee} resolved like the
# manifest loader would.
DEPLOY_SA_DEFAULT="$(sed -n 's/^[[:space:]]*deploy_sa:[[:space:]]*//p' \
    "$SCRIPT_DIR/provision/manifest.yaml" | head -1 | awk '{print $1}')"
DEPLOY_SA_DEFAULT="${DEPLOY_SA_DEFAULT//\$\{projects.apigee\}/${APIGEE_PROJECT}}"

if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi
APIGEE_ORG="${APIGEE_ORG:-$APIGEE_PROJECT}"
APIGEE_ENV="${APIGEE_ENV:-$APIGEE_ENV_DEFAULT}"
APIGEE_DEPLOY_SA="${APIGEE_DEPLOY_SA:-$DEPLOY_SA_DEFAULT}"

: "${APIGEE_ORG:?could not derive the org — check projects.apigee in demo-environment.yaml}"
: "${APIGEE_ENV:?could not derive the env — check apigee_env in demo-environment.yaml}"

BINS="curl jq zip"; [ "$CHECK" = 1 ] && BINS="curl jq"   # --check never zips
for bin in $BINS; do
    command -v "$bin" >/dev/null || { echo "ERROR: '$bin' not found on PATH." >&2; exit 1; }
done

TOKEN="${APIGEE_TOKEN:-$(gcloud auth print-access-token 2>/dev/null || true)}"
[ -n "$TOKEN" ] || { echo "ERROR: no access token — set APIGEE_TOKEN or run 'gcloud auth login'." >&2; exit 1; }

BASE="https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG}"

# A proxy needs a deployment service account if its target mints a Google token
# (<GoogleAccessToken>/<GoogleIDToken>) or it writes to Cloud Logging
# (MessageLogging) — both run AS the deployment SA. Auto-detected from the XML so
# new proxies classify themselves. IMPORTANT: Apigee X does NOT inherit the
# previous deployment's SA on override, so a proxy that needs one must be given
# it every deploy or its auth silently breaks.
proxy_needs_sa() {  # $1 = proxy dir
    grep -rqE "<GoogleAccessToken>|<GoogleIDToken>" "$1/apiproxy/targets" 2>/dev/null && return 0
    grep -rqE "<MessageLogging" "$1/apiproxy/policies" 2>/dev/null && return 0
    return 1
}

# Per-proxy SA override: APIGEE_SA_<proxy, dashes->underscores>. The global
# APIGEE_DEPLOY_SA is only a default for proxies that NEED an SA — it is never
# attached to proxies that don't (least privilege); use the per-proxy override
# to force one onto such a proxy.
proxy_sa_override() {  # $1 = proxy name
    local key="APIGEE_SA_${1//-/_}"
    printf '%s' "${!key:-}"
}

# The ecommerce proxies deploy as ONE dedicated invoker SA, sourced from the
# MANIFEST (meta.ecommerce_deploy_sa) — single source of truth, no env vars.
# A per-proxy APIGEE_SA_* override still wins if set.
ECOMMERCE_SA="$(sed -n 's/^[[:space:]]*ecommerce_deploy_sa:[[:space:]]*//p' \
    "$SCRIPT_DIR/provision/manifest.yaml" | head -1 | awk '{print $1}')"
ECOMMERCE_SA="${ECOMMERCE_SA//\$\{projects.apigee\}/${APIGEE_PROJECT}}"
proxy_group_default() {  # $1 = proxy name
    case "$1" in
        ecommerce-*) printf '%s' "${ECOMMERCE_SA}" ;;
        *) printf '%s' "" ;;
    esac
}

echo "Org=${APIGEE_ORG}  Env=${APIGEE_ENV}"
echo "Proxies: ${PROXIES[*]}"
echo

# ── --check: current deployment status (read-only, then exit) ────────────────
if [ "$CHECK" = 1 ]; then
    for proxy in "${PROXIES[@]}"; do
        dep="$(curl -sS -H "Authorization: Bearer ${TOKEN}" \
            "${BASE}/environments/${APIGEE_ENV}/apis/${proxy}/deployments")"
        if printf '%s' "$dep" | jq -e '.error' >/dev/null 2>&1; then
            code="$(printf '%s' "$dep" | jq -r '.error.code')"
            if [ "$code" = "404" ]; then
                echo "  ✗ ${proxy}: NOT deployed in ${APIGEE_ENV}"
            else
                echo "  ✗ ${proxy}: $(printf '%s' "$dep" | jq -r '.error.message')" >&2
            fi
            continue
        fi
        printf '%s' "$dep" | jq -r --arg p "$proxy" '
            (.deployments[]? // .) |
            "  ✓ \($p): rev \(.revision // "?")" +
            "  since \((.deployStartTime // "0") | tonumber / 1000 | strftime("%Y-%m-%d %H:%M UTC"))" +
            "  SA: \(.serviceAccount // "none")" +
            (if .state and .state != "READY" then "  state: \(.state)" else "" end)'
    done
    exit 0
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Deploy logs: one repo-root home (deploy-logs/apigee/<proxy>.log), gitignored.
LOG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/deploy-logs/apigee"
mkdir -p "$LOG_DIR"

for proxy in "${PROXIES[@]}"; do
    dir="$PROXY_DIR/$proxy"
    if [ ! -d "$dir/apiproxy" ]; then
        echo "  ✗ ${proxy}: no apiproxy/ at ${dir} — skipping" >&2
        continue
    fi
    log="$LOG_DIR/${proxy}.log"
    printf '=== %s — %s ===\n' "$proxy" "$(date -u +%FT%TZ 2>/dev/null || date -u)" > "$log"
    echo ">>> ${proxy}  (log: ${log})"

    # Bundle: the zip's root must contain apiproxy/. Source trees carry
    # __TOKENS__ (project ids never live in the repo) — render a copy from
    # demo-environment.yaml values before zipping.
    render="$TMP/render-${proxy}"
    mkdir -p "$render" && cp -r "$dir/apiproxy" "$render/"
    if grep -rq "__AI_PROJECT_NUMBER__" "$render/apiproxy"; then
        [ -n "${AI_PROJECT_NUMBER:-}" ] || AI_PROJECT_NUMBER="$(gcloud projects describe "$AI_PROJECT" --format='value(projectNumber)')"
        [ -n "$AI_PROJECT_NUMBER" ] || { echo "  ✗ cannot resolve AI project number" >&2; exit 1; }
    fi
    find "$render/apiproxy" -type f -name '*.xml' -exec sed -i.bak \
        -e "s/__AI_PROJECT__/${AI_PROJECT}/g" \
        -e "s/__AI_REGION__/${AI_REGION}/g" \
        -e "s/__AI_PROJECT_NUMBER__/${AI_PROJECT_NUMBER:-}/g" {} +
    find "$render/apiproxy" -name '*.bak' -delete
    if grep -rq "__[A-Z_]*__" "$render/apiproxy"; then
        echo "  ✗ ${proxy}: unresolved __TOKEN__ after render:" >&2
        grep -rho "__[A-Z_]*__" "$render/apiproxy" | sort -u >&2
        exit 1
    fi
    zipf="$TMP/${proxy}.zip"
    ( cd "$render" && zip -qr "$zipf" apiproxy )

    # Import → new revision.
    imp="$(curl -sS -X POST -H "Authorization: Bearer ${TOKEN}" \
        -F "file=@${zipf}" \
        "${BASE}/apis?action=import&name=${proxy}")"
    { echo "-- import response:"; printf '%s\n' "$imp" | jq . 2>/dev/null || printf '%s\n' "$imp"; } >> "$log"
    rev="$(printf '%s' "$imp" | jq -r '.revision // empty')"
    if [ -z "$rev" ]; then
        echo "  ✗ ${proxy}: import failed (log: ${log}):" >&2
        printf '%s\n' "$imp" | jq . 2>/dev/null || printf '%s\n' "$imp"
        exit 1
    fi
    echo "    imported revision ${rev}; deploying to ${APIGEE_ENV}..."

    # Resolve the deployment service account (required for Google-token targets).
    sa_param=""
    override="$(proxy_sa_override "$proxy")"
    group_default="$(proxy_group_default "$proxy")"
    sa="${override:-${group_default:-${APIGEE_DEPLOY_SA:-}}}"
    if proxy_needs_sa "$dir"; then
        if [ -z "$sa" ]; then
            echo "  ✗ ${proxy}: needs a deployment service account (its target mints a" >&2
            echo "      Google access token), but none resolved — check meta.deploy_sa in" >&2
            echo "      provision/manifest.yaml, or override with APIGEE_DEPLOY_SA /" >&2
            echo "      APIGEE_SA_${proxy//-/_} (apigee/.env). Deploying without it strips" >&2
            echo "      the SA and breaks the proxy's auth to Vertex." >&2
            exit 1
        fi
        sa_param="&serviceAccount=${sa}"
        echo "    service account: ${sa}"
    elif [ -n "$override" ]; then
        # Doesn't need one, but a PER-PROXY override forces it — honor that.
        sa_param="&serviceAccount=${override}"
        echo "    service account: ${override} (per-proxy override)"
    else
        # Global APIGEE_DEPLOY_SA deliberately NOT attached here (least privilege).
        echo "    service account: none required"
    fi

    # Deploy, overriding any existing deployment of this proxy in the env.
    dep="$(curl -sS -X POST -H "Authorization: Bearer ${TOKEN}" \
        "${BASE}/environments/${APIGEE_ENV}/apis/${proxy}/revisions/${rev}/deployments?override=true${sa_param}")"
    { echo "-- deploy response:"; printf '%s\n' "$dep" | jq . 2>/dev/null || printf '%s\n' "$dep"; } >> "$log"
    if printf '%s' "$dep" | jq -e '.error' >/dev/null 2>&1; then
        echo "  ✗ ${proxy}: deploy failed (log: ${log}):" >&2
        printf '%s\n' "$dep" | jq .
        exit 1
    fi
    echo "  ✓ ${proxy} rev ${rev} deploying to ${APIGEE_ENV}"
    echo
done

echo "Done. Deployments may take a few seconds to become active."
