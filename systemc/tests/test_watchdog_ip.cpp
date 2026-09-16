// Real, compiled SystemC testbench for WatchdogIpModel, validated against
// templates/watchdog_ip.template.yaml's test_scenarios -- the SystemC
// counterpart to tests/test_watchdog_ip.py, run independently (no shared
// process/state with the Python suite; each backend is validated for real on
// its own toolchain).
//
// SystemC's kernel is elaborated once per process, so every scenario below
// gets its own WatchdogIpModel instance and its own driver SC_THREAD, all
// constructed before the single sc_start() call and running concurrently
// within one shared simulated timeline -- not one process per scenario, and
// not scenarios sharing a model instance.

#include <systemc.h>

#include <iostream>
#include <string>
#include <vector>

#include "../models/watchdog_ip.h"

struct TestResult {
    std::string name;
    bool passed = false;
    std::vector<std::string> failures;
};

inline std::string stringify(const std::string& value) { return value; }
inline std::string stringify(long value) { return std::to_string(value); }
inline std::string stringify(int value) { return std::to_string(value); }
inline std::string stringify(double value) { return std::to_string(value); }

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
// template test_scenarios[0]: kick_before_expiry_prevents_expiration.
//
// Also the SystemC-side regression check for the request-timing bug the
// SimPy port's dogfooding found: a design that only re-checked its command
// queue on an *edge-triggered* event fire (rather than re-checking queue
// state before every wait()) would let a second queued command sit unhandled
// for up to a full tick. Sustained kicking here would still eventually
// expire under that bug, the same symptom the SimPy fix's regression test
// targets.
// ------------------------------------------------------------------------ //
SC_MODULE(KickPreventsExpiryScenario) {
    WatchdogIpModel watchdog;
    TestResult result;

    SC_CTOR(KickPreventsExpiryScenario) : watchdog("watchdog") {
        result.name = "kick_before_expiry_prevents_expiration";
        SC_THREAD(drive);
    }

    void drive() {
        watchdog.configure_timeout(20);
        watchdog.arm();
        for (int i = 0; i < 20; i++) {
            wait(1, SC_NS);
            watchdog.kick();
        }
        CHECK_EQ(result, watchdog.metrics["kicks_received"], 20L, "kicks_received");
        CHECK_EQ(result, watchdog.metrics["expirations"], 0L, "expirations");
        CHECK_EQ(result, watchdog.fsm_state["watchdog_main"], std::string("ARMED"), "fsm_state");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[1]: countdown_reaches_zero_without_kick_expires.
// ------------------------------------------------------------------------ //
SC_MODULE(ExpiresThenDisarmScenario) {
    WatchdogIpModel watchdog;
    TestResult result;

    SC_CTOR(ExpiresThenDisarmScenario) : watchdog("watchdog") {
        result.name = "countdown_reaches_zero_without_kick_expires";
        SC_THREAD(drive);
    }

    void drive() {
        watchdog.configure_timeout(5);
        watchdog.arm();
        sc_time armed_at = sc_time_stamp();

        // Wait until expiry is observed via the fsm_state_changed event
        // (the SimPy test traces this with env.step(); SystemC's equivalent
        // is watching the model's own transition-notification event).
        while (watchdog.fsm_state["watchdog_main"] != "EXPIRED") {
            wait(watchdog.fsm_state_changed);
        }
        sc_time expired_at = sc_time_stamp();

        watchdog.disarm();

        CHECK_EQ(result, watchdog.metrics["expirations"], 1L, "expirations");
        CHECK_EQ(result, watchdog.metrics["disarm_count"], 1L, "disarm_count");
        CHECK_EQ(
            result, watchdog.fsm_state["watchdog_main"], std::string("DISARMED"),
            "disarm did not clear the expired latch"
        );
        // 5 countdown_tick cycles + the 1-cycle expire delay declared in the
        // template's watchdog_main.expire operation.
        CHECK_EQ(
            result, (expired_at - armed_at).to_default_time_units(), sc_time(6, SC_NS).to_default_time_units(),
            "expiry did not land at the declared countdown + expire delay"
        );
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// template test_scenarios[2]: configure_timeout_flows_independently_of_countdown.
// ------------------------------------------------------------------------ //
SC_MODULE(ConfigureTimeoutIndependentScenario) {
    WatchdogIpModel watchdog;
    TestResult result;

    SC_CTOR(ConfigureTimeoutIndependentScenario) : watchdog("watchdog", 1, 1, 1, 1, 1, 1, 1, 1, 5) {
        result.name = "configure_timeout_flows_independently_of_countdown";
        SC_THREAD(drive);
    }

    void drive() {
        // default_timeout_cycles=5 in the constructor above: the running
        // countdown (5, then 3 after 2 ticks) must reach zero and expire
        // well within this scenario's 10ns wait, unaffected by the later
        // configure_timeout(8) below.
        watchdog.arm();
        wait(2, SC_NS);
        sc_time requested_at = sc_time_stamp();
        watchdog.configure_timeout(8);
        sc_time apply_elapsed = sc_time_stamp() - requested_at;

        // The running countdown (5 default, now at 3 after 2 ticks) is
        // unaffected: 3 more cycles brings it to zero and expiry, not 8.
        wait(10, SC_NS);

        CHECK_EQ(result, watchdog.fsm_state["config_intake"], std::string("IDLE"), "config_intake fsm_state");
        CHECK_EQ(result, watchdog.configured_timeout_cycles, 8, "configured_timeout_cycles");
        CHECK_EQ(
            result, apply_elapsed.to_default_time_units(), sc_time(2, SC_NS).to_default_time_units(),
            "accept_config + apply_timeout did not take the declared 2 cycles"
        );
        CHECK_EQ(
            result, watchdog.metrics["expirations"], 1L, "the stored config must not have reset the running countdown"
        );
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// EXPIRED -> DISARMED is the only declared exit; ARM/KICK while expired have
// no declared transition and must have no effect (DLD 6.1).
// ------------------------------------------------------------------------ //
SC_MODULE(NoEffectWhileExpiredScenario) {
    WatchdogIpModel watchdog;
    TestResult result;

    SC_CTOR(NoEffectWhileExpiredScenario) : watchdog("watchdog") {
        result.name = "no_effect_while_expired";
        SC_THREAD(drive);
    }

    void drive() {
        watchdog.configure_timeout(3);
        watchdog.arm();
        while (watchdog.fsm_state["watchdog_main"] != "EXPIRED") {
            wait(watchdog.fsm_state_changed);
        }
        watchdog.arm();   // ignored while EXPIRED
        watchdog.kick();  // ignored while EXPIRED

        CHECK_EQ(result, watchdog.fsm_state["watchdog_main"], std::string("EXPIRED"), "fsm_state");
        CHECK_EQ(result, watchdog.metrics["expirations"], 1L, "expirations");
        CHECK_EQ(result, watchdog.metrics["arm_count"], 1L, "the ignored re-arm while EXPIRED must not count");
        CHECK_EQ(result, watchdog.metrics["kicks_received"], 0L, "the ignored kick while EXPIRED must not count");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// No declared transition from DISARMED on KICK/DISARM (DLD 6.1): there is
// nothing to kick or cancel yet, so both are accepted with no effect.
// ------------------------------------------------------------------------ //
SC_MODULE(NoEffectWhileDisarmedScenario) {
    WatchdogIpModel watchdog;
    TestResult result;

    SC_CTOR(NoEffectWhileDisarmedScenario) : watchdog("watchdog") {
        result.name = "no_effect_while_disarmed";
        SC_THREAD(drive);
    }

    void drive() {
        watchdog.kick();
        watchdog.disarm();

        CHECK_EQ(result, watchdog.fsm_state["watchdog_main"], std::string("DISARMED"), "fsm_state");
        CHECK_EQ(result, watchdog.metrics["kicks_received"], 0L, "kicks_received");
        CHECK_EQ(result, watchdog.metrics["disarm_count"], 0L, "disarm_count");
        result.passed = result.failures.empty();
    }
};

// ------------------------------------------------------------------------ //
// ARMED -> ARMED has no declared transition on ARM (only on KICK); a
// redundant ARM while already armed must not reload the countdown or count
// as a second arm (DLD 6.1).
// ------------------------------------------------------------------------ //
SC_MODULE(NoEffectWhileArmedScenario) {
    WatchdogIpModel watchdog;
    TestResult result;
    int countdown_after_first_arm = -1;
    int countdown_after_redundant_arm = -1;

    SC_CTOR(NoEffectWhileArmedScenario) : watchdog("watchdog") {
        result.name = "no_effect_while_already_armed";
        SC_THREAD(drive);
    }

    void drive() {
        watchdog.configure_timeout(5);
        watchdog.arm();
        countdown_after_first_arm = watchdog.countdown_cycles;
        wait(2, SC_NS);  // let a couple of ticks decrement the countdown below 5
        watchdog.arm();  // redundant, ignored -- must not reload the countdown back to 5
        countdown_after_redundant_arm = watchdog.countdown_cycles;
        watchdog.disarm();

        CHECK_EQ(result, watchdog.fsm_state["watchdog_main"], std::string("DISARMED"), "fsm_state");
        CHECK_EQ(result, watchdog.metrics["arm_count"], 1L, "the redundant ARM must not count as a second arm");
        CHECK_EQ(result, countdown_after_first_arm, 5, "countdown_after_first_arm");
        if (countdown_after_redundant_arm >= 5) {
            result.failures.push_back("the redundant ARM must not reload the countdown");
        }
        result.passed = result.failures.empty();
    }
};

int sc_main(int argc, char* argv[]) {
    KickPreventsExpiryScenario s1("s1");
    ExpiresThenDisarmScenario s2("s2");
    ConfigureTimeoutIndependentScenario s3("s3");
    NoEffectWhileExpiredScenario s4("s4");
    NoEffectWhileDisarmedScenario s5("s5");
    NoEffectWhileArmedScenario s6("s6");

    sc_start(60, SC_NS);

    std::vector<TestResult*> results = {&s1.result, &s2.result, &s3.result, &s4.result, &s5.result, &s6.result};
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
