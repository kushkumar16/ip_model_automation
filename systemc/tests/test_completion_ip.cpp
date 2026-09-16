// Real, compiled SystemC testbench for CompletionIpModel, validated against
// templates/completion_ip.template.yaml's test_scenarios -- the SystemC
// counterpart to tests/test_completion_ip.py, run independently on its own
// toolchain. Expected values are taken directly from the reviewed SimPy
// model's own tests, per skills/systemc-model-generation/SKILL.md's
// two-sources-of-truth rule.

#include <systemc.h>

#include <iostream>
#include <string>
#include <vector>

#include "../models/completion_ip.h"

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

// ------------------------------------------------------------------------ //
// template test_scenarios[0]: write_debits_write_budget.
// ------------------------------------------------------------------------ //
SC_MODULE(WriteDebitsWriteBudgetScenario) {
    CompletionIpModel model;
    TestResult result;

    SC_CTOR(WriteDebitsWriteBudgetScenario) : model("model_s1", 1, 1, 1, 1) {
        result.name = "write_debits_write_budget";
        SC_THREAD(drive);
    }

    void drive() {
        model.configure_tenant("T0", 2, 1, 8, 4);
        model.submit({"w0", "WRITE", "T0", 4.0});
        wait(10, SC_NS);

        CHECK_EQ(result, model.completed.size() == 1 && model.completed[0].second == "w0", true, "w0 completed");
        CHECK_EQ(result, model.tokens["T0"].write, 0.0, "write_iops_tokens_reduced");
        CHECK_EQ(result, model.tokens["T0"].write_bw, 0.0, "write_bw_tokens_reduced");
        CHECK_EQ(result, model.tokens["T0"].read, 2.0, "read_iops_tokens_unchanged");
        CHECK_EQ(result, model.tokens["T0"].read_bw, 8.0, "read_bw_tokens_unchanged");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[1]: token_starvation_blocks_completion.
// ------------------------------------------------------------------------ //
SC_MODULE(TokenStarvationScenario) {
    CompletionIpModel model;
    TestResult result;

    SC_CTOR(TokenStarvationScenario) : model("model_s2", 1, 1, 1, 1) {
        result.name = "token_starvation_blocks_completion";
        SC_THREAD(drive);
    }

    void drive() {
        model.configure_tenant("T0", 2, 0, 8, 0);
        model.submit({"w0", "WRITE", "T0", 4.0});
        wait(12, SC_NS);

        CHECK_EQ(result, model.completed.empty(), true, "completion_not_emitted");
        CHECK_EQ(
            result, model.eligible_tenants.count("T0") == 0, true, "tenant_excluded_from_eligible_set"
        );
        CHECK_EQ(result, model.get_metrics()["token_stalls"], 0L, "starved tenant must not be selected");

        model.base_tokens["T0"].write = 1.0;
        model.base_tokens["T0"].write_bw = 4.0;
        model.refill_once();
        wait(24, SC_NS);

        CHECK_EQ(result, model.completed.size() == 1 && model.completed[0].second == "w0", true, "w0 completed");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[2]: partial_tokens_reach_wait_tokens.
// ------------------------------------------------------------------------ //
SC_MODULE(PartialTokensScenario) {
    CompletionIpModel model;
    TestResult result;

    SC_CTOR(PartialTokensScenario) : model("model_s3", 1, 1, 1, 1) {
        result.name = "partial_tokens_reach_wait_tokens";
        SC_THREAD(drive);
    }

    void drive() {
        model.configure_tenant("T0", 5, 5, 2, 2);
        model.submit({"r0", "READ", "T0", 8.0});
        wait(20, SC_NS);

        CHECK_EQ(result, model.completed.empty(), true, "completion_not_emitted");
        CHECK_EQ(result, model.eligible_tenants.count("T0") == 1, true, "eligible despite insufficient bandwidth");
        if (model.get_metrics()["token_stalls"] <= 0) {
            result.failures.push_back("token_stall_incremented: expected token_stalls > 0");
        }
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[3]: output_backpressure_holds_completion.
// ------------------------------------------------------------------------ //
SC_MODULE(OutputBackpressureScenario) {
    CompletionIpModel model;
    TestResult result;

    SC_CTOR(OutputBackpressureScenario) : model("model_s4", 1, 1, 1, 1) {
        result.name = "output_backpressure_holds_completion";
        SC_THREAD(drive);
    }

    void drive() {
        model.configure_tenant("T0", 1, 1, 4, 4);
        model.set_completion_ready(false);
        model.submit({"r0", "READ", "T0", 4.0});
        wait(8, SC_NS);

        CHECK_EQ(result, model.completed.empty(), true, "no_pop_until_ready");
        CHECK_EQ(result, model.pending_count("T0"), static_cast<std::size_t>(1), "held in pending");
        if (model.get_metrics()["output_stalls"] <= 0) {
            result.failures.push_back("output_stall_incremented: expected output_stalls > 0");
        }
        CHECK_EQ(
            result, model.fsm_state["completion_scheduler"], std::string("STALL_OUTPUT"), "fsm_state"
        );

        model.set_completion_ready(true);
        wait(6, SC_NS);

        CHECK_EQ(result, model.completed.size() == 1 && model.completed[0].second == "r0", true, "r0 completed");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[4]: refill_restores_token_window.
// ------------------------------------------------------------------------ //
SC_MODULE(RefillRestoresTokenWindowScenario) {
    CompletionIpModel model;
    TestResult result;

    SC_CTOR(RefillRestoresTokenWindowScenario) : model("model_s5", 1, 1, 1, 1) {
        result.name = "refill_restores_token_window";
        SC_THREAD(drive);
    }

    void drive() {
        model.configure_tenant("T0", 2, 2, 8, 8);
        model.submit({"r0", "READ", "T0", 4.0});
        wait(8, SC_NS);
        CHECK_EQ(result, model.tokens["T0"].read, 1.0, "read tokens debited before refill");

        model.refill_once();

        CHECK_EQ(result, model.tokens["T0"].read, 2.0, "base_tokens_restored (read)");
        CHECK_EQ(result, model.tokens["T0"].read_bw, 8.0, "base_tokens_restored (read_bw)");
        CHECK_EQ(result, model.get_metrics()["refill_windows"], 1L, "refill_windows");
        CHECK_EQ(result, model.window_metrics.size(), static_cast<std::size_t>(1), "window_metric_snapshot_recorded");
        if (!model.window_metrics.empty()) {
            CHECK_EQ(result, model.window_metrics[0].completed, 1.0, "window snapshot completed count");
        }
        result.passed = result.failures.empty();
    }
};

int sc_main(int argc, char* argv[]) {
    WriteDebitsWriteBudgetScenario s1("s1");
    TokenStarvationScenario s2("s2");
    PartialTokensScenario s3("s3");
    OutputBackpressureScenario s4("s4");
    RefillRestoresTokenWindowScenario s5("s5");

    sc_start(60, SC_NS);

    std::vector<TestResult*> results = {&s1.result, &s2.result, &s3.result, &s4.result, &s5.result};
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
