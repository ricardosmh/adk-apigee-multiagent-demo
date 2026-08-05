#!/usr/bin/env bash
# Vendor the canonical a2a_common/ package into every agent's app/ directory.
#
# WHY: each engine deploys with `agent_engines.create(extra_packages=["./app"])`
# from its own dir, which bundles only that agent's app/. A repo-root sibling
# package would not ship. So we copy a2a_common/ into each app/ as
# app/a2a_common/ and import it as `from app.a2a_common import ...`.
#
# Run this after editing the canonical a2a_common/, and before deploying.
# The vendored copies are overwritten verbatim — never hand-edit them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/a2a_common"

AGENTS=(
  my_supervisor_agent
  my_order_agent
  my_product_agent
  my_customer_agent
)

for agent in "${AGENTS[@]}"; do
  dest="$ROOT/$agent/app/a2a_common"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp "$SRC"/*.py "$dest"/
  echo "vendored a2a_common -> agents/$agent/app/a2a_common"
done

echo "done. (vendored copies are generated — edit a2a_common/ only)"
