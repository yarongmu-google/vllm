#!/usr/bin/env bash
# Forward baseline submission to the paired TPU inference checkout.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$(git -C "$ROOT" branch --show-current)" == bench ]] || {
  echo "Run this launcher from the bench branch" >&2; exit 2;
}
TPU_REPO="${TPU_INFERENCE_REPO:-$ROOT/../tpu-inference}"
if [[ ! -f "$TPU_REPO/tmp/baselines/run.sh" ]]; then
  echo "Set TPU_INFERENCE_REPO to the paired TPU inference bench checkout" >&2
  exit 2
fi
exec bash "$TPU_REPO/tmp/baselines/run.sh" "$@"
