# Timer IP Design-Level Document

## 1. Purpose

The Timer IP provides SoC timing services such as free-running counters,
one-shot timers, periodic timers, compare-match events, watchdog timeout, and
interrupt generation. The block contains multiple concurrent processes: counter
tick generation, register programming, compare evaluation, interrupt handling,
prescaler update, and optional watchdog supervision.

The performance model must represent concurrent timer processes and the
sequential dependencies between register writes, counter updates, compare
matches, and interrupt delivery.

## 2. Scope

In scope:

- Multiple independent timer channels.
- Free-running counter mode.
- One-shot mode.
- Periodic mode.
- Compare-match events.
- Prescaler configuration.
- Interrupt generation and clear.
- Optional watchdog timeout process.
- Clock-domain and register-write synchronization abstraction.

Out of scope:

- Analog clock behavior.
- Metastability modeling beyond configurable synchronization delay.
- Full APB/AHB signal-level timing.
- Real interrupt controller implementation.

## 3. Context

Timer IPs are common SoC utility blocks. Software configures timer channels over
a register interface. Each channel counts ticks derived from a clock and
prescaler. When a counter reaches a programmed compare value, the channel
generates an event, optionally reloads, and may assert an interrupt.

Some timer processes are naturally parallel: all enabled channels count
concurrently, interrupt aggregation runs independently, and watchdog supervision
may observe a separate heartbeat path. Some operations are sequential:
configuration writes must be synchronized before they affect the timer, compare
match precedes reload, and interrupt clear follows software acknowledgement.

## 4. Interfaces

### 4.1 Register Interface

Type: APB/AHB/AXI-lite style control interface.

Important registers:

- `GLOBAL_CTRL`: global enable, soft reset.
- `TIMER_CTRL[n]`: channel enable, mode, interrupt enable.
- `TIMER_COUNT[n]`: current counter value.
- `TIMER_COMPARE[n]`: compare threshold.
- `TIMER_RELOAD[n]`: reload value for periodic mode.
- `TIMER_PRESCALE[n]`: prescaler divide value.
- `INT_STATUS`: raw and masked interrupt status.
- `INT_CLEAR`: write-one-to-clear interrupt status.
- `WDT_CTRL`: watchdog enable and mode.
- `WDT_TIMEOUT`: watchdog timeout threshold.
- `WDT_KICK`: watchdog heartbeat write.

### 4.2 Tick Clock Interface

Fields:

- `tick_valid`
- `clock_domain`
- `tick_period`

Timing:

- Tick generation may be modeled as a periodic SimPy timeout.
- Prescaler determines when a timer channel observes an effective count tick.

### 4.3 Interrupt Output Interface

Fields:

- `irq_valid`
- `irq_vector`
- `irq_level`
- `irq_clear_seen`

Timing:

- Interrupt may be level or pulse depending on configuration.
- Level interrupt remains asserted until software clears status.

### 4.4 Watchdog Heartbeat Interface

Fields:

- `kick_valid`
- `source_id`
- `kick_time`

Timing:

- Watchdog counter resets on accepted kick.
- Timeout event occurs if no kick is observed before threshold.

## 5. Timer Modes

`FREE_RUNNING`:

- Counter increments continuously while enabled.
- Compare events may occur, but counter does not reload.

`ONE_SHOT`:

- Counter starts from configured value.
- Compare match disables the timer after event generation.

`PERIODIC`:

- Counter reloads after compare match.
- Interrupt/event can repeat.

`WATCHDOG`:

- Counter tracks absence of heartbeat.
- Timeout generates interrupt, reset request, or both depending on config.

## 6. FSMs and Processes

### 6.1 Register Access FSM

Role: Accept software reads/writes and update shadow configuration.

States:

- `RESET`
- `IDLE`
- `DECODE_ACCESS`
- `WRITE_SHADOW`
- `READ_RETURN`
- `ACCESS_ERROR`

Sequential dependencies:

- Register writes update shadow state first.
- Runtime timer logic observes shadow state after synchronization.

Parallel interaction:

- Runs independently from all timer channel counters.

### 6.2 Configuration Synchronizer FSM

Role: Move register shadow configuration into the timer clock domain.

States:

- `WAIT_UPDATE`
- `SYNC_STAGE_0`
- `SYNC_STAGE_1`
- `APPLY_CONFIG`
- `ACK_UPDATE`

Sequential dependencies:

- A software write is not effective until `APPLY_CONFIG`.
- Counter process must use active configuration, not partially written shadow
  configuration.

Parallel interaction:

- Runs in parallel with counter processes.
- May stall mode changes until a safe boundary.

### 6.3 Prescaler FSM

Role: Generate effective timer-channel ticks from the input clock.

States:

- `RESET`
- `WAIT_RAW_TICK`
- `INCREMENT_DIVIDER`
- `EMIT_EFFECTIVE_TICK`
- `LOAD_NEW_DIVIDER`

Sequential dependencies:

- Effective tick occurs only after divider reaches programmed threshold.
- New prescaler value is loaded after configuration synchronization.

Parallel interaction:

- One prescaler process may exist per channel.
- Prescaler processes run in parallel across channels.

### 6.4 Counter FSM

Role: Maintain counter value for each timer channel.

States:

- `RESET`
- `DISABLED`
- `LOAD`
- `COUNT`
- `COMPARE_PENDING`
- `RELOAD`
- `STOPPED`

Sequential dependencies:

- `LOAD` precedes `COUNT`.
- `COUNT` precedes `COMPARE_PENDING` when threshold is reached.
- In one-shot mode, `COMPARE_PENDING -> STOPPED`.
- In periodic mode, `COMPARE_PENDING -> RELOAD -> COUNT`.

Parallel interaction:

- Each timer channel counter runs independently.
- Counter process emits events to compare/event process.

### 6.5 Compare/Event FSM

Role: Detect compare match and publish timer events.

States:

- `WAIT_COUNTER_UPDATE`
- `CHECK_COMPARE`
- `EVENT_ASSERT`
- `EVENT_HOLD`
- `EVENT_CLEAR`

Sequential dependencies:

- Event generation depends on counter update.
- Interrupt status set depends on event assertion.

Parallel interaction:

- Runs per channel or as a shared compare scanner.
- Can run concurrently with register access and other channels.

### 6.6 Interrupt Aggregation FSM

Role: Convert per-channel events into interrupt output.

States:

- `IDLE`
- `COLLECT_EVENTS`
- `APPLY_MASK`
- `ASSERT_IRQ`
- `WAIT_SW_CLEAR`
- `DEASSERT_IRQ`

Sequential dependencies:

- Masking occurs before interrupt assertion.
- Level interrupt deassertion requires software clear.

Parallel interaction:

- Runs in parallel with counter and compare processes.
- Observes events from all channels.

### 6.7 Watchdog FSM

Role: Track heartbeat absence and generate watchdog timeout.

States:

- `RESET`
- `DISABLED`
- `ARMED`
- `WAIT_KICK_OR_TIMEOUT`
- `KICK_RELOAD`
- `TIMEOUT`
- `RECOVERY_WAIT`

Sequential dependencies:

- Watchdog must be armed before timeout monitoring.
- Kick reloads timeout counter before monitoring resumes.
- Timeout action occurs before recovery wait.

Parallel interaction:

- Runs independently of normal timer channels.
- Shares interrupt aggregation if watchdog interrupt is enabled.

### 6.8 Debug Freeze FSM

Role: Stop or resume counters during debug halt.

States:

- `RUN`
- `FREEZE_REQUEST`
- `FROZEN`
- `RESUME_REQUEST`

Sequential dependencies:

- Freeze request must be acknowledged before counters stop.
- Resume request must be acknowledged before counters restart.

Parallel interaction:

- Broadcasts freeze state to all counter processes.

## 7. Process Relationship Summary

Sequential channel event path:

```text
Register Write
  -> Configuration Synchronizer
  -> Prescaler Effective Tick
  -> Counter Update
  -> Compare/Event
  -> Interrupt Aggregation
  -> Software Clear
```

Parallel processes:

```text
Register Access
Configuration Synchronizer
Prescaler per channel
Counter per channel
Compare/Event per channel
Interrupt Aggregation
Watchdog
Debug Freeze
```

## 8. Resources and Queues

Queues/events:

- Register write update queue.
- Effective tick event per channel.
- Compare event queue.
- Interrupt pending queue.
- Watchdog kick event queue.
- Debug freeze event.

Resources:

- Register bus access port.
- Synchronizer latency pipeline.
- Interrupt output line.
- Per-channel counter state.

## 9. Performance Model Requirements

The SimPy model shall include:

- A register-access process.
- A configuration synchronization process.
- One prescaler process per timer channel.
- One counter process per timer channel.
- One compare/event process per timer channel or a shared scanner.
- One interrupt aggregation process.
- Optional watchdog and debug-freeze processes.
- Configurable synchronization latency.
- Metrics for event latency, interrupt latency, missed/merged events, counter
  drift due to prescaler, watchdog timeout, and software clear latency.

The performance model should use SimPy events or stores for tick, compare, and
interrupt communication between processes.

## 10. SimPy Functionality Requirements

The generated SimPy delay model shall include enough functionality to validate
the FSM behavior from this DLD. The model must include:

- Register model.
- Timer channel state.
- Counter step API.
- Compare-match behavior.
- One-shot and periodic reload behavior.
- Interrupt status/mask/clear behavior.
- Watchdog kick and timeout behavior.
- Debug freeze hold/resume behavior.
- Register-to-active configuration synchronization latency.
- Queues/events between prescaler, counter, compare, and interrupt processes.

The model may be cycle-stepped or event-stepped, but all generated timing
values must come from this DLD and the reviewed template.

## 11. FSM Timing Model

Clock assumption:

- Timer clock frequency: 500 MHz.
- Cycle time: 2 ns.
- All delays below are hard-coded starter values for model generation.

FSM/process count:

- Total FSM/processes: 8.
- Parallel processes: register access, configuration synchronizer, prescaler
  per channel, counter per channel, compare/event per channel, interrupt
  aggregation, watchdog, and debug freeze.
- Sequential dependency: a register write is visible to timer counters only
  after synchronization; counter update precedes compare event; compare event
  precedes interrupt aggregation.

Per-process timing:

| FSM/process | Runs as | Delay model |
| --- | --- | --- |
| Register Access FSM | Parallel bus process | Decode: 2 cycles = 4 ns. Register write/read: 2 cycles = 4 ns. Error response: 2 cycles = 4 ns. |
| Configuration Synchronizer FSM | Parallel clock-domain process | Two-stage sync: 2 cycles = 4 ns. Apply config: 2 cycles = 4 ns. Ack update: 1 cycle = 2 ns. |
| Prescaler FSM | Parallel per-channel process | Raw tick observe: 1 cycle = 2 ns. Divider increment: 1 cycle = 2 ns. Effective tick emit on terminal count: 1 cycle = 2 ns. |
| Counter FSM | Parallel per-channel process | Load: 1 cycle = 2 ns. Count update: 1 cycle = 2 ns. Reload/stop decision: 1 cycle = 2 ns. |
| Compare/Event FSM | Parallel per-channel or scanner process | Compare check: 2 cycles = 4 ns. Event assert: 1 cycle = 2 ns. Event clear: 1 cycle = 2 ns. |
| Interrupt Aggregation FSM | Parallel shared process | Collect events: 2 cycles = 4 ns. Apply mask: 2 cycles = 4 ns. Assert IRQ: 2 cycles = 4 ns. Deassert after clear: 1 cycle = 2 ns. |
| Watchdog FSM | Parallel watchdog process | Kick reload: 2 cycles = 4 ns. Timeout detect: 1 cycle = 2 ns. Timeout action publish: 2 cycles = 4 ns. |
| Debug Freeze FSM | Parallel broadcast process | Freeze request: 2 cycles = 4 ns. Freeze acknowledge: 2 cycles = 4 ns. Resume acknowledge: 2 cycles = 4 ns. |

Configuration-to-interrupt delay:

- Register write to active config:
  `decode 2 + write 2 + sync 2 + apply 2 + ack 1 = 9 cycles = 18 ns`.
- Effective tick to interrupt:
  `prescaler emit 1 + counter update 1 + compare check 2 + event assert 1 + collect 2 + mask 2 + irq 2 = 11 cycles = 22 ns`.
- Periodic timer event interval is primarily controlled by programmed compare,
  reload, and prescale values. Control-path overhead per event is
  `11 cycles = 22 ns`.

Sequential/parallel relationship:

```text
Sequential event path:
  register write -> config sync -> effective tick -> counter update
    -> compare event -> interrupt aggregation -> IRQ visible

Parallel:
  all channel prescalers/counters
  register access
  watchdog
  debug freeze
  interrupt aggregation
```

## 12. Open Items

- Number of timer channels.
- Counter width.
- Interrupt mode: pulse or level.
- Prescaler width and update timing.
- Watchdog timeout action: interrupt, reset, or both.
- Debug freeze policy per channel.
