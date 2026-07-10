from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

import simpy

from .common import Command, WeightedOrder, get_ip_logger


TOKEN_NAMES = ("read", "write", "read_bw", "write_bw")


class CompletionIpModel:
    def __init__(
        self,
        env: simpy.Environment,
        service_latency=4,
        tenant_select_latency=8,
        token_check_latency=5,
        emit_latency=4,
        retry_latency=1,
        refill_window=5_000_000,
        write_token_mode="WWV",
        wwv_write=3.0,
        wwv_read=2.0,
        tenant_weights: Optional[Dict[str, int]] = None,
        start_refill_process=False,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("completion_ip", log_level, log_file)
        self.service_latency = service_latency
        self.tenant_select_latency = tenant_select_latency
        self.token_check_latency = token_check_latency
        self.emit_latency = emit_latency
        self.retry_latency = retry_latency
        self.refill_window = refill_window
        self.write_token_mode = write_token_mode
        self.wwv_ratio = float(wwv_write) / float(wwv_read)

        self.input_q = simpy.Store(env)
        self.pending: Dict[str, Deque[Command]] = defaultdict(deque)
        self.tenant_weights = dict(tenant_weights or {"T0": 1})
        self.tenant_order = WeightedOrder(self.tenant_weights)
        self.tokens = defaultdict(self._new_token_bucket)
        self.base_tokens = defaultdict(self._new_token_bucket)
        self.burst_tokens = defaultdict(self._new_token_bucket)
        self.burst_max = defaultdict(self._new_token_bucket)
        self.limit_type = defaultdict(lambda: "SOFT")
        self.tenant_alive = defaultdict(lambda: True)
        self.output_ready = True
        self.completed: List[tuple[float, Command]] = []
        self.metrics = defaultdict(int)
        self.window_metrics: List[Dict[str, float]] = []

        self.env.process(self.accept_process())
        self.env.process(self.completion_scheduler())
        if start_refill_process:
            self.env.process(self.refill_process())
        self.logger.info("initialized write_token_mode=%s", self.write_token_mode)

    @staticmethod
    def _new_token_bucket() -> Dict[str, float]:
        return {name: 0.0 for name in TOKEN_NAMES}

    def configure_tenant(
        self,
        tenant_id: str,
        read: float,
        write: float,
        read_bw: float = 1000.0,
        write_bw: float = 1000.0,
        limit_type: str = "SOFT",
        burst_read: float = 0.0,
        burst_write: float = 0.0,
        burst_read_bw: float = 0.0,
        burst_write_bw: float = 0.0,
        weight: int = 1,
        alive: bool = True,
    ) -> None:
        self._ensure_tenant(tenant_id, weight)
        base = {"read": read, "write": write, "read_bw": read_bw, "write_bw": write_bw}
        burst = {
            "read": burst_read,
            "write": burst_write,
            "read_bw": burst_read_bw,
            "write_bw": burst_write_bw,
        }
        self.tokens[tenant_id].update(base)
        self.base_tokens[tenant_id].update(base)
        self.burst_tokens[tenant_id].update(burst)
        self.burst_max[tenant_id].update(burst)
        self.limit_type[tenant_id] = limit_type.upper()
        self.tenant_alive[tenant_id] = alive
        self.logger.info("configured tenant=%s limit=%s alive=%s", tenant_id, self.limit_type[tenant_id], alive)

    def _ensure_tenant(self, tenant_id: str, weight: int = 1) -> None:
        if tenant_id not in self.tenant_weights:
            self.tenant_weights[tenant_id] = max(1, int(weight))
            self.tenant_order = WeightedOrder(self.tenant_weights)

    def set_completion_ready(self, ready: bool) -> None:
        self.output_ready = ready
        self.logger.info("completion_ready=%s", ready)

    def submit(self, command: Command):
        self._ensure_tenant(command.tenant_id)
        self.logger.info("submit cmd=%s kind=%s tenant=%s", command.cmd_id, command.kind, command.tenant_id)
        return self.input_q.put(command)

    def accept_process(self):
        while True:
            command = yield self.input_q.get()
            yield self.env.timeout(self.service_latency)
            if not self.tenant_alive[command.tenant_id]:
                self.metrics["tenant_inactive_stalls"] += 1
                self.pending[command.tenant_id].append(command)
                self.logger.warning("tenant inactive cmd=%s tenant=%s", command.cmd_id, command.tenant_id)
                continue
            self.pending[command.tenant_id].append(command)
            self.metrics["accepted_commands"] += 1
            self.logger.debug("accepted cmd=%s tenant=%s time=%s", command.cmd_id, command.tenant_id, self.env.now)

    def _costs(self, command: Command) -> Dict[str, float]:
        if command.kind == "READ":
            return {"read": 1.0, "read_bw": float(command.size_kb)}
        if command.kind == "FLUSH":
            return {}
        if self.write_token_mode == "WWV":
            return {
                "write": 1.0,
                "write_bw": float(command.size_kb),
                "read": self.wwv_ratio,
                "read_bw": float(command.size_kb) * self.wwv_ratio,
            }
        return {"write": 1.0, "write_bw": float(command.size_kb)}

    def _select_tenant(self) -> Optional[str]:
        for tenant_id in self.tenant_order.scan():
            if self.pending[tenant_id]:
                self.tenant_order.advance_to_after(tenant_id)
                return tenant_id
        for tenant_id in sorted(self.pending):
            if self.pending[tenant_id]:
                return tenant_id
        return None

    def _available(self, tenant_id: str, token_name: str) -> float:
        available = self.tokens[tenant_id][token_name]
        if self.limit_type[tenant_id] == "SOFT":
            available += self.burst_tokens[tenant_id][token_name]
        return available

    def _can_pay(self, tenant_id: str, costs: Dict[str, float]) -> bool:
        return all(self._available(tenant_id, name) >= cost for name, cost in costs.items())

    def _debit(self, tenant_id: str, costs: Dict[str, float]) -> None:
        for name, cost in costs.items():
            base_debit = min(self.tokens[tenant_id][name], cost)
            self.tokens[tenant_id][name] -= base_debit
            remaining = cost - base_debit
            if remaining > 0.0 and self.limit_type[tenant_id] == "SOFT":
                self.burst_tokens[tenant_id][name] -= remaining
                self.metrics["burst_debits"] += 1

    def step_functional(self) -> Optional[Command]:
        tenant_id = self._select_tenant()
        if tenant_id is None:
            return None
        command = self.pending[tenant_id][0]
        if not self.tenant_alive[tenant_id]:
            self.metrics["tenant_inactive_stalls"] += 1
            self.logger.warning("tenant inactive stall tenant=%s", tenant_id)
            return None
        if not self.output_ready:
            self.metrics["output_stalls"] += 1
            self.logger.warning("output stall tenant=%s", tenant_id)
            return None
        costs = self._costs(command)
        if not self._can_pay(tenant_id, costs):
            self.metrics["token_stalls"] += 1
            self.logger.warning("token stall tenant=%s cmd=%s costs=%s", tenant_id, command.cmd_id, costs)
            return None
        self._debit(tenant_id, costs)
        return self.pending[tenant_id].popleft()

    def completion_scheduler(self):
        while True:
            yield self.env.timeout(self.tenant_select_latency)
            tenant_id = self._select_tenant()
            if tenant_id is None:
                self.metrics["stalls"] += 1
                yield self.env.timeout(self.retry_latency)
                continue

            command = self.pending[tenant_id][0]
            yield self.env.timeout(self.token_check_latency)
            if not self.tenant_alive[tenant_id]:
                self.metrics["tenant_inactive_stalls"] += 1
                self.logger.warning("tenant inactive stall tenant=%s", tenant_id)
                yield self.env.timeout(self.retry_latency)
                continue
            if not self.output_ready:
                self.metrics["output_stalls"] += 1
                self.logger.warning("output stall tenant=%s", tenant_id)
                yield self.env.timeout(self.retry_latency)
                continue

            costs = self._costs(command)
            if not self._can_pay(tenant_id, costs):
                self.metrics["token_stalls"] += 1
                self.logger.warning("token stall tenant=%s cmd=%s costs=%s", tenant_id, command.cmd_id, costs)
                yield self.env.timeout(self.retry_latency)
                continue

            self._debit(tenant_id, costs)
            self.pending[tenant_id].popleft()
            yield self.env.timeout(self.emit_latency)
            self.completed.append((self.env.now, command))
            self.metrics[f"completed_{command.kind.lower()}"] += 1
            self.metrics["completed_commands"] += 1
            self.logger.info("completed cmd=%s kind=%s tenant=%s time=%s", command.cmd_id, command.kind, tenant_id, self.env.now)

    def refill_once(self) -> None:
        snapshot = {"time": float(self.env.now), "completed": float(self.metrics["completed_commands"])}
        for tenant_id, base in self.base_tokens.items():
            if self.limit_type[tenant_id] == "SOFT":
                # APPLY_BURST: unused base tokens roll over into the burst
                # pool, capped at the configured burst maximum — so the
                # leftover must be read before REFILL_BASE overwrites it.
                for name in TOKEN_NAMES:
                    leftover = max(0.0, self.tokens[tenant_id][name])
                    self.burst_tokens[tenant_id][name] = min(
                        self.burst_max[tenant_id][name],
                        self.burst_tokens[tenant_id][name] + leftover,
                    )
            self.tokens[tenant_id].update(base)
        self.window_metrics.append(snapshot)
        self.metrics["refill_windows"] += 1
        self.logger.info("refill window time=%s", self.env.now)

    def refill_process(self):
        while True:
            yield self.env.timeout(self.refill_window)
            yield self.env.timeout(20)
            yield self.env.timeout(10)
            yield self.env.timeout(20)
            yield self.env.timeout(10)
            self.refill_once()
