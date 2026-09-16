// Real, compiled SystemC testbench for ArbitrationIpModel, validated against
// templates/arbitration_ip.template.yaml's test_scenarios -- the SystemC
// counterpart to tests/test_arbitration_ip.py, run independently on its own
// toolchain. Expected values (selection order, burst debits, cmd_ids) are
// taken directly from the reviewed SimPy model's own tests, per
// skills/systemc-model-generation/SKILL.md's two-sources-of-truth rule: the
// SimPy port already resolved these judgment calls once and this backend
// must not re-derive them from scratch.

#include <systemc.h>

#include <iostream>
#include <string>
#include <vector>

#include "../models/arbitration_ip.h"

struct TestResult {
    std::string name;
    bool passed = false;
    std::vector<std::string> failures;
};

inline std::string stringify(const std::string& value) { return value; }
inline std::string stringify(long value) { return std::to_string(value); }
inline std::string stringify(int value) { return std::to_string(value); }
inline std::string stringify(bool value) { return value ? "true" : "false"; }
inline std::string stringify(const std::vector<std::string>& values) {
    std::string out = "[";
    for (size_t i = 0; i < values.size(); i++) {
        if (i) {
            out += ", ";
        }
        out += values[i];
    }
    out += "]";
    return out;
}

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

// ------------------------------------------------------------------------ //
// template test_scenarios[0]: weighted_order_four_tenants.
//
// One command in every SQ; port alternation first (arbitration.hierarchy is
// [port, tenant, sq] with equal port weights), tenant weighting within a
// port. Pinned against tests/test_arbitration_ip.py's own
// test_arbitration_weighted_order_across_four_tenants, which measured this
// exact sequence rather than deriving it.
// ------------------------------------------------------------------------ //
SC_MODULE(WeightedOrderFourTenantsScenario) {
    ArbitrationIpModel arbiter;
    TestResult result;

    SC_CTOR(WeightedOrderFourTenantsScenario) : arbiter("arbiter_s1") {
        result.name = "weighted_order_four_tenants";
        SC_THREAD(drive);
    }

    void drive() {
        arbiter.enqueue("port0", "T0", "SQ0", "c0");
        arbiter.enqueue("port0", "T0", "SQ1", "c1");
        arbiter.enqueue("port0", "T1", "SQ0", "c2");
        arbiter.enqueue("port0", "T1", "SQ1", "c3");
        arbiter.enqueue("port0", "T1", "SQ2", "c4");
        arbiter.enqueue("port0", "T1", "SQ3", "c5");
        arbiter.enqueue("port1", "T2", "SQ0", "c6");
        arbiter.enqueue("port1", "T2", "SQ1", "c7");
        arbiter.enqueue("port1", "T3", "SQ0", "c8");

        wait(2000, SC_NS);

        std::vector<std::string> tenants;
        for (auto& entry : arbiter.get_selection_trace()) {
            tenants.push_back(entry[1]);
        }
        std::vector<std::string> expected = {"T0", "T2", "T1", "T2", "T1", "T3", "T1", "T1", "T0"};
        CHECK_EQ(result, tenants, expected, "selection order (tenant_id sequence)");
        CHECK_EQ(result, arbiter.get_metrics()["output_stalls"], 0L, "no_output_stall: output_stalls");
        CHECK_EQ(
            result, arbiter.get_metrics()["output_backpressure_cycles"], 0L,
            "no_output_stall: output_backpressure_cycles"
        );
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[1]: hierarchical_bitmap_selection.
// ------------------------------------------------------------------------ //
SC_MODULE(HierarchicalBitmapSelectionScenario) {
    ArbitrationIpModel arbiter;
    TestResult result;

    SC_CTOR(HierarchicalBitmapSelectionScenario) : arbiter("arbiter_s2") {
        result.name = "hierarchical_bitmap_selection";
        SC_THREAD(drive);
    }

    void drive() {
        arbiter.enqueue("port1", "T2", "SQ1", "only");
        wait(100, SC_NS);

        const auto& trace = arbiter.get_selection_trace();
        CHECK_EQ(result, static_cast<int>(trace.size()), 1, "exactly one selection");
        if (!trace.empty()) {
            CHECK_EQ(result, trace[0][0], std::string("port1"), "selected_port1");
            CHECK_EQ(result, trace[0][1], std::string("T2"), "selected_T2");
            CHECK_EQ(result, trace[0][2], std::string("SQ1"), "selected_SQ1");
        }
        const auto& issued = arbiter.get_issued_cmd_ids();
        CHECK_EQ(result, issued.size() == 1 && issued[0] == "only", true, "the command was issued");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[2]: burst_min_issue_count.
//
// Pipeline 2 uses min(pending, device burst, tenant burst, SQ burst) for
// downstream issue count. Values pinned against
// tests/test_arbitration_ip.py's test_arbitration_issue_pipeline_uses_min_burst_and_pending_count.
// ------------------------------------------------------------------------ //
SC_MODULE(BurstMinIssueCountScenario) {
    ArbitrationIpModel arbiter;
    TestResult result;

    SC_CTOR(BurstMinIssueCountScenario)
        : arbiter("arbiter_s3", 3, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 8, 1, 1, 10) {
        result.name = "burst_min_issue_count";
        SC_THREAD(drive);
    }

    void drive() {
        arbiter.configure_burst(6, {{"T1", 3}}, {{"T1", {{"SQ0", 5}}}});
        for (int i = 0; i < 8; i++) {
            arbiter.enqueue("port0", "T1", "SQ0", "cmd" + std::to_string(i));
        }

        wait(30, SC_NS);

        const auto& issued = arbiter.get_issued_cmd_ids();
        std::vector<std::string> expected = {"cmd0", "cmd1", "cmd2"};
        CHECK_EQ(result, issued, expected, "issued cmd_ids (issue_count_is_3)");
        CHECK_EQ(result, arbiter.get_metrics()["issued_commands"], 3L, "issued_commands");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[3]: downstream_backpressure.
// ------------------------------------------------------------------------ //
SC_MODULE(DownstreamBackpressureScenario) {
    ArbitrationIpModel arbiter;
    TestResult result;

    SC_CTOR(DownstreamBackpressureScenario) : arbiter("arbiter_s4") {
        result.name = "downstream_backpressure";
        SC_THREAD(drive);
    }

    void drive() {
        arbiter.set_issue_ready(false);
        arbiter.enqueue("port0", "T0", "SQ0", "held");

        wait(60, SC_NS);
        CHECK_EQ(result, arbiter.get_issued_cmd_ids().empty(), true, "no_pop_until_ready: nothing issued yet");
        if (arbiter.get_metrics()["output_stalls"] <= 0) {
            result.failures.push_back("output_stall_incremented: expected output_stalls > 0");
        }

        arbiter.set_issue_ready(true);
        wait(30, SC_NS);

        const auto& issued = arbiter.get_issued_cmd_ids();
        CHECK_EQ(result, issued.size() == 1 && issued[0] == "held", true, "held command issued once ready");
        result.passed = result.failures.empty();
    }
};

int sc_main(int argc, char* argv[]) {
    WeightedOrderFourTenantsScenario s1("s1");
    HierarchicalBitmapSelectionScenario s2("s2");
    BurstMinIssueCountScenario s3("s3");
    DownstreamBackpressureScenario s4("s4");

    sc_start(2100, SC_NS);

    std::vector<TestResult*> results = {&s1.result, &s2.result, &s3.result, &s4.result};
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
