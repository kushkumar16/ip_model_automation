# Proposal: Abort and NVMe Reset Support in the SimPy Framework

Oct 20, 2026 · Kush

**Contents**

- [1. Summary](#1-summary)
- [2. Scope](#2-scope)
- [3. What changes](#3-what-changes)
- [4. How abort works](#4-how-abort-works)
- [5. How reset works](#5-how-reset-works)
- [6. Changes expected in current HW IP models](#6-changes-expected-in-current-hw-ip-models)
- [7. Host interface](#7-host-interface)
- [8. FW modelling complexity](#8-fw-modelling-complexity)
- [9. What the model will prove](#9-what-the-model-will-prove)
- [10. Plan](#10-plan)
- [11. Risks and decisions](#11-risks-and-decisions)
- [Appendix A: Pseudocode](#appendix-a-pseudocode)

Diagrams are PNG renders; each has an editable draw.io source alongside it in [`nvme_abort_reset_img/`](nvme_abort_reset_img/).

## 1. Summary

- **Goal:** extend our SimPy performance framework to model NVMe **Abort** and **NVMe resets** (controller reset) while fio traffic is running. Architecture changes in these flows can then be checked before RTL and FW exist.
- **Approach:**
  - Keep the I/O path in HW exactly as today.
  - Add a small control-plane FW that receives the Abort command and reset interrupts, and acts on HW through registers.
  - Add exception handling to the HW IPs, and a simple host interface that injects an Abort or a reset into running fio traffic.
- **Outcome:** every architecture-exploration change comes with assurance that it does not break the abort and reset flows. Where a change does affect them, the impact is caught and validated with the arch model.
- **Impact on the performance model:** minimal on performance results, because FW stays outside the I/O path. Adding exception handling to most HW IP blocks does make the model more complex, and it will increase model execution time.

## 2. Scope

| In scope | Out of scope (later) |
| --- | --- |
| Abort command | Backend and NAND (replaced by a timing stub) |
| Controller reset (CC.EN) | NVM Subsystem Reset (NSSR), PCIe resets (FLR, PERST#) and shutdown |
| Minimal FW handling: an Abort handler and a reset handler | FW behaviour beyond these two handlers |
| Abort and reset, each injected during fio at full queue depth | Mixed abort-and-reset cases (complex to handle) |
| | SQ/CQ deletion during reset (needs handling on all three sides: host, HW and FW) |
| | Multiple controllers, multi-core FW |

## 3. What changes

Three additions, marked *new* or *extended* in the diagram:

1. **FW control plane** (one core): an Abort handler and a reset handler.
2. **HW IPs:**
   - Cmd Context with an abort flag
   - cancellable data transfer and media stub
   - fetch stop and completion gate
   - FW control registers
3. **Host:** a simple trigger interface that injects an Abort or a reset into a running fio job, and records how each command completed.

![Architecture: host, NVMe IP, FW](nvme_abort_reset_img/architecture.png)

*Editable source: [architecture.drawio](nvme_abort_reset_img/architecture.drawio)*

## 4. How abort works

The host's Abort goes to FW. FW tells HW which command to stop, HW stops it and confirms, and the host receives two completions: one for the aborted command and one for the Abort itself.

![Abort during fio](nvme_abort_reset_img/abort_sequence.png)

*Editable source: [abort_sequence.drawio](nvme_abort_reset_img/abort_sequence.drawio)*

Only the aborted command changes course; all other fio I/O completes normally.

## 5. How reset works

The reset reaches FW as an interrupt. FW tells HW to stop and cancel everything, HW confirms it is idle, and FW signals the host that the reset is done.

![Controller reset during fio](nvme_abort_reset_img/reset_sequence.png)

*Editable source: [reset_sequence.drawio](nvme_abort_reset_img/reset_sequence.drawio)*

Commands dropped by the reset get no completion; the host resubmits them once the controller is re-enabled.

## 6. Changes expected in current HW IP models

| # | Change | HW IP | What it does | Details | Owner | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Abort command dispatch to FW | Dispatcher | Recognises the Abort admin command and hands it to FW instead of the I/O path | | | |
| 2 | FW register write interface | All IPs (via FW control registers) | Lets FW tell HW when to act: abort a command, stop fetch, gate completions, cancel or force-stop work, reset queues | | | |
| 3 | Abort check interface | Cmd Context, datapath | Tells FW whether a command can still be aborted (aborted, too late, not found), and when the abort is done | | | |
| 4 | Reset acknowledge interface | All IPs | Lets FW check that every HW IP has stopped and gone idle before the reset completes | | | |
| 5 | Reset interrupt to FW | Register block | Raises an interrupt to FW on CC.EN 1→0 (reset) and CC.EN 0→1 (re-enable) | | | |
| 6 | Cmd Context with abort flag | New block | Tracks every in-flight command and its stage; each stage checks the abort flag; on reset, FW clears all entries and frees their tags and buffers | | | |
| 7 | Fetch stop | SQ fetch + arbiter | Stops fetching new commands when FW asks | | | |
| 8 | Cancellable datapath | DMA engine, media stub | Stops a command at the next chunk or immediately; supports cancel-all and force-stop for reset | | | |
| 9 | Completion gate | CQ poster + MSI-X | Blocks all completions and interrupts to the host during reset | | | |
| 10 | Completion status field | CQ poster | Completions carry a status code (for example 0x07 for aborted) instead of success only | | | |
| 11 | Queue and state reset | Register block, queues, Cmd Context | Returns queue pointers to defaults and clears all command state after reset | | | |
| 12 | Admin queue support | SQ fetch + arbiter, CQ poster | Fetches from the admin SQ (SQ 0) alongside the I/O SQs, and posts admin completions (such as the Abort's) to the admin CQ | | | |

## 7. Host interface

The host stays a fio traffic generator. One new object, an **injector**, lets a test say when to send an Abort or a reset. The host does not watch for slow or stuck commands, and never decides on its own to abort or reset; an Abort or reset happens only when the test schedules it.

```python
job = FioJob(rw="randrw", rwmixread=70, bs=4096, iodepth=32, runtime_us=50_000)

inj = Injector(host)
inj.abort(at_us=1_200, target="oldest")      # abort one in-flight command
inj.abort(at_us=2_000, target=(1, 7))        # or a specific (SQ, CID)
inj.reset(at_us=3_000, kind="CC_EN")         # controller reset

run(job, inj)
```

| Call | What the host does | What the host gets back |
| --- | --- | --- |
| `abort(at, target)` | At the given time, picks the target among in-flight commands and submits Abort(SQ, CID) on the admin queue | The target's completion (0x07, or normal success if it was too late) and the Abort's own completion |
| `reset(at, kind)` | Pauses fio submissions, writes CC.EN = 0, waits for CSTS.RDY = 0, re-enables, recreates the queues, resubmits the in-flight I/O, resumes fio | CSTS.RDY = 0 then 1; no completions for the dropped commands |

**Admin queue (new on the host side).** The current host has only I/O submission and completion queues. Abort is an admin command, so the host needs an admin SQ/CQ pair:

- set up at init and again after every reset (AQA, ASQ, ACQ);
- the Abort is submitted on the admin SQ;
- its completion is reaped from the admin CQ.

On the completion path, each completion now carries a status:

- **Success:** handled as today.
- **0x07:** recorded as aborted (not retried).
- **Anything else:** recorded as an error.

Results report a count per status alongside IOPS and latency.

Triggers are time-based to start with. Triggering on a model event, such as "when CID 7 starts its data transfer", can be added later for precise race cases.

## 8. FW modelling complexity

We start with a **basic FW**: one handler for the Abort command and one for the reset request. Each is a short, fixed sequence of register writes, handling one request at a time. That is enough to exercise the HW and host changes and keeps the model simple.

Complexity rises quickly beyond that:

| Stage | FW in the model | Complexity |
| --- | --- | --- |
| Basic (this proposal) | Abort handler and reset handler; fixed sequences; one request at a time | Low |
| Timing-accurate | Real handler execution times, interrupt latency and register access cost | Medium |
| More reset and error cases | NSSR, queue deletion, shutdown, mixed abort and reset, errors during reset | High |
| Concurrency and priority | Several requests in flight, preemption, and priority between abort, reset and admin work | High |

Once timing, more reset cases and priority between requests come in, modelling FW becomes the task of writing the actual FW. At that point, running real FW code against the HW models is the better path than growing the model's FW.

## 9. What the model will prove

- No command is lost, duplicated or completed twice, under abort and under reset.
- Nothing reaches host memory after the reset gate.
- No resource leaks after repeated resets.
- Reset finishes within the NVMe time limit (CAP.TO), even with a stuck data transfer.
- Success-path performance results stay essentially unchanged.

This is checked by targeted abort and reset scenarios injected during fio.

## 10. Plan

1. Spec and register map
2. FW control plane in SimPy
3. HW IP exception paths
4. Host trigger interface, admin queue and completion handling
5. Test scenarios and automated checks
6. Regression against today's results, and review

**Checkpoint after step 4:** demonstrate basic abort and reset during fio, then make a go/no-go decision before the full scenario suite.

## 11. Risks and decisions

| Item | Why it matters | Decision or ask |
| --- | --- | --- |
| Reset exposure window | Between the host's reset and FW's first register write, HW may still send completions to the host | Decide whether HW blocks completions on reset by itself (a HW change) or FW response time is enough. The model will quantify the window |
| Model complexity and run time | Exception handling in most HW IP blocks adds code paths and checks, which slows simulation | Keep the checks switchable so performance-only runs stay fast |
| FW and register timing | FW handling time and register access latency start as estimates, so reset duration and abort latency from the model are approximate | Treat timing results as relative, not absolute, until calibrated against FW profiling or RTL measurements |
| Backend and NAND | High complexity: NAND operations cannot be cancelled, and the backend may add FW cores | Plan as a separate follow-on phase |

## Appendix A: Pseudocode

The pseudocode below is for readers who want the mechanics behind the abort and reset diagrams. Each block has a short table of what its functions do.

### A.1 Abort: NVMe IP (HW)

| Function | What it does |
| --- | --- |
| `dispatcher` | Existing dispatcher with one new branch: admin commands (the Abort) go to FW as an interrupt. I/O is recorded in the Cmd Context and continues down the HW path as today. New work is refused while FW has stopped the datapath |
| `next_stage` | Moves a command to its next stage (data transfer or media stub), or to completion when no stages are left. Runs the abort check first |
| `boundary` | The abort check at every stage boundary. If the command's abort flag is set, it posts the 0x07 completion and reports the outcome to FW |
| `media_stub` | Stand-in for backend and NAND: a cancellable delay per command |
| `dma_job` | Existing data transfer, made cancellable: stops at a chunk boundary on abort or cancel-all, and can be force-stopped |
| `on_abort_req` | HW reaction to FW writing ABORT_REQ. Reports NOT_FOUND or TOO_LATE, or sets the abort flag and stops the command now (queued or in the media stub) or at the next chunk (mid-transfer) |
| `cq_post` | The only place an I/O completion is written. Claims each command once, so there is never a double completion; drops the write while FW gates completions; then frees the command |
| `report` | Queues the abort outcome in the ABORT_STS FIFO and raises ABORT_DONE to FW |

```python
# HW Cmd Context (new): (sqid, cid) -> state, steps, abort_pending, cancel_req

def dispatcher(cmd):                       # existing IP, new branches
    if DP_CTRL.stop_new: return            # reset in progress: refuse new work
    if cmd.sqid == 0:                      # admin (Abort and others) -> FW
        return fw.irq("ADMIN_CMD", cmd)
    t = tbl.add(cmd, state=DISPATCHED)     # I/O stays in HW, as today
    t.steps = [dma_job, media_stub] if cmd.is_write else [media_stub, dma_job]
    io_pipe.put(t)                         # existing pipe worker calls next_stage(t)

def next_stage(t):
    if not boundary(t): return             # aborted at this boundary
    if t.steps: t.proc = env.process(t.steps.pop(0)(t))
    else: cq_post(t, SUCCESS)

def boundary(t):                           # abort check between stages
    if not t.abort_pending: return True
    claimed = cq_post(t, ABORT_REQUESTED)  # target CQE 0x07 from HW
    report(t.key, "ABORTED" if claimed else "TOO_LATE")
    return False

def media_stub(t):                         # stand-in for backend + NAND
    t.state = SERVICE; stub_active.add(t)
    try:
        yield env.timeout(t.service_time)
    except simpy.Interrupt:
        return                             # abort or reset: interrupter handles it
    finally:
        stub_active.discard(t)
    next_stage(t)

def dma_job(t):                            # existing transfer, now cancellable
    t.state = DMA_ACTIVE; dma_active.add(t)
    try:
        for chunk in t.chunks:
            if t.abort_pending or t.cancel_req: break    # chunk boundary
            yield env.timeout(chunk_time(chunk)) | hard_stop_evt
            if hard_stop_evt.triggered: return           # reset fallback
    finally:
        dma_active.discard(t)
    if t.cancel_req: return                # reset: no completion
    next_stage(t)                          # boundary() aborts it if flagged

def on_abort_req(key):                     # FW wrote ABORT_REQ
    t = tbl.get(key)
    if t is None: return report(key, "NOT_FOUND")
    if t.state == CQE_PENDING: return report(key, "TOO_LATE")
    t.abort_pending = True
    if t.state == DISPATCHED:
        io_pipe.remove(t); boundary(t)
    elif t.state == SERVICE and t.proc.is_alive:
        t.proc.interrupt("abort"); boundary(t)
    # DMA_ACTIVE: dma_job stops at the next chunk, then boundary() reports

def cq_post(t, status):                    # the only writer of I/O CQEs
    if not tbl.claim(t): return False      # atomic -> CQE_PENDING, once
    if not CQ_CTRL.gate:                   # FW gates completions during reset
        cq_for(t.sqid).post(cid=t.cid, sqid=t.sqid, status=status)
        msix.maybe_fire(t.sqid)
    tbl.free(t)                            # DONE: tag and buffers released
    return True

def report(key, result):                   # ABORT_STS is a FIFO
    abort_sts.put((key, result)); fw.irq("ABORT_DONE")
```

### A.2 Abort: FW

| Function | What it does |
| --- | --- |
| `on_admin_cmd` | Runs when the dispatcher hands FW an admin command. Non-Abort commands go to the existing admin handling. For an Abort: rejects it with 0x03 if too many Aborts are outstanding (ACL); completes it at once as "not aborted" if it targets the admin queue or a command already being aborted; otherwise records it and writes ABORT_REQ to HW. It does not wait for the result |
| `on_abort_done` | Runs when HW raises ABORT_DONE. Takes one outcome from ABORT_STS and posts the Abort's own completion: DW0 = 0 if aborted, 1 if not. Does nothing if a reset has already taken over |

```python
def on_admin_cmd(cmd):                     # IRQ from the dispatcher
    yield from core.run(cyc.admin)
    if fw_state != READY: return           # reset in progress: no CQE
    if cmd.opc != ABORT:
        return (yield from handle_admin(cmd))    # existing admin handling
    if aborts_out >= ACL + 1:
        return admin_cqe(cmd, sct=1, sc=0x03)    # Abort Command Limit Exceeded
    target = (cmd.cdw10 & 0xFFFF, cmd.cdw10 >> 16)   # (SQID, CID)
    if target[0] == 0 or target in pending:      # admin target, or already aborting
        return admin_cqe(cmd, sct=0, sc=0, dw0=1)  # not aborted
    aborts_out += 1; pending[target] = cmd
    yield from reg_write(ABORT_REQ, target)      # costs register latency

def on_abort_done():                       # IRQ from HW
    key, result = yield from reg_read(ABORT_STS) # pops one FIFO entry
    cmd = pending.pop(key, None)
    if cmd is None or fw_state != READY: return
    aborts_out -= 1
    admin_cqe(cmd, sct=0, sc=0, dw0=0 if result == "ABORTED" else 1)
```

### A.3 Reset: NVMe IP (HW)

| Function | What it does |
| --- | --- |
| `on_host_reg_write` | Register block reaction to the host. CC.EN 1→0 raises CTRL_RESET to FW; CC.EN 0→1 raises CTRL_ENABLE. Doorbells are ignored while the controller is not ready. Optionally gates completions immediately (the HW auto-gate option under evaluation) |
| `on_fw_reg_write` | HW reaction to FW's control registers. Stores the value, which fetch, dispatcher and CQ poster read directly. Cancel-all empties the dispatcher queue, interrupts media-stub work and tells transfers to stop at their next chunk. Also: force-stop a stalled transfer, re-arm after reset, reset queues, clear the Cmd Context |
| `dp_sts` | Datapath status that FW polls: busy while any transfer, media-stub operation or queued command remains |

```python
def on_host_reg_write(reg, val):           # register block
    if reg == CC and CC.EN == 1 and val.EN == 0:
        CC.EN = 0; fw.irq("CTRL_RESET")
        if HW_AUTO_GATE: CQ_CTRL.gate = 1  # design option under study (see Risks)
    elif reg == CC and CC.EN == 0 and val.EN == 1:
        CC.EN = 1; fw.irq("CTRL_ENABLE")
    elif is_doorbell(reg) and CSTS.RDY == 1:
        ring_doorbell(reg, val)            # existing path; ignored while not ready

def on_fw_reg_write(reg, val):             # FW control registers
    regs[reg] = val                        # FETCH_CTRL.stop, CQ_CTRL.gate, DP_CTRL.*
    if reg == DP_CTRL:
        if val.cancel_all:
            io_pipe.clear()                # queued, not started
            for t in list(stub_active):
                if t.proc.is_alive: t.proc.interrupt("reset")
            for t in dma_active: t.cancel_req = True     # stop at next chunk
        if val.hard_stop and not hard_stop_evt.triggered:
            hard_stop_evt.succeed()
        if val == 0: hard_stop_evt = env.event()         # re-arm after reset
    elif reg == Q_RESET: reset_io_queues(); reset_admin_ptrs()
    elif reg == CMD_TBL_CLR: tbl.clear(); free_tags_and_buffers()

def dp_sts():                              # read by FW, or QUIESCED IRQ
    busy = bool(dma_active or stub_active or io_pipe)
    return Sts(busy=busy, dma_busy=len(dma_active), stub_busy=len(stub_active))
```

### A.4 Reset: FW

| Function | What it does |
| --- | --- |
| `on_ctrl_reset` | The reset sequence, run at highest priority. In order: stop fetch, gate completions, cancel all work, poll until idle (force-stop after a timeout), reset queues and command state, drop pending Aborts, then set CSTS.RDY = 0 |
| `on_ctrl_enable` | Runs when the host re-enables the controller. If the reset is still running (early re-enable), it waits for it to finish. Then it reloads the admin queue, lifts the datapath, completion and fetch controls, and sets CSTS.RDY = 1 |

```python
def on_ctrl_reset():                       # IRQ; highest priority
    fw_state = RESETTING                   # pending Aborts will get no CQE
    yield from reg_write(FETCH_CTRL, stop=1)                 # 1. no new commands
    yield from reg_write(CQ_CTRL, gate=1)                    # 2. nothing to host
    yield from reg_write(DP_CTRL, stop_new=1, cancel_all=1)  # 3. cancel work
    t0 = env.now
    while (yield from reg_read(DP_STS)).busy:                # 4. wait for idle
        if env.now - t0 > T_QUIESCE:                         #    stalled transfer
            yield from reg_write(DP_CTRL, stop_new=1, cancel_all=1, hard_stop=1)
            break
        yield env.timeout(POLL)
    yield from reg_write(Q_RESET, io=1, admin=1)             # 5. queues
    yield from reg_write(CMD_TBL_CLR, 1)                     #    and state
    pending.clear(); aborts_out = 0; abort_sts.clear()
    yield from reg_write(CSTS, RDY=0)                        # 6. done
    fw_state = DISABLED

def on_ctrl_enable():
    while fw_state == RESETTING:           # early re-enable: finish the reset first
        yield env.timeout(POLL)
    yield from core.run(cyc.init)
    hw.load_admin_queue(AQA, ASQ, ACQ)
    yield from reg_write(DP_CTRL, 0)       # clears stop/cancel, re-arms hard stop
    yield from reg_write(CQ_CTRL, gate=0)
    yield from reg_write(FETCH_CTRL, stop=0)
    yield from reg_write(CSTS, RDY=1); fw_state = READY
```
