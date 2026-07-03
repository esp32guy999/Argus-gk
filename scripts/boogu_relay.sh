#!/bin/bash
# Route around anvil's slow HF path: download the Boogu weights on glassgarden (11 MB/s
# home-ISP egress) then rsync them to anvil over the LAN (~gigabit). ~11x faster than
# anvil pulling HF directly (~1 MB/s through the Tailscale exit hairpin).
set -euo pipefail
BASE="https://huggingface.co/Comfy-Org/Boogu-Image/resolve/main"
GG="/mnt/user/downloads/boogu"
declare -A DEST=(
  [boogu_image_edit_fp8_scaled.safetensors]=/home/shane/ComfyUI/models/diffusion_models
  [qwen3vl_8b_fp8_scaled.safetensors]=/home/shane/ComfyUI/models/text_encoders
)
declare -A SRC=(
  [boogu_image_edit_fp8_scaled.safetensors]=diffusion_models/boogu_image_edit_fp8_scaled.safetensors
  [qwen3vl_8b_fp8_scaled.safetensors]=text_encoders/qwen3vl_8b_fp8_scaled.safetensors
)
ssh -o ConnectTimeout=10 unraid "mkdir -p $GG"
for fn in "${!SRC[@]}"; do
  echo "[boogu-relay] downloading $fn on gg…"
  ssh -o ConnectTimeout=10 unraid "curl -fL -C - --retry 5 --retry-delay 5 -o '$GG/$fn' '$BASE/${SRC[$fn]}'"
  echo "[boogu-relay] rsync $fn -> anvil ${DEST[$fn]}"
  mkdir -p "${DEST[$fn]}"
  rsync -a --inplace "unraid:$GG/$fn" "${DEST[$fn]}/"
  echo "[boogu-relay] landed: $(ls -lh "${DEST[$fn]}/$fn" | awk '{print $5}')"
done
# tidy the gg staging copies
ssh -o ConnectTimeout=10 unraid "rm -f $GG/*.safetensors; rmdir $GG 2>/dev/null || true"
echo "[boogu-relay] DONE — both weights on anvil"
/home/shane/.local/bin/ha-remind "🎨 Boogu Edit weights landed on anvil (via gg relay). Ready for the GPU bench vs Qwen-Image-Edit." || true
