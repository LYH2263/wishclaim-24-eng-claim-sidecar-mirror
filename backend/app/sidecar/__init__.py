"""Sidecar mirror: append-only claim/release 镜像仓。

- store.py     sidecar 存储（哈希链镜像行 + 重建履历）
- dualwrite.py 主库写操作的双写适配
- reconcile.py 对账报告与 append-only force 重建
"""
from app.sidecar import store  # noqa: F401
