#include "completion_ip.h"

#include <algorithm>
#include <cctype>
#include <stdexcept>

CompletionIpModel::CompletionIpModel(
    sc_module_name name, int service_latency, int tenant_select_latency, int token_check_latency, int emit_latency,
    int retry_latency, int refill_window, int usage_assessment_latency, int base_refill_latency,
    int metrics_publish_latency, int port_latency, int reset_latency, std::optional<int> pending_depth_in,
    bool start_refill_process_in
)
    : sc_module(name), tenant_order(std::vector<std::pair<std::string, int>>{{"T0", 1}}) {
    latency["service"] = sc_time(service_latency, SC_NS);
    latency["tenant_select"] = sc_time(tenant_select_latency, SC_NS);
    latency["token_check"] = sc_time(token_check_latency, SC_NS);
    latency["emit"] = sc_time(emit_latency, SC_NS);
    latency["retry"] = sc_time(retry_latency, SC_NS);
    latency["refill_window"] = sc_time(refill_window, SC_NS);
    latency["usage_assessment"] = sc_time(usage_assessment_latency, SC_NS);
    latency["base_refill"] = sc_time(base_refill_latency, SC_NS);
    latency["metrics_publish"] = sc_time(metrics_publish_latency, SC_NS);
    latency["port"] = sc_time(port_latency, SC_NS);
    latency["reset"] = sc_time(reset_latency, SC_NS);

    pending_depth = pending_depth_in;
    start_refill_process = start_refill_process_in;
    tenant_weights["T0"] = 1;

    fsm_state["accept"] = "RESET";
    fsm_state["completion_scheduler"] = "IDLE";
    fsm_state["refill"] = "WAIT_WINDOW";

    SC_THREAD(accept_process);
    SC_THREAD(completion_scheduler_process);
    SC_THREAD(release_output_port_process);
    if (start_refill_process) {
        SC_THREAD(refill_process_impl);
    }
}

// ---------------------------------------------------------------------- //
// Host-facing API
// ---------------------------------------------------------------------- //
void CompletionIpModel::configure_tenant(
    const std::string& tenant_id, double read, double write, double read_bw, double write_bw, int weight, bool alive
) {
    ensure_tenant(tenant_id, weight);
    TokenBucket base{read, write, read_bw, write_bw};
    tokens[tenant_id] = base;
    base_tokens[tenant_id] = base;
    tenant_alive[tenant_id] = alive;
    refresh_eligibility(tenant_id);
}

void CompletionIpModel::set_tenant_alive(const std::string& tenant_id, bool alive) {
    tenant_alive[tenant_id] = alive;
    refresh_eligibility(tenant_id);
}

void CompletionIpModel::set_completion_ready(bool ready) { output_ready = ready; }

void CompletionIpModel::submit(const Command& command) {
    ensure_tenant(command.tenant_id);
    input_queue.push_back(command);
    input_notify.notify();
}

void CompletionIpModel::refill_once() {
    WindowSnapshot snapshot = snapshot_window_usage();
    restore_base_tokens();
    write_window_metrics(snapshot);
}

std::size_t CompletionIpModel::pending_count(const std::string& tenant_id) const {
    auto it = pending.find(tenant_id);
    return it == pending.end() ? 0 : it->second.size();
}

// ---------------------------------------------------------------------- //
// helpers
// ---------------------------------------------------------------------- //
void CompletionIpModel::ensure_tenant(const std::string& tenant_id, int weight) {
    if (tenant_weights.count(tenant_id)) {
        return;
    }
    tenant_weights[tenant_id] = std::max(1, weight);
    tenant_order = WeightedOrder(
        std::vector<std::pair<std::string, int>>(tenant_weights.begin(), tenant_weights.end())
    );
    tenant_alive.emplace(tenant_id, true);
}

void CompletionIpModel::clear_state() {
    // ip.reset.behavior: clear pending queues, tokens and metrics. Token
    // state is deliberately left alone -- see decisions/completion_ip.md;
    // the SimPy port's own _clear_state carries the same ruling.
    pending.clear();
    metrics.clear();
    window_metrics.clear();
    completed.clear();
    eligible_tenants.clear();
    refresh_all_eligibility();
}

bool CompletionIpModel::pending_queue_full(const std::string& tenant_id) const {
    if (!pending_depth) {
        return false;
    }
    auto it = pending.find(tenant_id);
    std::size_t depth = it == pending.end() ? 0 : it->second.size();
    return static_cast<int>(depth) >= *pending_depth;
}

CompletionIpModel::Costs CompletionIpModel::costs_for(const Command& command) {
    if (command.kind == "READ") {
        return {{{"read", 1.0}, {"read_bw", command.size_kb}}};
    }
    if (command.kind == "FLUSH") {
        return {};
    }
    return {{{"write", 1.0}, {"write_bw", command.size_kb}}};
}

double& CompletionIpModel::token_field(TokenBucket& bucket, const std::string& name) {
    if (name == "read") {
        return bucket.read;
    }
    if (name == "write") {
        return bucket.write;
    }
    if (name == "read_bw") {
        return bucket.read_bw;
    }
    if (name == "write_bw") {
        return bucket.write_bw;
    }
    throw std::invalid_argument("unknown token field " + name);
}

bool CompletionIpModel::tenant_eligible(const std::string& tenant_id) {
    if (!tenant_alive[tenant_id] || pending[tenant_id].empty()) {
        return false;
    }
    Costs costs = costs_for(pending[tenant_id].front());
    for (auto& [name, cost] : costs.items) {
        (void)cost;
        if (token_field(tokens[tenant_id], name) <= 0) {
            return false;
        }
    }
    return true;
}

void CompletionIpModel::refresh_eligibility(const std::string& tenant_id) {
    if (tenant_eligible(tenant_id)) {
        eligible_tenants.insert(tenant_id);
        wake_scheduler();
    } else {
        eligible_tenants.erase(tenant_id);
    }
}

void CompletionIpModel::refresh_all_eligibility() {
    std::set<std::string> tenant_ids;
    for (auto& [tenant_id, _] : pending) {
        tenant_ids.insert(tenant_id);
    }
    for (auto& [tenant_id, _] : tokens) {
        tenant_ids.insert(tenant_id);
    }
    for (auto& tenant_id : tenant_ids) {
        refresh_eligibility(tenant_id);
    }
}

std::optional<std::string> CompletionIpModel::select_tenant() {
    for (auto& tenant_id : tenant_order.scan()) {
        if (pending[tenant_id].empty()) {
            continue;
        }
        if (!tenant_alive[tenant_id]) {
            metrics["tenant_inactive_stalls"]++;
            continue;
        }
        if (!eligible_tenants.count(tenant_id)) {
            continue;
        }
        tenant_order.advance_to_after(tenant_id);
        return tenant_id;
    }
    return std::nullopt;
}

bool CompletionIpModel::can_pay(const std::string& tenant_id, const Costs& costs) {
    for (auto& [name, cost] : costs.items) {
        if (token_field(tokens[tenant_id], name) < cost) {
            return false;
        }
    }
    return true;
}

void CompletionIpModel::debit(const std::string& tenant_id, const Costs& costs) {
    for (auto& [name, cost] : costs.items) {
        token_field(tokens[tenant_id], name) -= cost;
    }
}

CompletionIpModel::WindowSnapshot CompletionIpModel::snapshot_window_usage() {
    WindowSnapshot snapshot{
        static_cast<double>(sc_time_stamp().to_default_time_units()),
        static_cast<double>(metrics["completed_commands"])
    };
    pending_snapshot = snapshot;
    return snapshot;
}

void CompletionIpModel::restore_base_tokens() {
    for (auto& [tenant_id, base] : base_tokens) {
        tokens[tenant_id] = base;
    }
    refresh_all_eligibility();
}

void CompletionIpModel::write_window_metrics(const WindowSnapshot& snapshot) {
    window_metrics.push_back(snapshot);
    metrics["refill_windows"]++;
}

void CompletionIpModel::wake_scheduler() { scheduler_wake_event.notify(); }

// ---------------------------------------------------------------------- //
// FSM processes
// ---------------------------------------------------------------------- //
void CompletionIpModel::accept_process() {
    fsm_state["accept"] = "RESET";
    wait(latency["reset"]);
    clear_state();
    while (true) {
        fsm_state["accept"] = "READY";
        while (input_queue.empty()) {
            wait(input_notify);
        }
        Command command = input_queue.front();
        input_queue.pop_front();

        bool backpressured = false;
        while (pending_queue_full(command.tenant_id)) {
            backpressured = true;
            fsm_state["accept"] = "BACKPRESSURE";
            metrics["queue_full_stalls"]++;
            wait(latency["retry"]);
        }
        if (backpressured) {
            fsm_state["accept"] = "READY";
            wait(latency["retry"]);
        }

        fsm_state["accept"] = "ENQUEUE";
        wait(latency["service"]);
        pending[command.tenant_id].push_back(command);
        refresh_eligibility(command.tenant_id);
        if (!tenant_alive[command.tenant_id]) {
            metrics["tenant_inactive_stalls"]++;
        } else {
            metrics["accepted_commands"]++;
        }
    }
}

void CompletionIpModel::completion_scheduler_process() {
    while (true) {
        fsm_state["completion_scheduler"] = "IDLE";
        while (eligible_tenants.empty()) {
            wait(scheduler_wake_event);
        }

        while (true) {
            fsm_state["completion_scheduler"] = "SELECT_TENANT";
            wait(latency["tenant_select"]);
            auto tenant_id_opt = select_tenant();
            if (!tenant_id_opt) {
                metrics["stalls"]++;
                wait(latency["retry"]);
                break;
            }
            const std::string& tenant_id = *tenant_id_opt;
            Command command = pending[tenant_id].front();

            fsm_state["completion_scheduler"] = "CHECK_TOKENS";
            wait(latency["token_check"]);
            Costs costs = costs_for(command);
            if (!can_pay(tenant_id, costs)) {
                fsm_state["completion_scheduler"] = "WAIT_TOKENS";
                metrics["token_stalls"]++;
                wait(latency["retry"]);
                continue;
            }

            fsm_state["completion_scheduler"] = "EMIT";
            while (!output_ready) {
                fsm_state["completion_scheduler"] = "STALL_OUTPUT";
                metrics["output_stalls"]++;
                wait(latency["retry"]);
                fsm_state["completion_scheduler"] = "EMIT";
            }

            debit(tenant_id, costs);
            pending[tenant_id].pop_front();
            while (completion_port_busy) {
                wait(completion_port_free);
            }
            completion_port_busy = true;
            wait(latency["emit"]);
            release_output_port_request.notify();

            completed.emplace_back(static_cast<double>(sc_time_stamp().to_default_time_units()), command.cmd_id);
            std::string kind_lower = command.kind;
            std::transform(kind_lower.begin(), kind_lower.end(), kind_lower.begin(), [](unsigned char c) {
                return std::tolower(c);
            });
            metrics["completed_" + kind_lower]++;
            metrics["completed_commands"]++;
            refresh_eligibility(tenant_id);

            if (eligible_tenants.empty()) {
                break;
            }
        }
    }
}

void CompletionIpModel::refill_process_impl() {
    while (true) {
        fsm_state["refill"] = "WAIT_WINDOW";
        wait(latency["refill_window"]);
        WindowSnapshot snapshot = snapshot_window_usage();

        fsm_state["refill"] = "ASSESS_USAGE";
        wait(latency["usage_assessment"]);
        restore_base_tokens();

        fsm_state["refill"] = "REFILL_BASE";
        wait(latency["base_refill"]);
        write_window_metrics(snapshot);

        fsm_state["refill"] = "PUBLISH_METRICS";
        wait(latency["metrics_publish"]);
    }
}

void CompletionIpModel::release_output_port_process() {
    // Capacity-1 port: at most one release is ever pending (the scheduler
    // cannot re-acquire until this fires), so a single persistent thread is
    // enough -- same reasoning as arbitration_ip's issue_slot release.
    while (true) {
        wait(release_output_port_request);
        wait(latency["port"]);
        completion_port_busy = false;
        completion_port_free.notify();
    }
}
