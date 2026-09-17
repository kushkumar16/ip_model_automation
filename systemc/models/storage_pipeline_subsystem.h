#pragma once

#include <deque>
#include <map>
#include <string>
#include <vector>

#include <systemc.h>

#include "arbitration_ip.h"
#include "completion_ip.h"

// Pass-through latency knobs for the two member IPs -- glue timing comes
// from this subsystem's own template (hard-coded 1-2 cycle constants in the
// .cpp, matching timing_model.fsm_process_delays); each member IP's timing
// comes from its own reviewed template, and is overridable here the same
// way the SimPy model's arbitration_kwargs/completion_kwargs dicts are.
//
// Declared at namespace scope rather than nested in
// StoragePipelineSubsystemModel: a nested aggregate used as a constructor's
// default argument (`Foo foo = {}`) hits a GCC ordering quirk where the
// nested type's own default member initializers aren't considered complete
// yet at the point the enclosing constructor's default argument is parsed.
struct ArbitrationLatencies {
    int bitmap = 3;
    int port_scan = 4;
    int tenant_scan = 6;
    int sq_scan = 8;
    int grant = 2;
    int selection_accept = 1;
    int pending_count = 3;
    int burst_read = 4;
    int burst_calc = 2;
    int issue = 3;
    int burst_debit = 2;
    int weighted_order_rebuild = 8;
    int reset = 1;
    int issue_slot = 1;
    int credit_refill = 10;
};
struct CompletionLatencies {
    int service = 4;
    int tenant_select = 8;
    int token_check = 5;
    int emit = 4;
    int retry = 1;
    int refill_window = 5000000;
    int usage_assessment = 20;
    int base_refill = 10;
    int metrics_publish = 10;
    int port = 1;
    int reset = 1;
};

// SystemC delay model for storage_pipeline_subsystem, ported from the
// reviewed SimPy model (src/ip_model_automation/storage_pipeline_subsystem.py)
// against the same contract
// (templates/storage_pipeline_subsystem.template.yaml). Composes an
// ArbitrationIpModel and a CompletionIpModel sub-module with three glue
// SC_THREADs that bridge their boundaries, exactly mirroring the SimPy
// model's own intake_bridge/dispatch_bridge/backpressure_monitor. 1
// declared cycle = 1 SC_NS, the same convention every other model in this
// backend uses.
class StoragePipelineSubsystemModel : public sc_module {
  public:
    struct Command {
        std::string cmd_id;
        std::string kind;
        std::string port_id;
        std::string tenant_id;
        std::string sq_id;
        double size_kb = 4.0;
    };

    SC_HAS_PROCESS(StoragePipelineSubsystemModel);
    explicit StoragePipelineSubsystemModel(
        sc_module_name name, ArbitrationLatencies arbitration_latencies = {},
        CompletionLatencies completion_latencies = {}, int completion_backlog_limit = 4,
        double default_token_budget = 1'000'000.0
    );

    ArbitrationIpModel arbitration;
    CompletionIpModel completion;

    // Host-facing API (functionality_model.apis), mirroring the SimPy
    // model's public methods. Fire-and-forget from the caller's side, same
    // as every other host-facing command API in this backend: none of this
    // repo's SystemC testbenches await the SimPy model's returned accept
    // event either, they observe its downstream effect instead.
    void submit(const Command& command);
    void configure_qos(const std::string& tenant_id, double token_budget);

    std::map<std::string, long> get_metrics() const { return metrics; }

    std::map<std::string, std::string> fsm_state;
    std::map<std::string, long> transition_counts;
    bool issue_throttled = false;

  private:
    struct IntakeItem {
        bool is_command;
        Command command;
        std::string tenant_id;
        double token_budget;
    };

    void intake_bridge_process();
    void dispatch_bridge_process();
    void backpressure_monitor_process();

    int completion_backlog_limit;

    std::deque<IntakeItem> intake_queue;
    sc_event intake_notify;
    std::size_t issued_seen = 0;

    std::map<std::string, long> metrics;
};
