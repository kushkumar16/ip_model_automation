# Watchdog IP Design-Level Document

## 1. Purpose

The Watchdog IP monitors host liveness. Once armed, it counts down from a
configured timeout; the host must issue a periodic kick before the countdown
reaches zero, or the watchdog expires and asserts an expiry signal to the
system's reset/interrupt controller. This is the standard defensive mechanism
against a hung or runaway host: no application-level liveness check can catch
a host that has stopped running entirely, but a hardware countdown the host
itself must keep resetting can.

## 2. Scope

In scope:

- Arming and disarming the countdown on host command.
- Accepting a kick that reloads the countdown to the configured timeout.
- Counting down once armed, and asserting expiry when the countdown reaches
  zero without an intervening kick.
- Configuring the timeout window's cycle count before arming.
- Latching the expired state until the host acknowledges it.

Out of scope:

- The system's actual reset or power sequencing triggered by expiry; this IP
  only asserts the expiry signal, it does not act on it.
- Rejecting a kick issued "too early" (e.g. far from expiry, which might
  indicate a runaway loop kicking defensively rather than a live host) as a
  distinct fault condition; not modeled.

## 2a. Reset

- Core clock frequency: 500 MHz.
- Reset behavior: clear the countdown, the configured timeout, the expired
  latch, and every metric; return to `DISARMED`.

## 3. Context

A host processor and the peripherals it drives can hang for reasons no
software-level check reliably catches: a stuck interrupt handler, a deadlock,
a runaway loop that never returns to its main scheduling point. A watchdog
timer is the standard hardware answer: software must periodically prove it is
still running by kicking the timer, and a timer that stops being kicked
eventually expires and forces the system's attention through a signal no
software state can suppress.

## 4. Interfaces

Every interface below states a `Wait model:` block — how the requester (IP1)
waits on the responder (IP2) across it — the same convention other IPs' DLDs
use.

### 4.1 Host Control Interface

The host's only way to arm, kick, disarm, or configure the watchdog.

Fields:

- `command`: one of `ARM`, `KICK`, `DISARM`, `CONFIGURE_TIMEOUT`.
- `timeout_cycles`: present only on `CONFIGURE_TIMEOUT`; the new timeout
  window's cycle count.

Timing:

- One command accepted at a time; the watchdog acknowledges each once it has
  actually taken effect, not merely been received.

Wait model:

- Mode: `wait_for_ack_inline`
- Requester: peer
- Waits in: `watchdog_main.ARMED`
- Resumes on: `command_applied`
- Note: `CONFIGURE_TIMEOUT` is acknowledged by the Config Intake FSM instead
  (see 6.2); this wait model covers `ARM`/`KICK`/`DISARM`.

### 4.2 Expiry Interface

The watchdog's only way to tell the system's reset/interrupt controller that
the host failed to kick it in time.

Fields:

- `expired`: level-asserted once the countdown reaches zero without a kick;
  stays asserted until the host disarms.

Timing:

- Asserted one cycle after the countdown reaches zero (the FSM's declared
  1-cycle `assert_expiry` delay, section 6.1/9).

Wait model:

- Mode: `wait_for_ack_inline`
- Requester: this_ip
- Waits in: `watchdog_main.EXPIRED`
- Resumes on: `expiry_acknowledged`
- Note: the watchdog will not re-arm while `EXPIRED` — the host must disarm
  first, which is how it acknowledges the expiry.

## 5. Commands

- `ARM`: arms the watchdog with the currently configured timeout, starting
  the countdown from that value.
- `KICK`: reloads the countdown to the configured timeout; must be issued
  again before the countdown reaches zero, or the watchdog expires.
- `DISARM`: disarms the watchdog, canceling an in-progress countdown or
  clearing an expired latch, whichever applies.
- `CONFIGURE_TIMEOUT`: sets the timeout window's cycle count used the next
  time the watchdog is armed; does not affect a countdown already running.

## 6. FSMs

### 6.1 Watchdog Main FSM

States:

- `RESET`: initialize the countdown, the expired latch, and the metrics.
- `DISARMED`: no countdown running; the watchdog can be armed.
- `ARMED`: counting down from the configured timeout; a kick reloads it.
- `EXPIRED`: the countdown reached zero without a kick; the expiry signal is
  asserted and stays asserted until disarmed.

Transitions:

- `RESET -> DISARMED`: reset deasserted.
- `DISARMED -> ARMED`: `ARM` command accepted; loads the countdown from the
  configured timeout.
- `ARMED -> ARMED`: `KICK` command accepted; reloads the countdown.
- `ARMED -> DISARMED`: `DISARM` command accepted.
- `ARMED -> EXPIRED`: the countdown reaches zero without an intervening kick.
- `EXPIRED -> DISARMED`: `DISARM` command accepted; clears the expired latch.

### 6.2 Config Intake FSM

States:

- `IDLE`: waiting for a `CONFIGURE_TIMEOUT` command.
- `APPLY_TIMEOUT`: store the new timeout value for the next arm.

Transitions:

- `IDLE -> APPLY_TIMEOUT`: `CONFIGURE_TIMEOUT` command accepted.
- `APPLY_TIMEOUT -> IDLE`: new timeout value stored.

## 7. Performance Model Requirements

The SimPy model shall include:

- Watchdog Main process.
- Config Intake process.
- Countdown state, decremented once per cycle while armed.
- Configured timeout register, independent of the running countdown.
- Metrics: kicks received, expirations, arm count, disarm count.

The performance model shall not model the system's actual reset/interrupt
response to expiry; only the expiry signal itself.

## 8. SimPy Functionality Requirements

The generated SimPy delay model shall include enough functionality to
validate the FSM behavior from this DLD. The model must include:

- Arm/kick/disarm API.
- Configure-timeout API, independent of arm/kick/disarm.
- Countdown decrement once per cycle while armed, reloaded by a kick.
- Expiry detection and a latch that persists until disarmed.
- Metrics query API.

Minimum state variables:

- `armed`
- `countdown_cycles`
- `configured_timeout_cycles`
- `expired`

## 9. FSM Timing Model

Clock assumption:

- Core clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation, the
  same convention this repo's other DLDs use.

FSM/process count:

- Total FSM/processes: 2.
- Parallel processes: `Watchdog Main FSM` and `Config Intake FSM` run in
  parallel and do not gate each other directly.
- Sequential dependency: a `CONFIGURE_TIMEOUT` applied by the Config Intake
  FSM only takes effect the next time the Watchdog Main FSM arms; it never
  interrupts a countdown already running.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Watchdog Main FSM | Parallel process | Arm: 1 cycle = 2 ns. Kick: 1 cycle = 2 ns. Disarm: 1 cycle = 2 ns. Countdown tick: 1 cycle = 2 ns, charged once per cycle while armed. Expire: 1 cycle = 2 ns. |
| Config Intake FSM | Parallel process | Accept config: 1 cycle = 2 ns. Apply timeout: 1 cycle = 2 ns. |

End-to-end timing:

- Arm to first countdown tick: `arm 1 = 1 cycle = 2 ns`.
- Expiry latency once the countdown reaches zero: `expire 1 = 1 cycle = 2 ns`.
- A `CONFIGURE_TIMEOUT` takes `accept config 1 + apply timeout 1 = 2 cycles = 4 ns`
  to be stored, independent of the main FSM's own timing.

## 10. Open Items

- Whether a "kick too early" pattern should itself be flagged as a fault is
  explicitly out of scope (see Scope); no threshold for it is stated.
- The system's actual response to the expiry signal (reset, interrupt, or
  both) is outside this IP's boundary; only the signal itself is modeled.
