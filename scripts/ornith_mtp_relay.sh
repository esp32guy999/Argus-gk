#!/bin/bash
set -euo pipefail
REPO="vcruz305/Ornith-1.0-35B-AEON-Ultimate-Uncensored-GGUF"
FILE="ornith-aeon-35b-MTP-Q4_K_M.gguf"
URL="https://huggingface.co/$REPO/resolve/main/$FILE"
GG="/mnt/user/downloads/ornith-mtp"
DEST="/home/shane/models/ornith-35b-uncensored"
ssh -o ConnectTimeout=10 unraid "mkdir -p $GG; curl -fL -C - --retry 5 --retry-delay 5 -o '$GG/$FILE' '$URL'"
mkdir -p "$DEST"; rsync -a --inplace "unraid:$GG/$FILE" "$DEST/"
ssh -o ConnectTimeout=10 unraid "rm -f $GG/$FILE; rmdir $GG 2>/dev/null || true"
/home/shane/.local/bin/ha-remind "🔀 Ornith-35B MTP variant landed ($(ls -lh "$DEST/$FILE" | awk '{print $5}')). Ready for the spec-decode A/B." || true
