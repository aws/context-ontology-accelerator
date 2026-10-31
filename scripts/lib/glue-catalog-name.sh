# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

# Names of the federated Glue connections and catalogs a deployment's sources service creates,
# derived as build_catalog_name() (glue_connection_provisioner.py) derives them from
# RESOURCE_PREFIX="<prefix>-<env>-". Sourced by destroy.sh; a sources unit test pins the parity.

# "<sanitised prefix>ds_": lowercased, everything but [a-z0-9] dropped, as platform_catalog_prefix().
coa_glue_catalog_prefix() {
  printf '%sds_\n' "$(printf '%s-%s-' "$1" "$2" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')"
}

# The whole name as an ERE. build_catalog_name cuts the 16-hex digest to fit 41 characters, and
# prepends another "ds_" to a digit-leading prefix; no pattern (status 1) for those.
coa_glue_catalog_pattern() {
  local base digest
  base=$(coa_glue_catalog_prefix "$1" "$2")
  [[ "$base" =~ ^[a-z] ]] || return 1
  digest=$((41 - ${#base}))
  [ "$digest" -gt 16 ] && digest=16
  [ "$digest" -ge 1 ] || return 1
  printf '^%s[0-9a-f]{%d}$\n' "$base" "$digest"
}
