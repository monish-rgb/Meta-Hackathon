"""Importable FlowGate gateway client (see :mod:`flowgate_client.client`)."""

from flowgate_client.client import (
    FlowGateClientError,
    GatewayClient,
    _SyncFromAsyncTransport,
)

__all__ = ["FlowGateClientError", "GatewayClient", "_SyncFromAsyncTransport"]
