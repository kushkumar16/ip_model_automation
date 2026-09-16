#pragma once

#include <deque>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include <systemc.h>

#include "common.h"

// SystemC delay model for completion_ip, ported from the reviewed SimPy
// model (src/ip_model_automation/completion_ip.py) against the same
// contract (templates/completion_ip.template.yaml). 1 declared cycle = 1
// SC_NS, the same dimensionless-cycle convention the SimPy model's
// env.timeout(N) already uses.
class CompletionIpModel : public sc_module {
  public:
    struct Command {
        std::string cmd_id;
        std::string kind;  // READ, WRITE, or FLUSH
        std::string tenant_id;
        double size_kb = 0.0;
    };
    struct TokenBucket {
        double read = 0.0;
        double write = 0.0;
        double read_bw = 0.0;
        double write_bw = 0.0;
    };
    struct WindowSnapshot {
        double time = 0.0;
        double completed = 0.0;
    };

    SC_HAS_PROCESS(CompletionIpModel);
    explicit CompletionIpModel(
        sc_module_name name, int service_latency = 4, int tenant_select_latency = 8, int token_check_latency = 5,
        int emit_latency = 4, int retry_latency = 1, int refill_window = 5000000, int usage_assessment_latency = 20,
        int base_refill_latency = 10, int metrics_publish_latency = 10, int port_latency = 1, int reset_latency = 1,
        std::optional<int> pending_depth = std::nullopt, bool start_refill_process = false
    );

    // Host-facing API, mirroring the SimPy model's public methods.
    void configure_tenant(
        const std::string& tenant_id, double read, double write, double read_bw = 1000.0, double write_bw = 1000.0,
        int weight = 1, bool alive = true
    );
    void set_tenant_alive(const std::string& tenant_id, bool alive);
    void set_completion_ready(bool ready);
    // accepted_cmd_if's accept_command: fire-and-forget from the caller's
    // side, same as the SimPy tests' own usage of submit() (none of them
    // await the accept event it returns).
    void submit(const Command& command);
    // The whole-window shortcut for callers driving the model directly
    // (SimPy's refill_once()): performs all three declared refill actions
    // at once, without running the refill FSM's own timing.
    void refill_once();

    std::map<std::string, long> get_metrics() const { return metrics; }
    std::size_t pending_count(const std::string& tenant_id) const;
    // Sum of every touched tenant's pending queue length (SimPy:
    // `sum(len(queue) for queue in self.pending.values())`), for a consumer
    // (storage_pipeline_subsystem's backpressure monitor) that samples the
    // whole backlog rather than one tenant.
    std::size_t total_pending_count() const;

    std::map<std::string, std::string> fsm_state;
    std::set<std::string> eligible_tenants;
    std::map<std::string, TokenBucket> tokens;
    std::map<std::string, TokenBucket> base_tokens;
    std::vector<std::pair<double, std::string>> completed;  // (time, cmd_id)
    std::vector<WindowSnapshot> window_metrics;

  private:
    struct Costs {
        std::vector<std::pair<std::string, double>> items;
    };

    void accept_process();
    void completion_scheduler_process();
    void refill_process_impl();
    void release_output_port_process();

    void clear_state();
    void ensure_tenant(const std::string& tenant_id, int weight = 1);
    bool pending_queue_full(const std::string& tenant_id) const;
    static Costs costs_for(const Command& command);
    double& token_field(TokenBucket& bucket, const std::string& name);
    bool tenant_eligible(const std::string& tenant_id);
    void refresh_eligibility(const std::string& tenant_id);
    void refresh_all_eligibility();
    std::optional<std::string> select_tenant();
    bool can_pay(const std::string& tenant_id, const Costs& costs);
    void debit(const std::string& tenant_id, const Costs& costs);
    WindowSnapshot snapshot_window_usage();
    void restore_base_tokens();
    void write_window_metrics(const WindowSnapshot& snapshot);
    void wake_scheduler();

    std::map<std::string, sc_time> latency;

    std::optional<int> pending_depth;
    bool start_refill_process;

    std::map<std::string, int> tenant_weights;
    WeightedOrder tenant_order;
    std::map<std::string, bool> tenant_alive;
    std::map<std::string, std::deque<Command>> pending;
    bool output_ready = true;

    WindowSnapshot pending_snapshot;

    struct QueuedSubmission {
        Command command;
        sc_event* accepted;
    };
    std::deque<QueuedSubmission> input_queue;
    sc_event input_notify;

    bool completion_port_busy = false;
    sc_event completion_port_free;
    sc_event release_output_port_request;

    sc_event scheduler_wake_event;

    std::map<std::string, long> metrics;
};
