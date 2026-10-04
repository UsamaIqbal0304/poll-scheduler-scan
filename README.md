# poll-scheduler-scan

What a Niagara poll scheduler does when the bus cannot keep up - the three
rates, the one-point-per-pass loop, and the re-schedule arithmetic that has no
floor under it. Read out of `driver-rt.jar` rather than out of the
documentation.

One Python file, standard library only.

```
./poll-scheduler-scan.py [NIAGARA_HOME]
```

## Why this exists

A poll scheduler is configured with three rates - fast, normal, slow - and the
natural reading is that each bucket's points get polled at that rate. What the
bytecode says is narrower, and it fails quietly.

`pollQueue` polls **exactly one** pollable per pass, walking the bucket
round-robin, then re-schedules the bucket with `next = rate/size - elapsed`,
where `size` is the bucket's point count and `elapsed` is how long that single
poll actually took. **There is no floor under that subtraction.** When a poll
takes longer than its share of the rate, `next` goes to zero or below,
`nextTicks` lands at or before now, and the bucket is due again immediately.
`computeSleep()` then returns zero or less and the sleep is skipped outright.

The poll thread stops idling. It does not log, it does not set a status, and it
does not slow the bus down - the real cycle time simply stretches to
`size * elapsed` instead of the rate that was asked for, and the only visible
symptom is that trend data is older than it should be.

The scan prints, derived from the constants it read, the largest fast bucket
that still leaves a positive gap between polls at a given per-poll cost. On an
RS-485 multidrop, where a request and its reply is tens of milliseconds, those
numbers are smaller than most point counts in service.

## What it reads, and where from

- `javax.baja.driver.util.BPollScheduler` - `fastRate`, `normalRate` and
  `slowRate` with their minimum facets, read out of the static initialiser.
- `BAbstractPollService` - where `pollEnabled` lives, its actions, and what the
  thread does while it is false.
- The run loop disassembled, in order, with the 10-second gate over
  `checkBucketConfig()` and `updateStats()`.
- `pollQueue` - the single `get`, the single `poll()`, the round-robin cursor,
  the due test, and the re-schedule expression, with the absence of any
  `Math.max` stated as a fact about the method.
- `computeSleep()` - its starting value and the minimum it takes.

## Read first: what it does and does not touch

**It never connects to a station.** It unzips `modules/driver-rt.jar` and runs
`javap`. Nothing is installed, patched, written to a station or sent anywhere.

It needs a Niagara installation to read and a `javap` from a JDK 8. It looks
for `javap` in `$JAVAP`, then on `PATH`, then under `$JAVA_HOME` and the
Niagara install, then in Debian's default location. The install to read comes
from the first argument.

**The output below was measured against Niagara 4.15.5.22.** Another version
may differ, and that is the point - run it against yours rather than trusting
this page.

## Running it

```
$ ./poll-scheduler-scan.py
The poll scheduler: what happens when the bus cannot keep up
======================================================================

read from /opt/Niagara/Niagara-4.15.5.22
javap:    1.8.0_504
class:    javax.baja.driver.util.BPollScheduler (driver-rt.jar)

The three rates, read out of the static initialiser, not the docs:
  fastRate      1000 ms   (min facet 1 ms, showMilliseconds)
  normalRate    5000 ms   (min facet 1 ms, showMilliseconds)
  slowRate     30000 ms   (min facet 1 ms, showMilliseconds)
  pollEnabled lives on BAbstractPollService, with actions
  enable and disable. While it is false the thread sleeps
  1000 ms a turn and polls nothing.

Step 1. The run loop, in order, per pass:
          pollDibs()      - drain the on-demand stack, in full
          pollQueues()    - fast, then normal, then slow
          computeSleep()  - and sleep only if it is positive
        Every 10000 ms it also calls checkBucketConfig() and
        updateStats(); both sit behind that one gate (ifle 78).

Step 2. pollQueue polls EXACTLY ONE pollable per pass - one
        ArrayList.get and one poll() call - walking the bucket
        round-robin on its own index cursor. A bucket is due when
        nextTicks <= now + 5 ms.

        It then re-schedules itself, and this is the whole of it:
          next = (rate - size * elapsed) / size   i.e. rate/size
                                                  minus elapsed
          nextTicks = now + next
        where size is the bucket's point count and elapsed is how
        long that single poll actually took. An empty bucket
        re-schedules at 1000 ms.

        There is no Math.max in the method. Nothing floors next.

Step 3. So when a poll takes longer than its share of the rate,
        next falls to zero and then below it, nextTicks lands at
        or before now, and the bucket is due again at once. The
        sleep itself is behind an ifle, so zero is already enough.
        computeSleep() starts at 1000 ms and takes the min over all
        three buckets' nextTicks
        minus now, so it returns that same number or less, and
        the sleep is skipped outright.

        The poll thread stops sleeping. It does not log, it does
        not set a status, and it does not slow the bus down. It
        just stops idling, and the real cycle time stretches to
        size * elapsed instead of the rate that was asked for.

        Derived from the constants above, not asserted - the
        largest fast bucket that still leaves a positive gap
        between polls, so the thread still sleeps at all:
            2 ms per poll ->   333 points, and 334 points gives next = 0 ms
            5 ms per poll ->   166 points, and 167 points gives next = 0 ms
           10 ms per poll ->    90 points, and 91 points gives next = 0 ms
           20 ms per poll ->    47 points, and 48 points gives next = 0 ms
           50 ms per poll ->    19 points, and 20 points gives next = 0 ms
        On an RS-485 multidrop a request and its reply is tens of
        milliseconds, so those are the point counts that matter.

Step 4. The on-demand path starves the periodic one. subscribe()
        pushes the pollable onto a java.util.Stack called dibs AND
        adds it to one of fast/norm/slow by poll frequency
        (tableswitch 0..2, anything else is an
        IllegalStateException). pollDibs() then pops - LIFO - and
        loops back to the top until the stack is empty, before
        pollQueues() gets a single turn. So a burst of
        subscriptions is served newest-first and holds off every
        periodic poll until it is done.

Step 5. checkBucketConfig() re-sorts all three buckets and
        replaces each bucket's queue ArrayList (3 reSort calls, 3
        putfields). It does not reset the bucket's index cursor,
        so after a re-sort the cursor carries over into a
        different list.  [cursor preserved: True]

Step 6. What evidence you get, which is 14 properties, all String:
          averagePoll       busyTime          totalPolls
          dibsPolls         fastPolls         normalPolls
          slowPolls         dibsCount         fastCount
          normalCount       slowCount         fastCycleTime
          normalCycleTime   slowCycleTime
        Every one is flags 3 - READONLY|TRANSIENT - and defaults
        to the string "-". statisticsStart defaults to
        BAbsTime.NULL, and resetStatistics is flags 128
        (CONFIRM_REQUIRED).

        Being Strings, they are already rounded and already
        formatted when you read them:
          cycle times  "average = N ms", integer division, and
                       "-" until the first cycle completes
          counts       plain below 10000, then (n/1000) + "k"
          durations    "Nms" below 10000 ms, then (n/1000) + "sec"
        and they are recomputed only every 10000 ms.

        TRANSIENT means they are not saved, so a station restart
        clears them. String means you cannot put a history
        extension on them, cannot link them to a numeric and
        cannot set an alarm on them. The one number that says your
        bus is saturated is a piece of text, read by eye.

Step 7. Where the errors go: run() catches InterruptedException
        and continues silently, and catches Throwable and calls
        printStackTrace() - 1 site(s). The poll thread never
        dies, and nothing it survives reaches the station log.

Three consequences worth designing for
----------------------------------------------------------------------
  1. Overrun is silent and self-inflicted. The scheduler reacts
     to a slow bus by not sleeping, never by reporting. If your
     driver needs an operator to know the cycle has stretched,
     the driver has to say so itself.
  2. Rate is a budget per bucket, not per point. Doubling the
     points in a bucket halves each point's share, so adding
     points to a working network can push it over with no
     configuration change anywhere.
  3. The statistics cannot be trended. If cycle time matters to
     your customers, expose it as a numeric of your own.

Three checks one station settles in an afternoon
----------------------------------------------------------------------
  1. Put N points on one slow bus at the default 1000 ms fast rate
     and read fastCycleTime: it will say "average = ..." well
     above 1000 once N * elapsed exceeds it.
  2. Watch the poll thread's CPU while that is true - the sleep
     is being skipped every pass.
  3. Open a graphic with many points on it and watch the periodic
     poll stall while the dibs stack drains.
```

## The same finding, written up

The same reading is written up as a page: <https://plantroomlabs.com/tools/poll-scheduler-scan/>. It carries a captured run of this program, the download with its byte count (23,546) and SHA-256 (`f2bf8c99b0933d0c...`) measured off the file the site serves, the Niagara version the bytecode was read on (`4.15.5.22`) beside the version of the JACE this work targets (`4.14.0.162`), and the note on poll rates and tuning policies that explains what a station inherits from the defaults.

## Licence

MIT. Written by Usama Iqbal at [Plantroom Labs](https://plantroomlabs.com).
