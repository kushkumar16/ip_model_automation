#include "storage_pipeline_subsystem.h"

StoragePipelineSubsystemModel::StoragePipelineSubsystemModel(
    sc_module_name name, ArbitrationLatencies arb, CompletionLatencies comp, int completion_backlog_limit_in,
    double default_token_budget
)
    : sc_module(name),
      arbitration(
          "arbitration", arb.bitmap, arb.port_scan, arb.tenant_scan, arb.sq_scan, arb.grant, arb.selection_accept,
          arb.pending_count, arb.burst_read, arb.burst_calc, arb.issue, arb.burst_debit, arb.weighted_order_rebuild,
          arb.reset, arb.issue_slot, arb.credit_refill,
          ArbitrationIpModel::Topology{{"port0", {{"T0", {"SQ0"}}, {"T1", {"SQ0"}}}}},
          std::map<std::string, int>{{"T0", 1}, {"T1", 1}}
      ),
      completion(
          "completion", comp.service, comp.tenant_select, comp.token_check, comp.emit, comp.retry, comp.refill_window,
          comp.usage_assessment, comp.base_refill, comp.metrics_publish, comp.port, comp.reset, std::nullopt, false
      ),
      completion_backlog_limit(completion_backlog_limit_in) {
    for (auto& tenant_id : {"T0", "T1"}) {
        completion.configure_tenant(tenant_id, default_token_budget, default_token_budget, default_token_budget,
                                     default_token_budget);
    }

    fsm_state["intake_bridge"] = "IDLE";
    fsm_state["dispatch_bridge"] = "POLL_ISSUED";
    fsm_state["backpressure_monitor"] = "SAMPLE_BACKLOG";

    SC_THREAD(intake_bridge_process);
    SC_THREAD(dispatch_bridge_process);
    SC_THREAD(backpressure_monitor_process);
}

// ---------------------------------------------------------------------- //
// Host-facing API
// ---------------------------------------------------------------------- //
void StoragePipelineSubsystemModel::submit(const Command& command) {
    intake_queue.push_back({true, command, "", 0.0});
    intake_notify.notify();
}

void StoragePipelineSubsystemModel::configure_qos(const std::string& tenant_id, double token_budget) {
    intake_queue.push_back({false, {}, tenant_id, token_budget});
    intake_notify.notify();
}

// ---------------------------------------------------------------------- //
// FSM processes (glue)
// ---------------------------------------------------------------------- //
void StoragePipelineSubsystemModel::intake_bridge_process() {
    while (true) {
        fsm_state["intake_bridge"] = "IDLE";
        while (intake_queue.empty()) {
            wait(intake_notify);
        }
        IntakeItem item = intake_queue.front();
        intake_queue.pop_front();

        if (item.is_command) {
            fsm_state["intake_bridge"] = "ACCEPT_COMMAND";
            wait(1, SC_NS);
            transition_counts["IDLE->ACCEPT_COMMAND"]++;
            fsm_state["intake_bridge"] = "FORWARD_TO_ARBITRATION";
            wait(1, SC_NS);
            arbitration.enqueue(
                item.command.port_id, item.command.tenant_id, item.command.sq_id, item.command.cmd_id,
                item.command.kind, item.command.size_kb
            );
            metrics["commands_submitted"]++;
            transition_counts["ACCEPT_COMMAND->FORWARD_TO_ARBITRATION"]++;
            // FORWARD_TO_ARBITRATION -> IDLE (action: acknowledge_host) is
            // its own declared 1-cycle transition, not a free return to
            // IDLE (round thirteen's M2).
            wait(1, SC_NS);
            transition_counts["FORWARD_TO_ARBITRATION->IDLE"]++;
        } else {
            fsm_state["intake_bridge"] = "ACCEPT_QOS_CONFIG";
            wait(1, SC_NS);
            transition_counts["IDLE->ACCEPT_QOS_CONFIG"]++;
            fsm_state["intake_bridge"] = "APPLY_QOS_CONFIG";
            wait(1, SC_NS);
            // CONFIGURE_QOS: applied to all four Completion IP token
            // buckets alike (round thirteen's M3).
            completion.configure_tenant(item.tenant_id, item.token_budget, item.token_budget, item.token_budget,
                                         item.token_budget);
            metrics["qos_configs_applied"]++;
            transition_counts["ACCEPT_QOS_CONFIG->APPLY_QOS_CONFIG"]++;
            wait(1, SC_NS);
            transition_counts["APPLY_QOS_CONFIG->IDLE"]++;
        }
    }
}

void StoragePipelineSubsystemModel::dispatch_bridge_process() {
    // One issued-but-unforwarded command per pass, not a batch drain: the
    // declared POLL_ISSUED <-> FORWARD_TO_COMPLETION pair (plus the
    // POLL_ISSUED self-loop when nothing is new) is what a reader of the
    // template sees, so the model returns to POLL_ISSUED, for real, between
    // every command forwarded rather than looping inside
    // FORWARD_TO_COMPLETION for a whole batch.
    while (true) {
        fsm_state["dispatch_bridge"] = "POLL_ISSUED";
        wait(1, SC_NS);
        if (issued_seen >= arbitration.get_issued().size()) {
            transition_counts["POLL_ISSUED->POLL_ISSUED"]++;
            continue;
        }
        const ArbitrationIpModel::IssuedCommand& issued_command = arbitration.get_issued()[issued_seen];
        issued_seen++;
        transition_counts["POLL_ISSUED->FORWARD_TO_COMPLETION"]++;
        fsm_state["dispatch_bridge"] = "FORWARD_TO_COMPLETION";
        wait(1, SC_NS);
        completion.submit(
            {issued_command.cmd_id, issued_command.kind, issued_command.tenant_id, issued_command.size_kb}
        );
        metrics["completion_submitted"]++;
        transition_counts["FORWARD_TO_COMPLETION->POLL_ISSUED"]++;
    }
}

void StoragePipelineSubsystemModel::backpressure_monitor_process() {
    while (true) {
        fsm_state["backpressure_monitor"] = "SAMPLE_BACKLOG";
        wait(2, SC_NS);
        std::size_t backlog = completion.total_pending_count();
        bool backlogged = static_cast<int>(backlog) >= completion_backlog_limit;
        if (backlogged == issue_throttled) {
            transition_counts["SAMPLE_BACKLOG->SAMPLE_BACKLOG"]++;
            continue;
        }
        fsm_state["backpressure_monitor"] = "APPLY_THROTTLE";
        wait(1, SC_NS);
        transition_counts["SAMPLE_BACKLOG->APPLY_THROTTLE"]++;
        issue_throttled = backlogged;
        arbitration.set_issue_ready(!backlogged);
        transition_counts["APPLY_THROTTLE->SAMPLE_BACKLOG"]++;
        if (backlogged) {
            metrics["backpressure_events"]++;
        }
    }
}
