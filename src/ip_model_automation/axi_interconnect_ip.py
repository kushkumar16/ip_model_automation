from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

import simpy

from .common import Command, get_ip_logger


class AxiInterconnectIpModel:
    def __init__(
        self,
        env: simpy.Environment,
        decode_latency=12,
        arbitration_latency=14,
        slave_latency=4,
        response_latency=9,
        write_join_latency=10,
        error_latency=6,
        outstanding_limit=4,
        log_level: str = "WARNING",
        log_file: str = "run.log",
    ):
        self.env = env
        self.logger = get_ip_logger("axi_interconnect_ip", log_level, log_file)
        self.req_q = simpy.Store(env)
        self.aw_q = simpy.Store(env)
        self.w_q = simpy.Store(env)
        self.write_join_q = simpy.Store(env)
        self.write_resp_q = simpy.Store(env)
        self.ar_q = simpy.Store(env)
        self.read_resp_q = simpy.Store(env)
        self.decode_error_q = simpy.Store(env)
        self.routes: List[tuple[int, int, str]] = []
        self.responses: List[tuple[float, Command]] = []
        self.decode_errors = 0
        self.metrics = defaultdict(int)
        self.lat = {
            "decode": decode_latency,
            "arbitration": arbitration_latency,
            "slave": slave_latency,
            "response": response_latency,
            "write_join": write_join_latency,
            "error": error_latency,
        }
        self.outstanding_limit = outstanding_limit
        self.outstanding: Dict[str, Dict[str, object]] = {}
        self.slave_ready = defaultdict(lambda: True)
        self.pending_aw: Dict[str, tuple[float, Command, str]] = {}
        self.pending_w: Dict[str, Command] = {}
        self.fsm_state = {
            "write_address_ingress": "WAIT_AW",
            "write_data_ingress": "WAIT_W",
            "write_join_ordering": "WAIT_AW_AND_W",
            "slave_write_arbitration": "SCAN_MASTERS",
            "write_response_routing": "WAIT_B",
            "read_address_ingress": "WAIT_AR",
            "slave_read_arbitration": "SCAN_MASTERS",
            "read_data_routing": "WAIT_R",
            "decode_error_response": "WAIT_ERROR_REQ",
        }

        self.env.process(self.dispatch_process())
        self.env.process(self.write_address_ingress_process())
        self.env.process(self.write_data_ingress_process())
        self.env.process(self.write_join_ordering_process())
        self.env.process(self.slave_write_arbitration_process())
        self.env.process(self.write_response_routing_process())
        self.env.process(self.read_address_ingress_process())
        self.env.process(self.slave_read_arbitration_process())
        self.env.process(self.read_data_routing_process())
        self.env.process(self.decode_error_response_process())
        self.logger.info("initialized outstanding_limit=%s", self.outstanding_limit)

    def add_route(self, base: int, limit: int, slave_id: str) -> None:
        self.routes.append((base, limit, slave_id))
        self.logger.info("route added base=0x%x limit=0x%x slave=%s", base, limit, slave_id)

    def set_slave_ready(self, slave_id: str, ready: bool) -> None:
        self.slave_ready[slave_id] = ready
        self.logger.info("slave_ready slave=%s ready=%s", slave_id, ready)

    def submit(self, command: Command):
        self.logger.info("submit cmd=%s kind=%s addr=0x%x", command.cmd_id, command.kind, command.addr)
        return self.req_q.put((self.env.now, command))

    def submit_write_data(self, command: Command):
        self.logger.debug("submit write data cmd=%s", command.cmd_id)
        return self.w_q.put(command)

    def _decode(self, addr: int) -> Optional[str]:
        for base, limit, slave_id in self.routes:
            if base <= addr < limit:
                return slave_id
        return None

    def _response(self, command: Command, status: str) -> Command:
        return Command(
            command.cmd_id,
            command.kind,
            source_id=command.source_id,
            addr=command.addr,
            length=command.length,
            status=status,
        )

    def transact_functional(self, command: Command) -> Optional[Command]:
        slave = self._decode(command.addr)
        if slave is None:
            self.decode_errors += 1
            return None
        return self._response(command, f"{slave}:OK")

    def dispatch_process(self):
        while True:
            start, command = yield self.req_q.get()
            if command.kind == "WRITE":
                yield self.aw_q.put((start, command))
                yield self.w_q.put(command)
            else:
                yield self.ar_q.put((start, command))

    def _has_outstanding_credit(self) -> bool:
        return len(self.outstanding) < self.outstanding_limit

    def write_address_ingress_process(self):
        while True:
            self.fsm_state["write_address_ingress"] = "WAIT_AW"
            start, command = yield self.aw_q.get()
            self.fsm_state["write_address_ingress"] = "DECODE_ADDR"
            yield self.env.timeout(self.lat["decode"])
            slave = self._decode(command.addr)
            if slave is None:
                self.fsm_state["write_address_ingress"] = "AW_ERROR"
                self.logger.error("decode error write cmd=%s addr=0x%x", command.cmd_id, command.addr)
                yield self.decode_error_q.put((start, command))
                continue
            while not self._has_outstanding_credit():
                self.fsm_state["write_address_ingress"] = "AW_STALL"
                self.metrics["outstanding_stalls"] += 1
                self.logger.warning("outstanding stall write cmd=%s", command.cmd_id)
                yield self.env.timeout(1)
            self.fsm_state["write_address_ingress"] = "ENQUEUE_WRITE_REQ"
            self.pending_aw[command.cmd_id] = (start, command, slave)
            self.metrics["aw_captured"] += 1

    def write_data_ingress_process(self):
        while True:
            self.fsm_state["write_data_ingress"] = "WAIT_W"
            command = yield self.w_q.get()
            self.fsm_state["write_data_ingress"] = "BUFFER_W_BEAT"
            self.pending_w[command.cmd_id] = command
            self.metrics["w_beats_buffered"] += max(1, command.length)

    def write_join_ordering_process(self):
        while True:
            self.fsm_state["write_join_ordering"] = "WAIT_AW_AND_W"
            matched = False
            for cmd_id in list(self.pending_aw):
                if cmd_id not in self.pending_w:
                    continue
                start, command, slave = self.pending_aw.pop(cmd_id)
                self.pending_w.pop(cmd_id)
                self.fsm_state["write_join_ordering"] = "MERGE_WRITE"
                yield self.env.timeout(self.lat["write_join"])
                yield self.write_join_q.put((start, command, slave))
                self.metrics["write_joins"] += 1
                matched = True
                break
            if not matched:
                yield self.env.timeout(1)

    def slave_write_arbitration_process(self):
        while True:
            self.fsm_state["slave_write_arbitration"] = "SCAN_MASTERS"
            start, command, slave = yield self.write_join_q.get()
            yield self.env.timeout(self.lat["arbitration"])
            while not self.slave_ready[slave]:
                self.fsm_state["slave_write_arbitration"] = "SLAVE_BACKPRESSURE"
                self.metrics["slave_write_stalls"] += 1
                self.logger.warning("slave write backpressure slave=%s cmd=%s", slave, command.cmd_id)
                yield self.env.timeout(1)
            self.fsm_state["slave_write_arbitration"] = "GRANT_WRITE"
            self.outstanding[command.cmd_id] = {"start": start, "command": command, "slave": slave}
            yield self.env.timeout(self.lat["slave"])
            yield self.write_resp_q.put(command.cmd_id)
            self.metrics["write_grants"] += 1

    def write_response_routing_process(self):
        while True:
            self.fsm_state["write_response_routing"] = "WAIT_B"
            cmd_id = yield self.write_resp_q.get()
            self.fsm_state["write_response_routing"] = "LOOKUP_OUTSTANDING"
            yield self.env.timeout(self.lat["response"])
            entry = self.outstanding.pop(cmd_id)
            command = entry["command"]
            slave = entry["slave"]
            response = self._response(command, f"{slave}:OK")
            self.fsm_state["write_response_routing"] = "ROUTE_B"
            self.responses.append((self.env.now, response))
            self.metrics["write_latency"] = self.env.now - float(entry["start"])
            self.metrics["outstanding_depth"] = max(self.metrics["outstanding_depth"], len(self.outstanding))
            self.logger.info("write response routed cmd=%s status=%s time=%s", command.cmd_id, response.status, self.env.now)

    def read_address_ingress_process(self):
        while True:
            self.fsm_state["read_address_ingress"] = "WAIT_AR"
            start, command = yield self.ar_q.get()
            self.fsm_state["read_address_ingress"] = "DECODE_ADDR"
            yield self.env.timeout(self.lat["decode"])
            slave = self._decode(command.addr)
            if slave is None:
                self.fsm_state["read_address_ingress"] = "AR_ERROR"
                self.logger.error("decode error read cmd=%s addr=0x%x", command.cmd_id, command.addr)
                yield self.decode_error_q.put((start, command))
                continue
            while not self._has_outstanding_credit():
                self.fsm_state["read_address_ingress"] = "AR_STALL"
                self.metrics["outstanding_stalls"] += 1
                self.logger.warning("outstanding stall read cmd=%s", command.cmd_id)
                yield self.env.timeout(1)
            self.fsm_state["read_address_ingress"] = "ENQUEUE_READ_REQ"
            yield self.read_resp_q.put((start, command, slave))
            self.metrics["ar_captured"] += 1

    def slave_read_arbitration_process(self):
        while True:
            self.fsm_state["slave_read_arbitration"] = "SCAN_MASTERS"
            start, command, slave = yield self.read_resp_q.get()
            yield self.env.timeout(self.lat["arbitration"])
            while not self.slave_ready[slave]:
                self.fsm_state["slave_read_arbitration"] = "SLAVE_BACKPRESSURE"
                self.metrics["slave_read_stalls"] += 1
                self.logger.warning("slave read backpressure slave=%s cmd=%s", slave, command.cmd_id)
                yield self.env.timeout(1)
            self.fsm_state["slave_read_arbitration"] = "GRANT_READ"
            self.outstanding[command.cmd_id] = {"start": start, "command": command, "slave": slave}
            yield self.env.timeout(self.lat["slave"])
            yield self.read_data_routing_queue_put(command.cmd_id)
            self.metrics["read_grants"] += 1

    def read_data_routing_queue_put(self, cmd_id: str):
        return self.env.process(self._read_data_route_delay(cmd_id))

    def _read_data_route_delay(self, cmd_id: str):
        yield self.env.timeout(0)
        self.fsm_state["read_data_routing"] = "LOOKUP_OUTSTANDING"
        yield self.env.timeout(self.lat["response"])
        entry = self.outstanding.pop(cmd_id)
        command = entry["command"]
        slave = entry["slave"]
        response = self._response(command, f"{slave}:OK")
        self.fsm_state["read_data_routing"] = "ROUTE_R_BEAT"
        self.responses.append((self.env.now, response))
        self.metrics["read_latency"] = self.env.now - float(entry["start"])
        self.logger.info("read response routed cmd=%s status=%s time=%s", command.cmd_id, response.status, self.env.now)

    def read_data_routing_process(self):
        while True:
            self.fsm_state["read_data_routing"] = "WAIT_R"
            yield self.env.timeout(1)

    def decode_error_response_process(self):
        while True:
            self.fsm_state["decode_error_response"] = "WAIT_ERROR_REQ"
            start, command = yield self.decode_error_q.get()
            self.fsm_state["decode_error_response"] = "BUILD_ERROR_RESP"
            yield self.env.timeout(self.lat["error"])
            self.decode_errors += 1
            response = self._response(command, "DECERR")
            self.fsm_state["decode_error_response"] = "ROUTE_ERROR"
            self.responses.append((self.env.now, response))
            self.metrics["decode_errors"] += 1
            self.metrics[f"{command.kind.lower()}_latency"] = self.env.now - start
            self.logger.error("decode error response cmd=%s kind=%s time=%s", command.cmd_id, command.kind, self.env.now)
