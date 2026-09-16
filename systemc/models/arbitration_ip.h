#pragma once

#include <array>
#include <deque>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include <systemc.h>

#include "common.h"

// SystemC delay model for arbitration_ip, ported from the reviewed SimPy
// model (src/ip_model_automation/arbitration_ip.py) against the same
// contract (templates/arbitration_ip.template.yaml). 1 declared cycle = 1
// SC_NS, the same dimensionless-cycle convention the SimPy model's
// env.timeout(N) already uses.
class ArbitrationIpModel : public sc_module {
  public:
    // port -> tenant -> [sq...]. Defaults to the standalone pilot's own
    // 2-port/4-tenant topology; a caller composing this model into a larger
    // subsystem (storage_pipeline_subsystem) injects a smaller one --
    // mirroring the SimPy model's own `topology` constructor argument.
    using Topology = std::map<std::string, std::map<std::string, std::vector<std::string>>>;

    SC_HAS_PROCESS(ArbitrationIpModel);
    explicit ArbitrationIpModel(
        sc_module_name name,
        int bitmap_latency = 3,
        int port_scan_latency = 4,
        int tenant_scan_latency = 6,
        int sq_scan_latency = 8,
        int grant_latency = 2,
        int selection_accept_latency = 1,
        int pending_count_latency = 3,
        int burst_read_latency = 4,
        int burst_calc_latency = 2,
        int issue_latency = 3,
        int burst_debit_latency = 2,
        int weighted_order_rebuild_latency = 8,
        int reset_latency = 1,
        int issue_slot_latency = 1,
        int credit_refill_latency = 10,
        std::optional<Topology> topology_override = std::nullopt,
        std::optional<std::map<std::string, int>> tenant_weight_override = std::nullopt
    );

    // A command as it survives arbitration untouched -- arbitration_ip.py
    // never branches on kind (M44/M45), but the full Command object still
    // flows through its queues and issued trace for a downstream consumer
    // (storage_pipeline_subsystem's dispatch bridge) to forward on. Defaults
    // mirror common.py's Command dataclass defaults (kind="READ", size_kb=4).
    struct IssuedCommand {
        std::string cmd_id;
        std::string tenant_id;
        std::string kind;
        double size_kb;
    };

    // Host-facing API, mirroring the SimPy model's public methods.
    void enqueue(const std::string& port_id, const std::string& tenant_id, const std::string& sq_id,
                 const std::string& cmd_id, const std::string& kind = "READ", double size_kb = 4.0);
    void configure_burst(std::optional<int> device, const std::map<std::string, int>& tenants,
                          const std::map<std::string, std::map<std::string, int>>& sqs);
    // qos_credit_if's eligibility_check fields. `field` is one of
    // tenant_alive, read_iops_credit, write_iops_credit, read_bw_credit,
    // write_bw_credit.
    void set_tenant_credit(const std::string& tenant_id, const std::string& field, bool value);
    void set_issue_ready(bool ready);

    std::map<std::string, long> get_metrics() const { return metrics; }
    const std::vector<std::string>& get_issued_cmd_ids() const { return issued_cmd_ids; }
    const std::vector<IssuedCommand>& get_issued() const { return issued; }
    const std::vector<std::array<std::string, 3>>& get_selection_trace() const { return selection_trace; }
    bool is_issue_ready() const { return output_ready; }

    std::map<std::string, std::string> fsm_state;

  private:
    struct Selection {
        std::string port_id;
        std::string tenant_id;
        std::string sq_id;
    };
    struct QueuedCommand {
        std::string cmd_id;
        std::string kind;
        double size_kb;
    };

    void arbiter_main_process();
    void issue_pipeline_process();
    void policy_update_process();

    // arbiter_main helpers
    void update_pending_bitmaps();
    bool has_actionable_work() const;
    std::vector<std::string> pending_ports() const;
    std::vector<std::string> active_tenants(const std::string& port_id) const;
    bool sample_eligibility(const std::string& tenant_id) const;
    void refill_tenant_credit(const std::vector<std::string>& tenant_ids);
    std::pair<std::optional<std::string>, std::optional<std::string>> scan_candidate(
        const std::string& port_id, const std::set<std::string>& exclude_tenants
    );
    std::optional<std::string> select_tenant(const std::string& port_id, const std::set<std::string>& exclude);
    std::optional<std::string> select_sq(const std::string& tenant_id) const;
    void stall(const char* reason, const char* metric);

    // issue_pipeline helpers
    int pending_count(const Selection& selection) const;
    int issue_count(const Selection& selection) const;
    // Background thread: lets the issue port's own latency elapse before
    // releasing it, off the pipeline's critical path (same ruling as
    // completion_ip's completion_output_port -- see decisions/arbitration_ip.md).
    void release_issue_slot_process();

    void wake_arbiter();
    void set_fsm_state(const std::string& fsm, const std::string& state);

    // selected_sq_q: capacity-1 handoff store between arbiter_main and
    // issue_pipeline (SimPy simpy.Store(capacity=1)).
    void selected_sq_put(const Selection& selection);
    Selection selected_sq_get();
    // policy_update_q: unbounded handoff store (SimPy simpy.Store()).
    void policy_update_put(const Selection& selection);
    Selection policy_update_get();

    std::map<std::string, sc_time> latency;

    // topology: port -> tenant -> [sq...], fixed at construction (mirrors
    // DEFAULT_TOPOLOGY in the SimPy model, or an injected override).
    Topology topology;

    WeightedOrder port_policy;
    std::map<std::string, WeightedOrder> tenant_policies;  // keyed by port_id
    std::map<std::string, WeightedOrder> sq_policies;      // keyed by tenant_id

    struct TenantCredit {
        bool tenant_alive = true;
        bool read_iops_credit = true;
        bool write_iops_credit = true;
        bool read_bw_credit = true;
        bool write_bw_credit = true;
    };
    std::map<std::string, TenantCredit> tenant_credit;

    int device_burst_available = 1024;
    std::map<std::string, int> tenant_burst_available;
    std::map<std::string, std::map<std::string, int>> sq_burst_available;

    // queues[port][tenant][sq] = command FIFO
    std::map<std::string, std::map<std::string, std::map<std::string, std::deque<QueuedCommand>>>> queues;
    std::map<std::string, bool> port_pending_bitmap;
    std::map<std::string, std::map<std::string, bool>> tenant_pending_bitmap;
    std::map<std::string, std::map<std::string, bool>> sq_pending_bitmap;
    bool bitmap_dirty = false;
    std::set<std::pair<std::string, std::string>> inflight_sqs;  // (tenant_id, sq_id)

    bool output_ready = true;

    std::vector<std::string> issued_cmd_ids;
    std::vector<IssuedCommand> issued;
    std::vector<std::array<std::string, 3>> selection_trace;
    std::map<std::string, long> metrics;

    bool issue_slot_busy = false;
    sc_event issue_slot_free;
    sc_event release_issue_slot_request;

    std::optional<Selection> pending_selected_sq;
    sc_event selected_sq_has_item;
    sc_event selected_sq_consumed;

    std::deque<Selection> policy_update_queue;
    sc_event policy_update_notify;

    sc_event arbiter_wake_event;
};
