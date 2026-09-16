// Real, compiled SystemC testbench for StoragePipelineSubsystemModel,
// validated against templates/storage_pipeline_subsystem.template.yaml's
// test_scenarios -- the SystemC counterpart to
// tests/test_storage_pipeline_subsystem.py, run independently on its own
// toolchain. Expected values are taken directly from the reviewed SimPy
// model's own tests, per skills/systemc-model-generation/SKILL.md's
// two-sources-of-truth rule.

#include <systemc.h>

#include <iostream>
#include <string>
#include <vector>

#include "../models/storage_pipeline_subsystem.h"

namespace {
// Mirrors tests/test_storage_pipeline_subsystem.py's FAST_ARBITRATION /
// FAST_COMPLETION dicts, so scenarios settle within a short sc_start window.
ArbitrationLatencies fast_arbitration() {
    ArbitrationLatencies latencies;
    latencies.bitmap = 1;
    latencies.port_scan = 1;
    latencies.tenant_scan = 1;
    latencies.sq_scan = 1;
    latencies.grant = 1;
    latencies.selection_accept = 1;
    latencies.pending_count = 1;
    latencies.burst_read = 1;
    latencies.burst_calc = 1;
    latencies.issue = 1;
    latencies.burst_debit = 1;
    latencies.issue_slot = 1;
    return latencies;
}
CompletionLatencies fast_completion() {
    CompletionLatencies latencies;
    latencies.service = 1;
    latencies.tenant_select = 1;
    latencies.token_check = 1;
    latencies.emit = 1;
    latencies.retry = 1;
    latencies.port = 1;
    return latencies;
}
}  // namespace

struct TestResult {
    std::string name;
    bool passed = false;
    std::vector<std::string> failures;
};

inline std::string stringify(const std::string& value) { return value; }
inline std::string stringify(long value) { return std::to_string(value); }
inline std::string stringify(int value) { return std::to_string(value); }
inline std::string stringify(double value) { return std::to_string(value); }
inline std::string stringify(bool value) { return value ? "true" : "false"; }
inline std::string stringify(std::size_t value) { return std::to_string(value); }

#define CHECK_EQ(result, actual, expected, msg)                                                            \
    do {                                                                                                    \
        auto _actual = (actual);                                                                            \
        auto _expected = (expected);                                                                        \
        if (!(_actual == _expected)) {                                                                      \
            (result).failures.push_back(                                                                    \
                std::string(msg) + ": expected " + stringify(_expected) + " got " + stringify(_actual)      \
            );                                                                                               \
        }                                                                                                    \
    } while (0)

#define CHECK_GT(result, actual, floor, msg)                                                                \
    do {                                                                                                     \
        auto _actual = (actual);                                                                             \
        if (!(_actual > (floor))) {                                                                          \
            (result).failures.push_back(std::string(msg) + ": expected > " + stringify(floor) + " got " + stringify(_actual)); \
        }                                                                                                     \
    } while (0)

// ------------------------------------------------------------------------ //
// template test_scenarios[0]: command_flows_end_to_end.
// ------------------------------------------------------------------------ //
SC_MODULE(CommandFlowsEndToEndScenario) {
    StoragePipelineSubsystemModel subsystem;
    TestResult result;

    SC_CTOR(CommandFlowsEndToEndScenario) : subsystem("subsystem", fast_arbitration(), fast_completion()) {
        result.name = "command_flows_end_to_end";
        SC_THREAD(drive);
    }

    void drive() {
        sc_time submitted_at = sc_time_stamp();
        subsystem.submit({"c0", "READ", "port0", "T0", "SQ0", 4.0});
        wait(80, SC_NS);

        CHECK_EQ(result, subsystem.get_metrics()["commands_submitted"], 1L, "commands_submitted");
        CHECK_EQ(result, subsystem.get_metrics()["completion_submitted"], 1L, "completion_submitted");
        CHECK_EQ(result, subsystem.completion.completed.size(), static_cast<std::size_t>(1), "completed size");
        if (!subsystem.completion.completed.empty()) {
            CHECK_EQ(result, subsystem.completion.completed[0].second, std::string("c0"), "completed cmd_id");
            double completed_at = subsystem.completion.completed[0].first;
            CHECK_GT(result, completed_at, submitted_at.to_default_time_units(), "end_to_end_latency_measured");
        }
        CHECK_GT(result, subsystem.transition_counts["IDLE->ACCEPT_COMMAND"], 0L, "IDLE->ACCEPT_COMMAND");
        CHECK_GT(
            result, subsystem.transition_counts["ACCEPT_COMMAND->FORWARD_TO_ARBITRATION"], 0L,
            "ACCEPT_COMMAND->FORWARD_TO_ARBITRATION"
        );
        CHECK_GT(
            result, subsystem.transition_counts["POLL_ISSUED->FORWARD_TO_COMPLETION"], 0L,
            "POLL_ISSUED->FORWARD_TO_COMPLETION"
        );
        CHECK_GT(
            result, subsystem.transition_counts["FORWARD_TO_COMPLETION->POLL_ISSUED"], 0L,
            "FORWARD_TO_COMPLETION->POLL_ISSUED"
        );
        CHECK_GT(result, subsystem.transition_counts["POLL_ISSUED->POLL_ISSUED"], 0L, "POLL_ISSUED->POLL_ISSUED");
        CHECK_EQ(result, subsystem.fsm_state["intake_bridge"], std::string("IDLE"), "intake_bridge fsm_state");
        CHECK_EQ(result, subsystem.fsm_state["dispatch_bridge"], std::string("POLL_ISSUED"), "dispatch_bridge fsm_state");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[1]: qos_config_flows_to_completion_ip.
// ------------------------------------------------------------------------ //
SC_MODULE(QosConfigFlowsToCompletionIpScenario) {
    StoragePipelineSubsystemModel subsystem;
    TestResult result;

    SC_CTOR(QosConfigFlowsToCompletionIpScenario) : subsystem("subsystem", fast_arbitration(), fast_completion()) {
        result.name = "qos_config_flows_to_completion_ip";
        SC_THREAD(drive);
    }

    void drive() {
        subsystem.configure_qos("T0", 250.0);
        wait(10, SC_NS);

        CHECK_EQ(result, subsystem.get_metrics()["qos_configs_applied"], 1L, "qos_configs_applied");
        CHECK_EQ(result, subsystem.completion.tokens["T0"].read, 250.0, "tokens.read");
        CHECK_EQ(result, subsystem.completion.tokens["T0"].write, 250.0, "tokens.write");
        CHECK_EQ(result, subsystem.completion.tokens["T0"].read_bw, 250.0, "tokens.read_bw");
        CHECK_EQ(result, subsystem.completion.tokens["T0"].write_bw, 250.0, "tokens.write_bw");
        CHECK_GT(result, subsystem.transition_counts["IDLE->ACCEPT_QOS_CONFIG"], 0L, "IDLE->ACCEPT_QOS_CONFIG");
        CHECK_GT(
            result, subsystem.transition_counts["ACCEPT_QOS_CONFIG->APPLY_QOS_CONFIG"], 0L,
            "ACCEPT_QOS_CONFIG->APPLY_QOS_CONFIG"
        );
        CHECK_EQ(result, subsystem.fsm_state["intake_bridge"], std::string("IDLE"), "intake_bridge fsm_state");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[2]: completion_backlog_throttles_arbitration_issue_readiness.
// ------------------------------------------------------------------------ //
SC_MODULE(BacklogThrottlesIssueReadinessScenario) {
    StoragePipelineSubsystemModel subsystem;
    TestResult result;

    SC_CTOR(BacklogThrottlesIssueReadinessScenario)
        : subsystem("subsystem", fast_arbitration(), fast_completion(), 2) {
        result.name = "completion_backlog_throttles_arbitration_issue_readiness";
        SC_THREAD(drive);
    }

    void drive() {
        subsystem.completion.set_completion_ready(false);
        for (int i = 0; i < 5; i++) {
            subsystem.submit({"c" + std::to_string(i), "READ", "port0", "T0", "SQ0", 4.0});
        }
        wait(120, SC_NS);

        CHECK_EQ(result, subsystem.issue_throttled, true, "arbitration_ip_issue_ready_cleared_at_backlog_limit");
        CHECK_EQ(result, subsystem.arbitration.is_issue_ready(), false, "arbitration issue_ready");
        if (subsystem.get_metrics()["backpressure_events"] < 1) {
            result.failures.push_back("backpressure_events: expected >= 1");
        }
        CHECK_GT(
            result, subsystem.transition_counts["SAMPLE_BACKLOG->APPLY_THROTTLE"], 0L, "SAMPLE_BACKLOG->APPLY_THROTTLE"
        );
        CHECK_GT(
            result, subsystem.transition_counts["SAMPLE_BACKLOG->SAMPLE_BACKLOG"], 0L, "SAMPLE_BACKLOG->SAMPLE_BACKLOG"
        );

        subsystem.completion.set_completion_ready(true);
        wait(280, SC_NS);

        CHECK_EQ(
            result, subsystem.issue_throttled, false, "arbitration_ip_issue_ready_restored_below_backlog_limit"
        );
        CHECK_EQ(result, subsystem.arbitration.is_issue_ready(), true, "arbitration issue_ready restored");
        CHECK_EQ(result, subsystem.completion.completed.size(), static_cast<std::size_t>(5), "all 5 completed");
        result.passed = result.failures.empty();
    }
};

int sc_main(int argc, char* argv[]) {
    CommandFlowsEndToEndScenario s1("s1");
    QosConfigFlowsToCompletionIpScenario s2("s2");
    BacklogThrottlesIssueReadinessScenario s3("s3");

    sc_start(500, SC_NS);

    std::vector<TestResult*> results = {&s1.result, &s2.result, &s3.result};
    int failed = 0;
    for (auto* r : results) {
        if (r->passed) {
            std::cout << "PASS  " << r->name << std::endl;
        } else {
            failed++;
            std::cout << "FAIL  " << r->name << (r->failures.empty() ? " (did not complete)" : "") << std::endl;
            for (const auto& f : r->failures) {
                std::cout << "        " << f << std::endl;
            }
        }
    }
    std::cout << (static_cast<int>(results.size()) - failed) << "/" << results.size() << " passed" << std::endl;
    return failed == 0 ? 0 : 1;
}
