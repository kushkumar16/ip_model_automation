"""IP registry and public exports for generated SimPy delay models."""

from __future__ import annotations

from .arbitration_ip import ArbitrationIpModel
from .axi_interconnect_ip import AxiInterconnectIpModel
from .common import IP_ARTIFACTS, Command, Descriptor, IpArtifact, list_ips, resolve_artifacts
from .completion_ip import CompletionIpModel
from .dma_subsystem import DmaSubsystemModel
from .gdma_ip import GdmaIpModel
from .i3c_ip import I3cIpModel
from .interrupt_controller_ip import InterruptControllerIpModel
from .mailbox_ip import MailboxIpModel
from .mailbox_irq_subsystem import MailboxIrqSubsystemModel
from .spi_master_ip import SpiMasterIpModel
from .sram_ctrl_ip import SramCtrlIpModel
from .timer_ip import TimerIpModel

__all__ = [
    "ArbitrationIpModel",
    "AxiInterconnectIpModel",
    "Command",
    "CompletionIpModel",
    "Descriptor",
    "DmaSubsystemModel",
    "GdmaIpModel",
    "I3cIpModel",
    "IP_ARTIFACTS",
    "InterruptControllerIpModel",
    "IpArtifact",
    "MailboxIpModel",
    "MailboxIrqSubsystemModel",
    "SpiMasterIpModel",
    "SramCtrlIpModel",
    "TimerIpModel",
    "list_ips",
    "resolve_artifacts",
]
