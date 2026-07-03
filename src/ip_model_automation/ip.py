"""IP registry and public exports for generated SimPy delay models."""

from __future__ import annotations

from .arbitration_ip import ArbitrationIpModel
from .axi_interconnect_ip import AxiInterconnectIpModel
from .common import Command, Descriptor, IP_ARTIFACTS, IpArtifact, list_ips, resolve_artifacts
from .completion_ip import CompletionIpModel
from .gdma_ip import GdmaIpModel
from .interrupt_controller_ip import InterruptControllerIpModel
from .mailbox_ip import MailboxIpModel
from .spi_master_ip import SpiMasterIpModel
from .timer_ip import TimerIpModel

__all__ = [
    "ArbitrationIpModel",
    "AxiInterconnectIpModel",
    "Command",
    "CompletionIpModel",
    "Descriptor",
    "GdmaIpModel",
    "IP_ARTIFACTS",
    "InterruptControllerIpModel",
    "IpArtifact",
    "MailboxIpModel",
    "SpiMasterIpModel",
    "TimerIpModel",
    "list_ips",
    "resolve_artifacts",
]
