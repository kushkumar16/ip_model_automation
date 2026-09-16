# SimPy Concurrency Patterns

Two real bugs this project's own models hit, both from the same root cause —
racing a resource request (`Store.get()`, `Resource.request()`, ...) against a
timeout inside a loop — plus what the SimPy library's own source (not a
tutorial) actually says about them. Read this before writing any FSM process
that does `yield request_event | timeout_event`.

## The pattern to avoid: a fresh request every iteration

```python
# WRONG -- do not copy this shape.
while True:
    get_event = self._host_q.get()      # a NEW request every time through the loop
    tick_event = self.env.timeout(1)
    result = yield get_event | tick_event
    if get_event in result:
        ...
    else:
        ...  # tick won; get_event is simply dropped here
```

This looks reasonable and is exactly how a naive port of "race two events"
reads. It has two independent failure modes, both verified against SimPy's
real source (`simpy/resources/base.py`, `simpy/events.py`) and both fixed the
same way.

### Failure 1 — the dropped request leaks a queue registration

`Store.get()` is not a query; it is a *registration*.
`base.Get.__init__` does `resource.get_queue.append(self)` — the pending
request sits in the store's own `get_queue` until it either fires or is
explicitly cancelled. When `tick_event` wins the race, nothing about that
removes `get_event` from `get_queue`: `Condition._remove_check_callbacks()`
(the machinery behind `event_a | event_b`) only detaches the *Condition's own*
bookkeeping from the losing event, not the resource-level registration the
`Get` object itself holds. The abandoned `get_event` stays in `get_queue`,
and because `Store` fulfills getters strictly FIFO
(`Store._do_get`: `if self.items: event.succeed(self.items.pop(0))`, called
in `get_queue` order), a *later, unrelated* `put()` gets silently absorbed by
this abandoned request instead of the one currently being waited on. This
project's own `watchdog_ip.py` shipped exactly this bug once: sustained
`kick()` calls were silently swallowed by earlier abandoned countdown-tick
losers, and the watchdog still expired despite being kicked.

### Failure 2 — a fresh request can win and still be lost, same instant

Less obvious, and not caused by leaving anything abandoned: if the resource's
item becomes available in the *exact same simulated instant* the competing
timeout also fires, a **freshly created** request can be genuinely fulfilled
by `_do_get()` and still be excluded from the `Condition`'s result — so
`if get_event in result:` reads `False` even though `get_event.value` already
holds the item, and the naive "else" branch discards it. This is not a
logic bug in your code; it is `ConditionValue._populate_value()` only
including events whose `callbacks is None` (i.e. *already fully processed* by
the environment's step loop) at the moment the `Condition` itself is
processed — and same-instant events are ordered by *when they were inserted
into the event heap*, not by which one your code intends to "win". A
freshly-built `get_event`'s own succeed() is scheduled *after* the
already-queued timeout in this exact collision, so the timeout's callback
(and the `Condition`'s résolution) runs first, before `get_event`'s value is
visible to `_populate_value()`.

Verified directly against SimPy 4.1's real source (not asserted from theory):

```python
def consumer(env, store, log):
    while True:
        with store.get() as get_event:   # SimPy's own documented idiom
            result = yield get_event | env.timeout(1)
            if get_event in result:
                log.append((env.now, "GOT", result[get_event]))
            else:
                log.append((env.now, "tick"))
                # get_event.cancel() on __exit__ is a no-op here: it IS
                # triggered, just not "in result" -- its value is lost.

def producer(env, store, log):
    yield env.timeout(3)
    yield store.put("A")

env = simpy.Environment()
store = simpy.Store(env)
env.process(consumer(env, store, []))
env.process(producer(env, store, []))
env.run(until=6)
# Real output: "A" is never logged as GOT. It is gone -- store.items is
# empty and get_queue is empty too, so nothing *looks* wrong from the
# outside. Only the item itself is missing.
```

Note what this means about SimPy's own docstring: `Get`/`Put` are documented
as context managers specifically for this reason —
`with resource.get() as request: yield request` is the correct, canonical
idiom for a **single** request that might need to be aborted (an interrupt,
an early return). It is not safe to rebuild fresh per iteration when the same
logical wait is raced against a timeout repeatedly in a loop, because "this
particular iteration's Condition didn't count it" and "the request itself was
lost" are silently the same outcome.

## The fix: one request object, replaced only once it wins

```python
# RIGHT -- the pattern this project's models actually use.
get_event = self._host_q.get()
while True:
    tick_event = self.env.timeout(1)      # safe to recreate every iteration
    result = yield get_event | tick_event
    if get_event in result:
        ...                                 # handle it
        get_event = self._host_q.get()      # only replace it *after* it fired
    else:
        ...                                 # tick won; get_event is still the
                                             # same live, pending request
```

Verified against the exact same same-instant collision as Failure 2 above:
the persistent `get_event` is still visible to the *next* iteration's
freshly-built `Condition`. `Condition.__init__` checks
`if event.callbacks is None: self._check(event)` for every event it is given
— an event that finished processing between iterations resolves the new
`Condition` immediately and synchronously, so nothing is lost even when the
win is detected one iteration "late" within the same simulated instant.

Why the asymmetry (fine to recreate `tick_event`, never fine to recreate
`get_event`) is correct, not arbitrary: a `Timeout` has no shared state to
leak — it belongs to nothing but this one `yield`. A `Store`/`Resource`
request is a registration in *shared, mutable, persistent* state
(`get_queue`, `items`) that outlives the `yield` that created it until
something explicit resolves or cancels it. Recreating the thing with no
shared state costs nothing; recreating the thing that *is* shared state is
where both failure modes above come from.

## Checklist for any `yield event_a | event_b` in a model you write

- If either side is a resource request (`.get()`, `.request()`, a `Store`,
  `Container`, or `Resource` event) inside a loop that might race it more
  than once: keep the **same** request object alive across iterations,
  replacing it only in the branch where it actually won.
- Prefer checking the resource's *actual current state* over trusting only
  `event.triggered` when the two are meant to agree — a persistent-request
  loop and re-checking on the next pass is usually enough; SystemC's
  equivalent hazard and the matching principle is in
  `systemc/README.md`.
- Write a regression test that submits two requests back-to-back (the
  second queued while the first is still being processed) and a test that
  puts the resource's item at the exact instant a competing timeout also
  elapses, if the model's own interface contract allows more than one
  request in flight at a time. If the interface is documented as "one
  request at a time" (check the wait_model), a comment saying so is enough
  — don't manufacture a test for a scenario the contract rules out.
