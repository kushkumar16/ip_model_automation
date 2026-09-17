#include "arbitration_ip.h"

#include <algorithm>
#include <stdexcept>
#include <tuple>

namespace {
using Topology = ArbitrationIpModel::Topology;

// DEFAULT_TOPOLOGY / DEFAULT_WEIGHTS from the SimPy model. Port and SQ
// weights stay at these defaults regardless of caller overrides -- only
// tenant_weight_override varies in practice (storage_pipeline_subsystem's
// own flat {"T0": 1, "T1": 1} shorthand mirrors what _normalize_weights
// does for an all-int weights dict: only the tenant level is touched).
Topology default_topology() {
    return {
        {"port0", {{"T0", {"SQ0", "SQ1"}}, {"T1", {"SQ0", "SQ1", "SQ2", "SQ3"}}}},
        {"port1", {{"T2", {"SQ0", "SQ1"}}, {"T3", {"SQ0"}}}},
    };
}
std::map<std::string, int> default_port_weights() { return {{"port0", 1}, {"port1", 1}}; }
std::map<std::string, int> default_tenant_weights() { return {{"T0", 1}, {"T1", 4}, {"T2", 2}, {"T3", 1}}; }
std::map<std::string, int> default_sq_weights() { return {{"SQ0", 4}, {"SQ1", 2}, {"SQ2", 1}, {"SQ3", 1}}; }

std::map<std::string, int> merge_weights(
    std::map<std::string, int> base, const std::optional<std::map<std::string, int>>& override
) {
    if (override) {
        for (auto& [name, weight] : *override) {
            base[name] = weight;
        }
    }
    return base;
}

int weight_of(const std::map<std::string, int>& weights, const std::string& name) {
    auto it = weights.find(name);
    return it == weights.end() ? 1 : it->second;
}

WeightedOrder build_port_policy(const Topology& topology, const std::map<std::string, int>& weights) {
    std::vector<std::pair<std::string, int>> pairs;
    for (auto& [port_id, tenants] : topology) {
        (void)tenants;
        pairs.push_back({port_id, weight_of(weights, port_id)});
    }
    return WeightedOrder(pairs);
}

std::map<std::string, WeightedOrder> build_tenant_policies(
    const Topology& topology, const std::map<std::string, int>& weights
) {
    std::map<std::string, WeightedOrder> result;
    for (auto& [port_id, tenants] : topology) {
        std::vector<std::pair<std::string, int>> pairs;
        for (auto& [tenant_id, sqs] : tenants) {
            (void)sqs;
            pairs.push_back({tenant_id, weight_of(weights, tenant_id)});
        }
        result.emplace(port_id, WeightedOrder(pairs));
    }
    return result;
}

std::map<std::string, WeightedOrder> build_sq_policies(
    const Topology& topology, const std::map<std::string, int>& weights
) {
    std::map<std::string, WeightedOrder> result;
    for (auto& [port_id, tenants] : topology) {
        (void)port_id;
        for (auto& [tenant_id, sqs] : tenants) {
            std::vector<std::pair<std::string, int>> pairs;
            for (auto& sq_id : sqs) {
                pairs.push_back({sq_id, weight_of(weights, sq_id)});
            }
            result.emplace(tenant_id, WeightedOrder(pairs));
        }
    }
    return result;
}
}  // namespace

ArbitrationIpModel::ArbitrationIpModel(
    sc_module_name name, int bitmap_latency, int port_scan_latency, int tenant_scan_latency, int sq_scan_latency,
    int grant_latency, int selection_accept_latency, int pending_count_latency, int burst_read_latency,
    int burst_calc_latency, int issue_latency, int burst_debit_latency, int weighted_order_rebuild_latency,
    int reset_latency, int issue_slot_latency, int credit_refill_latency, std::optional<Topology> topology_override,
    std::optional<std::map<std::string, int>> tenant_weight_override
)
    : sc_module(name),
      topology(topology_override.value_or(default_topology())),
      port_policy(build_port_policy(topology, default_port_weights())),
      tenant_policies(build_tenant_policies(topology, merge_weights(default_tenant_weights(), tenant_weight_override))),
      sq_policies(build_sq_policies(topology, default_sq_weights())) {
    latency["bitmap"] = sc_time(bitmap_latency, SC_NS);
    latency["port_scan"] = sc_time(port_scan_latency, SC_NS);
    latency["tenant_scan"] = sc_time(tenant_scan_latency, SC_NS);
    latency["sq_scan"] = sc_time(sq_scan_latency, SC_NS);
    latency["grant"] = sc_time(grant_latency, SC_NS);
    latency["selection_accept"] = sc_time(selection_accept_latency, SC_NS);
    latency["pending_count"] = sc_time(pending_count_latency, SC_NS);
    latency["burst_read"] = sc_time(burst_read_latency, SC_NS);
    latency["burst_calc"] = sc_time(burst_calc_latency, SC_NS);
    latency["issue"] = sc_time(issue_latency, SC_NS);
    latency["burst_debit"] = sc_time(burst_debit_latency, SC_NS);
    latency["pointer_update"] = sc_time(1, SC_NS);
    latency["weighted_order_rebuild"] = sc_time(weighted_order_rebuild_latency, SC_NS);
    latency["reset"] = sc_time(reset_latency, SC_NS);
    latency["issue_slot"] = sc_time(issue_slot_latency, SC_NS);
    latency["credit_refill"] = sc_time(credit_refill_latency, SC_NS);
    latency["backpressure_retry"] = sc_time(1, SC_NS);

    for (auto& [port_id, tenants] : topology) {
        port_pending_bitmap[port_id] = false;
        for (auto& [tenant_id, sqs] : tenants) {
            tenant_pending_bitmap[port_id][tenant_id] = false;
            tenant_credit[tenant_id];       // default-construct: every credit axis true
            tenant_burst_available[tenant_id] = 1024;
            for (auto& sq_id : sqs) {
                sq_pending_bitmap[tenant_id][sq_id] = false;
                sq_burst_available[tenant_id][sq_id] = 1024;
                queues[port_id][tenant_id][sq_id];  // pre-populate so .at() is safe everywhere
            }
        }
    }

    fsm_state["arbiter_main"] = "RESET";
    fsm_state["issue_pipeline"] = "RESET";
    fsm_state["policy_update"] = "RESET";

    SC_THREAD(arbiter_main_process);
    SC_THREAD(issue_pipeline_process);
    SC_THREAD(policy_update_process);
    SC_THREAD(release_issue_slot_process);
}

// ---------------------------------------------------------------------- //
// Host-facing API
// ---------------------------------------------------------------------- //
void ArbitrationIpModel::enqueue(
    const std::string& port_id, const std::string& tenant_id, const std::string& sq_id, const std::string& cmd_id,
    const std::string& kind, double size_kb
) {
    queues[port_id][tenant_id][sq_id].push_back({cmd_id, kind, size_kb});
    bitmap_dirty = true;
    wake_arbiter();
}

void ArbitrationIpModel::configure_burst(
    std::optional<int> device, const std::map<std::string, int>& tenants,
    const std::map<std::string, std::map<std::string, int>>& sqs
) {
    if (device) {
        device_burst_available = *device;
    }
    for (auto& [tenant_id, value] : tenants) {
        tenant_burst_available[tenant_id] = value;
    }
    for (auto& [tenant_id, sq_values] : sqs) {
        for (auto& [sq_id, value] : sq_values) {
            sq_burst_available[tenant_id][sq_id] = value;
        }
    }
}

void ArbitrationIpModel::set_tenant_credit(const std::string& tenant_id, const std::string& field, bool value) {
    TenantCredit& credit = tenant_credit[tenant_id];
    if (field == "tenant_alive") {
        credit.tenant_alive = value;
    } else if (field == "read_iops_credit") {
        credit.read_iops_credit = value;
    } else if (field == "write_iops_credit") {
        credit.write_iops_credit = value;
    } else if (field == "read_bw_credit") {
        credit.read_bw_credit = value;
    } else if (field == "write_bw_credit") {
        credit.write_bw_credit = value;
    } else {
        throw std::invalid_argument("qos_credit_if declares no field " + field);
    }
    // tenant_alive reviving a previously credit-blocked, bitmap-pending
    // tenant can make actionable work exist again; nothing else would wake
    // an arbiter already blocked in IDLE to notice (mirrors the SimPy port).
    wake_arbiter();
}

void ArbitrationIpModel::set_issue_ready(bool ready) { output_ready = ready; }

// ---------------------------------------------------------------------- //
// arbiter_main helpers
// ---------------------------------------------------------------------- //
void ArbitrationIpModel::set_fsm_state(const std::string& fsm, const std::string& state) { fsm_state[fsm] = state; }

void ArbitrationIpModel::wake_arbiter() {
    // Unconditional notify: a no-op if nobody is currently waiting on it
    // (SystemC events are not latched). The arbiter re-checks real state
    // (has_actionable_work) before every wait, so a wake that arrives while
    // it is busy is never missed -- it will simply see the state directly.
    arbiter_wake_event.notify();
}

void ArbitrationIpModel::update_pending_bitmaps() {
    for (auto& [port_id, tenants] : topology) {
        bool port_pending = false;
        for (auto& [tenant_id, sqs] : tenants) {
            bool tenant_pending = false;
            for (auto& sq_id : sqs) {
                bool sq_pending = !queues[port_id][tenant_id][sq_id].empty();
                sq_pending_bitmap[tenant_id][sq_id] = sq_pending;
                tenant_pending = tenant_pending || sq_pending;
            }
            tenant_pending_bitmap[port_id][tenant_id] = tenant_pending;
            port_pending = port_pending || tenant_pending;
        }
        port_pending_bitmap[port_id] = port_pending;
    }
    bitmap_dirty = false;
    metrics["bitmap_updates"]++;
}

bool ArbitrationIpModel::has_actionable_work() const {
    for (auto& [port_id, tenants] : topology) {
        if (!port_pending_bitmap.at(port_id)) {
            continue;
        }
        for (auto& [tenant_id, sqs] : tenants) {
            if (!tenant_credit.at(tenant_id).tenant_alive) {
                continue;
            }
            for (auto& sq_id : sqs) {
                if (!queues.at(port_id).at(tenant_id).at(sq_id).empty() &&
                    !inflight_sqs.count({tenant_id, sq_id})) {
                    return true;
                }
            }
        }
    }
    return false;
}

std::vector<std::string> ArbitrationIpModel::pending_ports() const {
    std::vector<std::string> result;
    for (auto& port_id : port_policy.scan()) {
        if (port_pending_bitmap.at(port_id)) {
            result.push_back(port_id);
        }
    }
    return result;
}

std::vector<std::string> ArbitrationIpModel::active_tenants(const std::string& port_id) const {
    std::vector<std::string> result;
    for (auto& tenant_id : tenant_policies.at(port_id).scan()) {
        if (tenant_pending_bitmap.at(port_id).at(tenant_id) && tenant_credit.at(tenant_id).tenant_alive) {
            result.push_back(tenant_id);
        }
    }
    return result;
}

bool ArbitrationIpModel::sample_eligibility(const std::string& tenant_id) const {
    const TenantCredit& c = tenant_credit.at(tenant_id);
    return c.tenant_alive && c.read_iops_credit && c.write_iops_credit && c.read_bw_credit && c.write_bw_credit;
}

void ArbitrationIpModel::refill_tenant_credit(const std::vector<std::string>& tenant_ids) {
    for (auto& tenant_id : tenant_ids) {
        TenantCredit& c = tenant_credit[tenant_id];
        c.read_iops_credit = true;
        c.write_iops_credit = true;
        c.read_bw_credit = true;
        c.write_bw_credit = true;
    }
    metrics["credit_refills"]++;
}

std::optional<std::string> ArbitrationIpModel::select_tenant(
    const std::string& port_id, const std::set<std::string>& exclude
) {
    for (auto& tenant_id : tenant_policies.at(port_id).scan()) {
        if (exclude.count(tenant_id)) {
            continue;
        }
        if (!tenant_pending_bitmap.at(port_id).at(tenant_id)) {
            continue;
        }
        if (!sample_eligibility(tenant_id)) {
            metrics["eligibility_rejections"]++;
            continue;
        }
        return tenant_id;
    }
    return std::nullopt;
}

std::optional<std::string> ArbitrationIpModel::select_sq(const std::string& tenant_id) const {
    for (auto& sq_id : sq_policies.at(tenant_id).scan()) {
        if (sq_pending_bitmap.at(tenant_id).at(sq_id) && !inflight_sqs.count({tenant_id, sq_id})) {
            return sq_id;
        }
    }
    return std::nullopt;
}

std::pair<std::optional<std::string>, std::optional<std::string>> ArbitrationIpModel::scan_candidate(
    const std::string& port_id, const std::set<std::string>& exclude_tenants
) {
    set_fsm_state("arbiter_main", "TENANT_SCAN");
    wait(latency["tenant_scan"]);
    auto tenant_id = select_tenant(port_id, exclude_tenants);
    if (!tenant_id) {
        return {std::nullopt, std::nullopt};
    }

    set_fsm_state("arbiter_main", "SQ_SCAN");
    wait(latency["sq_scan"]);
    auto sq_id = select_sq(*tenant_id);
    return {tenant_id, sq_id};
}

void ArbitrationIpModel::stall(const char* reason, const char* metric) {
    (void)reason;
    set_fsm_state("arbiter_main", "STALL");
    metrics["stalls"]++;
    metrics[metric]++;
    wait(latency["backpressure_retry"]);
}

// ---------------------------------------------------------------------- //
// issue_pipeline helpers
// ---------------------------------------------------------------------- //
int ArbitrationIpModel::pending_count(const Selection& selection) const {
    return static_cast<int>(queues.at(selection.port_id).at(selection.tenant_id).at(selection.sq_id).size());
}

int ArbitrationIpModel::issue_count(const Selection& selection) const {
    int result = pending_count(selection);
    result = std::min(result, device_burst_available);
    result = std::min(result, tenant_burst_available.at(selection.tenant_id));
    result = std::min(result, sq_burst_available.at(selection.tenant_id).at(selection.sq_id));
    return result;
}

void ArbitrationIpModel::release_issue_slot_process() {
    // Capacity-1 slot: at most one release is ever pending (the pipeline
    // cannot re-acquire until this fires), so a single persistent thread is
    // enough -- no dynamic per-release process needed.
    while (true) {
        wait(release_issue_slot_request);
        wait(latency["issue_slot"]);
        issue_slot_busy = false;
        issue_slot_free.notify();
    }
}

// ---------------------------------------------------------------------- //
// selected_sq_q / policy_update_q handoff stores
// ---------------------------------------------------------------------- //
void ArbitrationIpModel::selected_sq_put(const Selection& selection) {
    while (pending_selected_sq.has_value()) {
        wait(selected_sq_consumed);
    }
    pending_selected_sq = selection;
    selected_sq_has_item.notify();
}

ArbitrationIpModel::Selection ArbitrationIpModel::selected_sq_get() {
    while (!pending_selected_sq.has_value()) {
        wait(selected_sq_has_item);
    }
    Selection selection = *pending_selected_sq;
    pending_selected_sq.reset();
    selected_sq_consumed.notify();
    return selection;
}

void ArbitrationIpModel::policy_update_put(const Selection& selection) {
    policy_update_queue.push_back(selection);
    policy_update_notify.notify();
}

ArbitrationIpModel::Selection ArbitrationIpModel::policy_update_get() {
    while (policy_update_queue.empty()) {
        wait(policy_update_notify);
    }
    Selection selection = policy_update_queue.front();
    policy_update_queue.pop_front();
    return selection;
}

// ---------------------------------------------------------------------- //
// FSM processes
// ---------------------------------------------------------------------- //
void ArbitrationIpModel::arbiter_main_process() {
    set_fsm_state("arbiter_main", "RESET");
    wait(latency["reset"]);
    set_fsm_state("arbiter_main", "IDLE");
    metrics["resets"]++;
    update_pending_bitmaps();

    std::set<std::string> tried_ports;
    std::map<std::string, std::set<std::string>> tried_tenants;

    while (true) {
        set_fsm_state("arbiter_main", "IDLE");
        while (true) {
            if (bitmap_dirty) {
                wait(latency["bitmap"]);
                update_pending_bitmaps();
            }
            if (has_actionable_work()) {
                break;
            }
            wait(arbiter_wake_event);
            tried_ports.clear();
            tried_tenants.clear();
        }

        set_fsm_state("arbiter_main", "PORT_SCAN");
        wait(latency["port_scan"]);
        std::optional<std::string> port_id;
        for (auto& p : pending_ports()) {
            if (!tried_ports.count(p)) {
                port_id = p;
                break;
            }
        }
        if (!port_id) {
            tried_ports.clear();
            tried_tenants.clear();
            stall("no eligible port", "no_port_pending_stalls");
            continue;
        }

        auto [tenant_id, sq_id] = scan_candidate(*port_id, tried_tenants[*port_id]);

        if (!tenant_id) {
            tried_ports.insert(*port_id);
            bool more_candidates = false;
            for (auto& p : pending_ports()) {
                if (!tried_ports.count(p)) {
                    more_candidates = true;
                    break;
                }
            }
            if (!more_candidates) {
                std::vector<std::string> exhausted;
                for (auto& p : pending_ports()) {
                    for (auto& t : active_tenants(p)) {
                        if (!sample_eligibility(t)) {
                            exhausted.push_back(t);
                        }
                    }
                }
                if (!exhausted.empty()) {
                    set_fsm_state("arbiter_main", "CREDIT_REFILL");
                    wait(latency["credit_refill"]);
                    refill_tenant_credit(exhausted);
                    std::tie(tenant_id, sq_id) = scan_candidate(*port_id, {});
                    if (!tenant_id) {
                        tried_ports.clear();
                        tried_tenants.clear();
                        stall("no eligible tenant", "no_tenant_pending_stalls");
                        continue;
                    }
                } else {
                    stall("no eligible tenant", "no_tenant_pending_stalls");
                    continue;
                }
            } else {
                stall("no eligible tenant", "no_tenant_pending_stalls");
                continue;
            }
        }

        if (!sq_id) {
            tried_tenants[*port_id].insert(*tenant_id);
            stall("no eligible sq", "no_sq_pending_stalls");
            continue;
        }

        inflight_sqs.insert({*tenant_id, *sq_id});
        selection_trace.push_back({*port_id, *tenant_id, *sq_id});
        metrics["selected_sqs"]++;
        Selection selection{*port_id, *tenant_id, *sq_id};
        set_fsm_state("arbiter_main", "GRANT");
        wait(latency["grant"]);
        selected_sq_put(selection);
        policy_update_put(selection);
        metrics["grants"]++;
        tried_ports.clear();
        tried_tenants.clear();
    }
}

void ArbitrationIpModel::issue_pipeline_process() {
    set_fsm_state("issue_pipeline", "WAIT_SELECTION");
    while (true) {
        set_fsm_state("issue_pipeline", "WAIT_SELECTION");
        Selection selection = selected_sq_get();
        wait(latency["selection_accept"]);

        bool hold_for_issue_ready = false;
        int cnt_pending = 0;
        int cnt_issue = 0;
        while (true) {
            if (hold_for_issue_ready) {
                while (!output_ready) {
                    set_fsm_state("issue_pipeline", "ISSUE_STALL");
                    metrics["output_stalls"]++;
                    metrics["output_backpressure_cycles"]++;
                    wait(1, SC_NS);
                }
                hold_for_issue_ready = false;
            }

            set_fsm_state("issue_pipeline", "READ_PENDING_COUNT");
            wait(latency["pending_count"]);
            cnt_pending = pending_count(selection);
            set_fsm_state("issue_pipeline", "READ_BURST");
            wait(latency["burst_read"]);
            set_fsm_state("issue_pipeline", "CALC_ISSUE_COUNT");
            wait(latency["burst_calc"]);
            cnt_issue = issue_count(selection);
            if (cnt_issue <= 0) {
                set_fsm_state("issue_pipeline", "ISSUE_STALL");
                metrics["burst_stalls"]++;
                wait(1, SC_NS);
                continue;
            }

            while (issue_slot_busy) {
                wait(issue_slot_free);
            }
            issue_slot_busy = true;
            set_fsm_state("issue_pipeline", "ISSUE_REQUEST");
            wait(latency["issue"]);
            if (output_ready) {
                break;
            }

            issue_slot_busy = false;
            issue_slot_free.notify();
            set_fsm_state("issue_pipeline", "ISSUE_STALL");
            metrics["output_stalls"]++;
            metrics["output_backpressure_cycles"]++;
            wait(1, SC_NS);
            hold_for_issue_ready = true;
        }

        auto& queue = queues[selection.port_id][selection.tenant_id][selection.sq_id];
        for (int i = 0; i < cnt_issue; i++) {
            const QueuedCommand& command = queue.front();
            issued_cmd_ids.push_back(command.cmd_id);
            issued.push_back({command.cmd_id, selection.tenant_id, command.kind, command.size_kb});
            queue.pop_front();
        }

        release_issue_slot_request.notify();

        device_burst_available -= cnt_issue;
        tenant_burst_available[selection.tenant_id] -= cnt_issue;
        sq_burst_available[selection.tenant_id][selection.sq_id] -= cnt_issue;
        set_fsm_state("issue_pipeline", "UPDATE_BURST");
        wait(latency["burst_debit"]);
        inflight_sqs.erase({selection.tenant_id, selection.sq_id});
        update_pending_bitmaps();
        // A burst limit below cnt_pending leaves real, unclaimed work
        // behind: wake a possibly-idle arbiter so it notices (mirrors the
        // SimPy port's own _wake_arbiter() call here).
        wake_arbiter();
        metrics["downstream_requests"]++;
        metrics["issued_commands"] += cnt_issue;
        (void)cnt_pending;
    }
}

void ArbitrationIpModel::policy_update_process() {
    set_fsm_state("policy_update", "WAIT_GRANT");
    while (true) {
        Selection selection = policy_update_get();
        set_fsm_state("policy_update", "UPDATE_POINTER");
        wait(latency["pointer_update"]);
        port_policy.advance_to_after(selection.port_id);
        tenant_policies.at(selection.port_id).advance_to_after(selection.tenant_id);
        sq_policies.at(selection.tenant_id).advance_to_after(selection.sq_id);
        wait(latency["weighted_order_rebuild"]);
        metrics["policy_updates"]++;
        set_fsm_state("policy_update", "WAIT_GRANT");
    }
}
