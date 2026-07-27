from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List


@dataclass(frozen=True)
class IpArtifact:
    name: str
    dld: str
    template: str


IP_ARTIFACTS: Dict[str, IpArtifact] = {
    "arbitration_ip": IpArtifact(
        "arbitration_ip", "dlds/arbitration_ip_dld.md", "templates/arbitration_ip.template.yaml"
    ),
    "completion_ip": IpArtifact("completion_ip", "dlds/completion_ip_dld.md", "templates/completion_ip.template.yaml"),
    "gdma_ip": IpArtifact("gdma_ip", "dlds/gdma_ip_dld.md", "templates/gdma_ip.template.yaml"),
    "timer_ip": IpArtifact("timer_ip", "dlds/timer_ip_dld.md", "templates/timer_ip.template.yaml"),
    "axi_interconnect_ip": IpArtifact(
        "axi_interconnect_ip", "dlds/axi_interconnect_ip_dld.md", "templates/axi_interconnect_ip.template.yaml"
    ),
    "interrupt_controller_ip": IpArtifact(
        "interrupt_controller_ip",
        "dlds/interrupt_controller_ip_dld.md",
        "templates/interrupt_controller_ip.template.yaml",
    ),
    "mailbox_ip": IpArtifact("mailbox_ip", "dlds/mailbox_ip_dld.md", "templates/mailbox_ip.template.yaml"),
    "spi_master_ip": IpArtifact("spi_master_ip", "dlds/spi_master_ip_dld.md", "templates/spi_master_ip.template.yaml"),
    "i3c_ip": IpArtifact("i3c_ip", "dlds/i3c_ip_dld.md", "templates/i3c_ip.template.yaml"),
    "dma_subsystem": IpArtifact("dma_subsystem", "dlds/dma_subsystem_dld.md", "templates/dma_subsystem.template.yaml"),
    "mailbox_irq_subsystem": IpArtifact(
        "mailbox_irq_subsystem",
        "dlds/mailbox_irq_subsystem_dld.md",
        "templates/mailbox_irq_subsystem.template.yaml",
    ),
    "sram_ctrl_ip": IpArtifact("sram_ctrl_ip", "dlds/sram_ctrl_ip_dld.md", "templates/sram_ctrl_ip.template.yaml"),
}


def list_ips() -> Iterable[str]:
    return tuple(IP_ARTIFACTS.keys())


def resolve_artifacts(repo_root: Path, name: str) -> Dict[str, Path]:
    artifact = IP_ARTIFACTS[name]
    return {
        "dld": repo_root / artifact.dld,
        "template": repo_root / artifact.template,
    }


@dataclass
class Command:
    cmd_id: str
    kind: str
    port_id: str = "port0"
    tenant_id: str = "T0"
    sq_id: str = "SQ0"
    source_id: str = "S0"
    size_kb: int = 4
    priority: int = 0
    addr: int = 0
    length: int = 1
    target_id: str = "CPU0"
    status: str = "OK"


@dataclass
class Descriptor:
    desc_id: str
    channel_id: int = 0
    src_addr: int = 0
    dst_addr: int = 0
    length_bytes: int = 4096
    interrupt: bool = True


class WeightedOrder:
    def __init__(self, weights: Dict[str, int]):
        if not weights:
            raise ValueError("weights must not be empty")
        self.order: List[str] = []
        for name, weight in weights.items():
            if weight > 0:
                self.order.extend([name] * int(weight))
        if not self.order:
            raise ValueError("at least one weight must be positive")
        self.index = 0

    def scan(self):
        start = self.index
        for offset in range(len(self.order)):
            yield self.order[(start + offset) % len(self.order)]

    def advance_to_after(self, selected: str) -> None:
        for offset in range(len(self.order)):
            idx = (self.index + offset) % len(self.order)
            if self.order[idx] == selected:
                self.index = idx + 1
                return


class IpLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("ip_name", self.extra["ip_name"])
        return msg, kwargs


def get_ip_logger(ip_name: str, level: str | int = "INFO", log_file: str | Path = "run.log") -> IpLoggerAdapter:
    logger = logging.getLogger(f"ip_model_automation.{ip_name}")
    logger.setLevel(_normalize_log_level(level))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(ip_name)s] %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(_normalize_log_level(level))
    logger.addHandler(stream_handler)

    log_path = Path(log_file)
    if log_path.parent != Path("."):
        log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(_normalize_log_level(level))
    logger.addHandler(file_handler)

    return IpLoggerAdapter(logger, {"ip_name": ip_name})


def _normalize_log_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    normalized = level.upper()
    if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        raise ValueError(f"unsupported log level: {level}")
    return getattr(logging, normalized)
