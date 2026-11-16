#!/usr/bin/env bash
# 一键复现整个培训实验：生成素材 -> 双实例五段实验 -> 对照图 -> 离线校验。
# 不修改任何源码；只在 training/evidence 下写入运行产物。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRAINING="$(cd "$HERE/.." && pwd)"
GO_BIN="${GO_BIN:-go}"

# 允许通过 GO_BIN 指定 go；若 PATH 中没有 go 且 /tmp/go-sdk 存在则自动使用
if ! command -v "$GO_BIN" >/dev/null 2>&1 && [ -x /tmp/go-sdk/go/bin/go ]; then
  GO_BIN=/tmp/go-sdk/go/bin/go
fi
echo "使用 Go: $($GO_BIN version)"

echo "== 1/4 生成自制 PNG 素材（字节可重复） =="
python3 -I "$HERE/make_png.py" "$TRAINING/assets"

echo "== 2/4 双实例五段实验（接入/冻结/退役/再冻结失败/跨实例） =="
python3 -I "$HERE/lab_retire_flow.py" --binary "$GO_BIN" --out "$TRAINING/evidence"

echo "== 3/4 生成图像对照 =="
python3 -I "$HERE/make_contact_sheet.py"

echo "== 4/4 离线核对全部证据 =="
python3 -I "$HERE/verify_evidence.py"

echo
echo "完成。请查看："
echo "  $TRAINING/manual/  培训手册（00-05）"
echo "  $TRAINING/evidence/contact-sheet.png  图像对照"
echo "  $TRAINING/evidence/steps/  每一步的原始 JSON/PNG 证据"
