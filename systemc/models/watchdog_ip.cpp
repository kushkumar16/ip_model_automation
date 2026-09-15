#include "watchdog_ip.h"

WatchdogIpModel::WatchdogIpModel(
    sc_module_name name,
    int reset_latency,
    int arm_latency,
    int kick_latency,
    int disarm_latency,
    int countdown_tick_latency,
    int expire_latency,
    int accept_config_latency,
    int apply_timeout_latency,
    int default_timeout_cycles
)
    : sc_module(name), configured_timeout_cycles(default_timeout_cycles) {
    latency["reset"] = sc_time(reset_latency, SC_NS);
    latency["arm"] = sc_time(arm_latency, SC_NS);
    latency["kick"] = sc_time(kick_latency, SC_NS);
    latency["disarm"] = sc_time(disarm_latency, SC_NS);
    latency["countdown_tick"] = sc_time(countdown_tick_latency, SC_NS);
    latency["expire"] = sc_time(expire_latency, SC_NS);
    latency["accept_config"] = sc_time(accept_config_latency, SC_NS);
    latency["apply_timeout"] = sc_time(apply_timeout_latency, SC_NS);

    metrics["kicks_received"] = 0;
    metrics["expirations"] = 0;
    metrics["arm_count"] = 0;
    metrics["disarm_count"] = 0;

    fsm_state["watchdog_main"] = "RESET";
    SC_THREAD(watchdog_main_process);
    fsm_state["config_intake"] = "IDLE";
    SC_THREAD(config_intake_process);
}

// ---------------------------------------------------------------------- //
// Host-facing command API
// ---------------------------------------------------------------------- //
void WatchdogIpModel::submit_host_command(const std::string& command) {
    sc_event done;
    host_queue.push_back({command, &done});
    host_command_notify.notify();
    wait(done);
}

void WatchdogIpModel::arm() { submit_host_command("ARM"); }
void WatchdogIpModel::kick() { submit_host_command("KICK"); }
void WatchdogIpModel::disarm() { submit_host_command("DISARM"); }

void WatchdogIpModel::configure_timeout(int timeout_cycles) {
    sc_event done;
    config_queue.push_back({timeout_cycles, &done});
    config_command_notify.notify();
    wait(done);
}

// ---------------------------------------------------------------------- //
// FSM processes
// ---------------------------------------------------------------------- //
void WatchdogIpModel::set_fsm_state(const std::string& fsm, const std::string& state) {
    fsm_state[fsm] = state;
    fsm_state_changed.notify();
}

void WatchdogIpModel::watchdog_main_process() {
    set_fsm_state("watchdog_main", "RESET");
    wait(latency["reset"]);
    transition_counts["RESET->DISARMED"]++;

    while (true) {
        set_fsm_state("watchdog_main", "DISARMED");
        while (host_queue.empty()) {
            wait(host_command_notify);
        }
        HostCommand cmd = host_queue.front();
        host_queue.pop_front();

        if (cmd.command != "ARM") {
            // No declared transition from DISARMED on KICK/DISARM: there is
            // nothing to kick or cancel yet, so the command is accepted with
            // no further effect.
            cmd.done->notify();
            continue;
        }

        wait(latency["arm"]);
        armed = true;
        countdown_cycles = configured_timeout_cycles;
        metrics["arm_count"]++;
        transition_counts["DISARMED->ARMED"]++;
        set_fsm_state("watchdog_main", "ARMED");
        cmd.done->notify();

        bool expired_flag = run_armed();
        if (expired_flag) {
            run_expired();
        }
        // Either path returns here disarmed; loop back to DISARMED.
    }
}

bool WatchdogIpModel::run_armed() {
    while (true) {
        // Race a countdown tick against the next host command. Checking
        // host_queue.empty() before waiting -- rather than relying solely on
        // host_command_notify's edge-triggered fire -- matters if more than
        // one command is ever in flight at once: sc_event notify() only
        // wakes a *currently blocked* wait(), so a second command queued
        // while this thread was still processing the first would otherwise
        // sit unnoticed for up to a full countdown_tick. host_control_if's
        // own contract ("one command accepted at a time", DLD 4.1) and
        // submit_host_command()'s blocking-until-acked design mean that
        // cannot actually happen under documented usage -- this is defensive
        // robustness beyond the contract, verified by mutation testing to
        // change nothing observable under it, not a proven-necessary fix.
        // Kept because it is free (equally simple either way) and it mirrors
        // SimPy's Store.get(), which resolves immediately (zero simulated
        // delay) when the store already holds an item at the moment of the
        // call, rather than relying only on future put() notifications.
        if (host_queue.empty()) {
            wait(latency["countdown_tick"], host_command_notify);
        }
        if (!host_queue.empty()) {
            HostCommand cmd = host_queue.front();
            host_queue.pop_front();
            if (cmd.command == "KICK") {
                wait(latency["kick"]);
                countdown_cycles = configured_timeout_cycles;
                metrics["kicks_received"]++;
                transition_counts["ARMED->ARMED"]++;
                cmd.done->notify();
                continue;
            }
            if (cmd.command == "DISARM") {
                wait(latency["disarm"]);
                armed = false;
                countdown_cycles = 0;
                metrics["disarm_count"]++;
                transition_counts["ARMED->DISARMED"]++;
                cmd.done->notify();
                return false;
            }
            // ARM while already armed: no declared transition, no effect.
            cmd.done->notify();
            continue;
        }

        // The wait() timed out with the queue still empty: charge the tick.
        countdown_cycles--;
        if (countdown_cycles > 0) {
            continue;
        }

        wait(latency["expire"]);
        armed = false;
        expired = true;
        metrics["expirations"]++;
        transition_counts["ARMED->EXPIRED"]++;
        set_fsm_state("watchdog_main", "EXPIRED");
        return true;
    }
}

void WatchdogIpModel::run_expired() {
    while (true) {
        while (host_queue.empty()) {
            wait(host_command_notify);
        }
        HostCommand cmd = host_queue.front();
        host_queue.pop_front();
        if (cmd.command == "DISARM") {
            wait(latency["disarm"]);
            expired = false;
            metrics["disarm_count"]++;
            transition_counts["EXPIRED->DISARMED"]++;
            cmd.done->notify();
            return;
        }
        // ARM/KICK while expired: no declared transition, no effect -- the
        // host must disarm before the watchdog can be armed again.
        cmd.done->notify();
    }
}

void WatchdogIpModel::config_intake_process() {
    set_fsm_state("config_intake", "IDLE");
    while (true) {
        while (config_queue.empty()) {
            wait(config_command_notify);
        }
        ConfigCommand cmd = config_queue.front();
        config_queue.pop_front();
        wait(latency["accept_config"]);
        transition_counts["IDLE->APPLY_TIMEOUT"]++;
        set_fsm_state("config_intake", "APPLY_TIMEOUT");
        wait(latency["apply_timeout"]);
        configured_timeout_cycles = cmd.timeout_cycles;
        transition_counts["APPLY_TIMEOUT->IDLE"]++;
        set_fsm_state("config_intake", "IDLE");
        cmd.done->notify();
    }
}
