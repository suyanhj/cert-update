#!/usr/bin/env bash
# 构建 cert-monitor 镜像并打双标签后推送：参数版本 + latest（新版本）
# 用法：./dk-build.sh <版本号> [服务名]
# 可选环境变量：IMAGE_BASE（不含标签，默认与 docker-compose.yml 中 image 一致）

set -eu

VERSION="${1:-}"
SERVICE="${2:-cert-monitor}"
IMG=

if [[ -z "$VERSION" ]]; then
  echo "示例: $0 1.2.3"
  echo "      IMAGE_BASE=registry.example.com/ns/app $0 2.0.0"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"


IMAGE_BASE="${IMAGE_BASE:-$IMG$SERVICE}"
IMAGE_LATEST="${IMAGE_BASE}:latest"
IMAGE_VERSIONED="${IMAGE_BASE}:${VERSION}"


docker build -t "$IMAGE_VERSIONED" .
docker tag "$IMAGE_VERSIONED" "$IMAGE_LATEST"
docker push "$IMAGE_VERSIONED"
docker push "$IMAGE_LATEST"

echo "[dk-build] 完成: 已推送 ${IMAGE_VERSIONED} 与 ${IMAGE_LATEST}"

