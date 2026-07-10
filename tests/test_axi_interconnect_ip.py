import unittest

import simpy

from ip_model_automation.ip import AxiInterconnectIpModel, Command


class TestAxiInterconnectIpModel(unittest.TestCase):
    def test_axi_decode_error(self):
        env = simpy.Environment()
        model = AxiInterconnectIpModel(env, decode_latency=1, error_latency=1)
        model.add_route(0x1000, 0x2000, "S0")
        model.submit(Command("r0", "READ", addr=0x3000))
        env.run(until=8)
        self.assertEqual(model.metrics["decode_errors"], 1)
        self.assertEqual(model.responses[0][1].status, "DECERR")

    def test_axi_routes_mapped_read_and_write(self):
        env = simpy.Environment()
        model = AxiInterconnectIpModel(env, decode_latency=1, arbitration_latency=1, slave_latency=1, response_latency=1, write_join_latency=1)
        model.add_route(0x1000, 0x2000, "S0")
        model.submit(Command("r0", "READ", addr=0x1004))
        model.submit(Command("w0", "WRITE", addr=0x1008))
        env.run(until=12)
        statuses = {response.cmd_id: response.status for _, response in model.responses}
        self.assertEqual(statuses["r0"], "S0:OK")
        self.assertEqual(statuses["w0"], "S0:OK")
        self.assertEqual(model.metrics["read_grants"], 1)
        self.assertEqual(model.metrics["write_grants"], 1)
        self.assertEqual(model.metrics["write_joins"], 1)

    def test_axi_write_backpressure_stalls_until_slave_ready(self):
        env = simpy.Environment()
        model = AxiInterconnectIpModel(env, decode_latency=1, arbitration_latency=1, slave_latency=1, response_latency=1, write_join_latency=1)
        model.add_route(0x1000, 0x2000, "S0")
        model.set_slave_ready("S0", False)
        model.submit(Command("w0", "WRITE", addr=0x1008))
        env.run(until=8)
        self.assertEqual(model.responses, [])
        self.assertGreater(model.metrics["slave_write_stalls"], 0)
        model.set_slave_ready("S0", True)
        env.run(until=14)
        self.assertEqual(model.responses[0][1].cmd_id, "w0")


    def test_axi_write_decode_error_returns_decerr(self):
        env = simpy.Environment()
        model = AxiInterconnectIpModel(env, decode_latency=1, error_latency=1, write_join_latency=1)
        model.add_route(0x1000, 0x2000, "S0")
        model.submit(Command("w0", "WRITE", addr=0x3000))
        env.run(until=8)
        self.assertEqual(model.metrics["decode_errors"], 1)
        self.assertEqual(model.responses[0][1].status, "DECERR")

    def test_axi_read_backpressure_stalls_until_slave_ready(self):
        env = simpy.Environment()
        model = AxiInterconnectIpModel(env, decode_latency=1, arbitration_latency=1, slave_latency=1, response_latency=1)
        model.add_route(0x1000, 0x2000, "S0")
        model.set_slave_ready("S0", False)
        model.submit(Command("r0", "READ", addr=0x1004))
        env.run(until=8)
        self.assertEqual(model.responses, [])
        self.assertGreater(model.metrics["slave_read_stalls"], 0)
        model.set_slave_ready("S0", True)
        env.run(until=14)
        self.assertEqual(model.responses[0][1].cmd_id, "r0")

    def test_axi_write_join_waits_for_late_write_data(self):
        env = simpy.Environment()
        model = AxiInterconnectIpModel(env, decode_latency=1, arbitration_latency=1, slave_latency=1, response_latency=1, write_join_latency=1)
        model.add_route(0x1000, 0x2000, "S0")
        command = Command("w0", "WRITE", addr=0x1008)
        model.aw_q.put((0, command))
        env.run(until=6)
        self.assertEqual(model.metrics["aw_captured"], 1)
        self.assertEqual(model.metrics["write_joins"], 0)
        model.submit_write_data(command)
        env.run(until=16)
        self.assertEqual(model.metrics["write_joins"], 1)
        self.assertEqual(model.responses[0][1].cmd_id, "w0")

    def test_axi_transact_functional_decodes_or_errors(self):
        env = simpy.Environment()
        model = AxiInterconnectIpModel(env)
        self.assertIsNone(model.transact_functional(Command("r0", "READ", addr=0x3000)))
        self.assertEqual(model.decode_errors, 1)
        model.add_route(0x1000, 0x2000, "S0")
        response = model.transact_functional(Command("r1", "READ", addr=0x1004))
        self.assertEqual(response.status, "S0:OK")


if __name__ == "__main__":
    unittest.main()
