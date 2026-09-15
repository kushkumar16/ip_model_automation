#pragma once

#include <deque>
#include <map>
#include <string>

#include <systemc.h>

// SystemC delay model for watchdog_ip, ported from the reviewed SimPy model
// (src/ip_model_automation/watchdog_ip.py) against the same contract
// (templates/watchdog_ip.template.yaml). Every cycle count from the template
// is represented as 1 SC_NS here, the same dimensionless-cycle convention the
// SimPy model itself uses (env.timeout(N) with no unit) -- the template's
// cycle_time_ns is a separate reporting concern neither model applies
// internally.
class WatchdogIpModel : public sc_module {
  public:
    SC_HAS_PROCESS(WatchdogIpModel);
    explicit WatchdogIpModel(
        sc_module_name name,
        int reset_latency = 1,
        int arm_latency = 1,
        int kick_latency = 1,
        int disarm_latency = 1,
        int countdown_tick_latency = 1,
        int expire_latency = 1,
        int accept_config_latency = 1,
        int apply_timeout_latency = 1,
        int default_timeout_cycles = 100
    );

    // Host-facing command API. Each call blocks until the command has
    // actually taken effect (mirrors the SimPy model's wait_for_ack_inline
    // contract), so it must run inside a SystemC thread -- a testbench
    // SC_THREAD, not sc_main directly.
    void arm();
    void kick();
    void disarm();
    void configure_timeout(int timeout_cycles);

    std::map<std::string, long> get_metrics() const { return metrics; }

    // Notified every time any fsm_state entry changes. The SimPy model has no
    // equivalent (its tests trace transitions via env.step() single-stepping
    // instead); a SystemC testbench has no such step primitive, so this is
    // the idiomatic substitute for precisely timing a transition that isn't
    // itself the direct result of a blocking command call (e.g. an autonomous
    // expiry).
    sc_event fsm_state_changed;

    // Mirrors the SimPy model's public state for a testbench to inspect.
    std::map<std::string, std::string> fsm_state;
    std::map<std::string, long> metrics;
    std::map<std::string, long> transition_counts;
    int configured_timeout_cycles;
    int countdown_cycles = 0;
    bool armed = false;
    bool expired = false;

  private:
    struct HostCommand {
        std::string command;
        sc_event* done;
    };
    struct ConfigCommand {
        int timeout_cycles;
        sc_event* done;
    };

    void watchdog_main_process();
    // Returns true if the countdown reached zero (caller must then run
    // run_expired), false if the host disarmed first.
    bool run_armed();
    void run_expired();
    void config_intake_process();

    void submit_host_command(const std::string& command);
    void set_fsm_state(const std::string& fsm, const std::string& state);

    std::map<std::string, sc_time> latency;
    std::deque<HostCommand> host_queue;
    sc_event host_command_notify;
    std::deque<ConfigCommand> config_queue;
    sc_event config_command_notify;
};
