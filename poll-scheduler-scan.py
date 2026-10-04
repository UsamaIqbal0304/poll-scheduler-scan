#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Usama Iqbal (Plantroom Labs)
"""What the Niagara poll scheduler does when the bus cannot keep up.

Reads javax.baja.driver.util.BPollScheduler and BAbstractPollService out of
driver-rt.jar with javap and reports, from the bytecode rather than the docs:

  - the three rate defaults and their 'min' facet
  - that all fourteen statistics are pre-formatted Strings, readonly and
    transient, defaulting to "-"
  - the run loop: a 10 s statistics deadline, and a sleep that is skipped
    outright when computeSleep() returns zero or less
  - computeSleep(): capped at 1000 ms and free to go negative
  - pollQueue(): one point per pass, re-scheduled at rate/size - elapsed
    with no floor, which is the overrun behaviour
  - subscribe(): a LIFO dibs stack drained in full before any periodic poll

Usage: poll-scheduler-scan.py [NIAGARA_HOME]
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


def _find_javap(niagara_home=None):
    """javap from $JAVAP, then PATH, then the JDK Niagara ships, then Debian's."""
    cand = [os.environ.get("JAVAP"), shutil.which("javap")]
    for base in (os.environ.get("JAVA_HOME"), niagara_home):
        if base:
            cand += [os.path.join(base, "bin", "javap"),
                     os.path.join(base, "jre", "bin", "javap")]
    cand.append("/usr/lib/jvm/java-8-openjdk-amd64/bin/javap")
    for c in cand:
        if c and os.path.exists(c):
            return c
    return None

HOME = sys.argv[1] if len(sys.argv) > 1 else "/opt/Niagara/Niagara-4.15.5.22"
JAVAP = _find_javap(HOME)
PKG = "javax/baja/driver/util"


def abort(why):
    sys.exit("ABORT %s" % why)


if not os.path.isdir(HOME):
    abort("no Niagara install at %s" % HOME)
if not JAVAP:
    abort("no javap found - set $JAVAP or put a JDK 8 javap on PATH")

TMP = tempfile.mkdtemp(prefix="pollscan-")
try:
    jar = os.path.join(HOME, "modules", "driver-rt.jar")
    if not os.path.exists(jar):
        abort("no driver-rt.jar under %s" % HOME)
    with zipfile.ZipFile(jar) as z:
        names = [n for n in z.namelist()
                 if n.startswith(PKG) and n.endswith(".class")]
        if not names:
            abort("driver-rt.jar has no %s classes" % PKG)
        z.extractall(TMP, names)

    def dis(cls):
        p = subprocess.run([JAVAP, "-p", "-c", "-constants",
                            os.path.join(PKG, cls + ".class")],
                           cwd=TMP, capture_output=True, text=True)
        if p.returncode != 0 or "Compiled from" not in p.stdout:
            abort("javap failed on %s: %s" % (cls, p.stderr.strip()[:200]))
        return p.stdout

    SCHED = dis("BPollScheduler")
    SVC = dis("BAbstractPollService")

    def method(text, sig, what):
        """Slice one method body out of a javap dump, by signature."""
        i = text.find("  " + sig + ";\n")
        if i < 0:
            abort("no method %r in %s" % (sig, what))
        j = text.find("\n\n", i)
        return text[i:j if j > 0 else len(text)]

    def one(hay, pat, what):
        m = re.findall(pat, hay)
        if len(m) != 1:
            abort("%d matches for %s (%r)" % (len(m), what, pat))
        return m[0]

    def atleast(hay, pat, n, what):
        m = re.findall(pat, hay)
        if len(m) < n:
            abort("%d matches for %s, wanted at least %d" % (len(m), what, n))
        return m

    # ---- the three rate defaults, out of the static initialiser ----------
    INIT = method(SCHED, "static {}", "BPollScheduler")
    RATES = []
    for prop in ("fastRate", "normalRate", "slowRate"):
        # no intervening putstatic, so one rate's block cannot reach the
        # next rate's property - the three blocks are ~900 chars apart
        blk = one(INIT,
                  r"iconst_0\n\s+\d+: ldc2_w\s+#\d+\s+// long (\d+)l\n"
                  r"(?:(?!putstatic)[\s\S]){0,1200}?"
                  r"putstatic\s+#\d+\s+// Field %s:" % prop,
                  "%s default" % prop)
        RATES.append((prop, int(blk)))
    FAST, NORM, SLOW = (v for _, v in RATES)
    if not (FAST < NORM < SLOW):
        abort("rate defaults are not ascending: %r" % RATES)

    # every rate carries a 'min' facet of 1 ms and showMilliseconds
    MINF = atleast(INIT, r'ldc\s+#\d+\s+// String min\n\s+\d+: lconst_1', 3,
                   "the 'min' facet of 1 ms on each rate")
    atleast(INIT, r"// String showMilliseconds", 3, "showMilliseconds facets")

    # ---- the statistics properties: Strings, flags 3, default "-" --------
    STATS = re.findall(
        r"iconst_3\n\s+\d+: ldc\s+#\d+\s+// String -\n\s+\d+: aconst_null\n"
        r"\s+\d+: invokestatic\s+#\d+\s+// Method newProperty:"
        r"\(ILjava/lang/String;Ljavax/baja/sys/BFacets;\)"
        r"Ljavax/baja/sys/Property;\n\s+\d+: putstatic\s+#\d+\s+// Field "
        r"(\w+):", INIT)
    if len(STATS) != 14:
        abort("expected 14 readonly-transient String statistics, got %d: %r"
              % (len(STATS), STATS))
    # flags 3 is READONLY|TRANSIENT - check that against baja's own Flags
    fj = os.path.join(HOME, "modules", "baja.jar")
    with zipfile.ZipFile(fj) as z:
        z.extract("javax/baja/sys/Flags.class", TMP)
    fp = subprocess.run([JAVAP, "-p", "-constants",
                         "javax/baja/sys/Flags.class"],
                        cwd=TMP, capture_output=True, text=True)
    FL = dict(re.findall(r"int (\w+) = (\d+);", fp.stdout))
    if FL.get("READONLY") != "1" or FL.get("TRANSIENT") != "2":
        abort("Flags.READONLY|TRANSIENT is not 3 in this build: %r" % FL)
    if FL.get("CONFIRM_REQUIRED") != "128":
        abort("Flags.CONFIRM_REQUIRED is not 128 in this build")
    one(INIT, r"sipush\s+128\n\s+\d+: aconst_null\n\s+\d+: invokestatic"
              r"\s+#\d+\s+// Method newAction:[\s\S]{0,200}?"
              r"// Field (resetStatistics):", "the resetStatistics action")

    # ---- the run loop ---------------------------------------------------
    RUN = method(SCHED, "public void run()", "BPollScheduler")
    # armed once before the loop and re-armed after each stats pass, so
    # there are exactly two and they must be the same constant
    DL = re.findall(r"Method javax/baja/sys/Clock\.ticks:\(\)J\n"
                    r"\s+\d+: ldc2_w\s+#\d+\s+// long (\d+)l\n"
                    r"\s+\d+: ladd", RUN)
    if len(DL) != 2 or DL[0] != DL[1]:
        abort("the statistics deadline is not armed twice with one "
              "constant: %r" % DL)
    DEADLINE = int(DL[0])
    # the deadline gates BOTH checkBucketConfig and updateStats
    GATED = one(RUN, r"lcmp\n\s+\d+: ifle\s+(\d+)\n\s+\d+: aload_0\n"
                     r"\s+\d+: invokespecial\s+#\d+\s+// Method "
                     r"checkBucketConfig:\(\)V\n\s+\d+: aload_0\n"
                     r"\s+\d+: invokespecial\s+#\d+\s+// Method "
                     r"updateStats:\(\)V", "the 10 s statistics gate")
    # the sleep is skipped when computeSleep() returns <= 0
    SKIP = one(RUN, r"Method computeSleep:\(\)J\n\s+\d+: lstore_3\n"
                    r"\s+\d+: lload_3\n\s+\d+: lconst_0\n\s+\d+: lcmp\n"
                    r"\s+(\d+): (ifle)\s+(\d+)\n\s+\d+: lload_3\n"
                    r"\s+\d+: invokestatic\s+#\d+\s+// Method "
                    r"java/lang/Thread\.sleep:\(J\)V", "the sleep guard")
    if SKIP[1] != "ifle":
        abort("the sleep guard is %s, not ifle - re-read the branch" % SKIP[1])
    DISABLED = int(one(RUN, r"Method getPollEnabled:\(\)Z\n\s+\d+: ifne\s+\d+\n"
                            r"\s+\d+: ldc2_w\s+#\d+\s+// long (\d+)l\n"
                            r"\s+\d+: invokestatic\s+#\d+\s+// Method "
                            r"java/lang/Thread\.sleep:\(J\)V",
                       "the poll-disabled sleep"))
    # errors: InterruptedException swallowed, Throwable to stderr
    if "printStackTrace" not in RUN:
        abort("run() no longer calls printStackTrace - re-read error handling")
    EXC = re.findall(r"\d+\s+\d+\s+\d+\s+Class (java/lang/\w+)", RUN)
    if "java/lang/InterruptedException" not in EXC or \
       "java/lang/Throwable" not in EXC:
        abort("run()'s exception table lost Interrupted/Throwable: %r" % EXC)
    STACKS = len(re.findall(r"Method java/lang/Throwable\.printStackTrace", RUN))

    # ---- computeSleep: capped at 1000, free to go negative --------------
    CS = method(SCHED, "private long computeSleep()", "BPollScheduler")
    CAP = int(one(CS, r"ldc2_w\s+#\d+\s+// long (\d+)l\n\s+\d+: lstore_3",
                  "the computeSleep ceiling"))
    MINS = re.findall(r"Field javax/baja/driver/util/BPollScheduler\$Bucket"
                      r"\.nextTicks:J\n\s+\d+: lload_1\n\s+\d+: lsub\n"
                      r"\s+\d+: lload_3\n\s+\d+: invokestatic\s+#\d+\s+"
                      r"// Method java/lang/Math\.min:\(JJ\)J", CS)
    if len(MINS) != 3:
        abort("computeSleep does not take the min over three buckets (%d)"
              % len(MINS))
    if re.search(r"Math\.max", CS):
        abort("computeSleep now has a Math.max - the negative sleep is floored")

    # ---- pollQueue: one point per pass, rate/size - elapsed, no floor ----
    PQ = method(SCHED,
                "private void pollQueue(javax.baja.driver.util."
                "BPollScheduler$Bucket)", "BPollScheduler")
    GRACE = int(one(PQ, r"Method javax/baja/sys/Clock\.ticks:\(\)J\n"
                        r"\s+\d+: ldc2_w\s+#\d+\s+// long (\d+)l\n"
                        r"\s+\d+: ladd\n\s+\d+: lcmp\n\s+\d+: ifle",
                    "the due-now grace window"))
    GETS = re.findall(r"Method java/util/ArrayList\.get:\(I\)"
                      r"Ljava/lang/Object;", PQ)
    POLLS = re.findall(r"Method poll:\(Ljavax/baja/driver/util/"
                       r"BIPollable;\)V", PQ)
    if len(GETS) != 1 or len(POLLS) != 1:
        abort("pollQueue is no longer one get and one poll per pass "
              "(%d gets, %d polls)" % (len(GETS), len(POLLS)))
    # the re-schedule arithmetic, read instruction by instruction
    one(PQ, r"Field javax/baja/driver/util/BPollScheduler\$Bucket\.rateProp:"
            r"[\s\S]{0,400}?Method javax/baja/sys/BRelTime\.getMillis:\(\)J",
        "the rate the bucket re-schedules against")
    one(PQ, r"l2d\n\s+\d+: dstore\s+10\n\s+\d+: iload_3\n\s+\d+: i2d\n"
            r"\s+\d+: dstore\s+12\n\s+\d+: dload\s+10\n\s+\d+: dload\s+12\n"
            r"\s+\d+: lload\s+4\n\s+\d+: l2d\n\s+\d+: dmul\n\s+\d+: dsub\n"
            r"\s+\d+: dload\s+12\n\s+\d+: ddiv\n\s+\d+: d2l",
        "the (rate - size*elapsed)/size re-schedule")
    EMPTY = int(one(PQ, r"ifle\s+\d+\n[\s\S]{0,400}?ldc2_w\s+#\d+\s+"
                        r"// long (\d+)l\n\s+\d+: lstore\s+8",
                    "the empty-bucket re-schedule"))
    if re.search(r"Math\.max|Math\.min", PQ):
        abort("pollQueue now clamps its re-schedule - the overrun is floored")

    def next_ticks(rate, size, elapsed):
        """Exactly what the bytecode above computes, in its own order."""
        if size <= 0:
            return EMPTY
        return int((float(rate) - float(size) * float(elapsed)) / float(size))

    # derive the break-even point count rather than assert it
    def breakeven(rate, elapsed):
        """Largest bucket size that still leaves a positive gap.

        Positive is the whole question: the run loop's sleep sits behind an
        ifle, so a next of 0 is skipped exactly like a negative one.
        """
        n = 1
        while n < 100000 and next_ticks(rate, n, elapsed) > 0:
            n += 1
        return n - 1

    if next_ticks(FAST, 1, 0) != FAST:
        abort("a one-point bucket with an instant poll should re-schedule at "
              "the rate, got %d" % next_ticks(FAST, 1, 0))
    # 100 points at 10 ms each exactly exhausts a 1000 ms rate, so next is
    # 0 - and 0 is already enough, because the sleep is behind an ifle
    if next_ticks(FAST, 100, 10) > 0:
        abort("100 points at 10 ms each should exhaust a %d ms rate, "
              "got next = %d" % (FAST, next_ticks(FAST, 100, 10)))

    # ---- subscribe and pollDibs: a LIFO stack, drained first ------------
    SUB = method(SCHED,
                 "public void subscribe(javax.baja.driver.util.BIPollable)",
                 "BPollScheduler")
    one(SUB, r"Field dibs:Ljava/util/Stack;\n\s+\d+: aload_1\n"
             r"\s+\d+: invokevirtual\s+#\d+\s+// Method "
             r"java/util/Stack\.push:", "the dibs push on subscribe")
    SW = one(SUB, r"tableswitch\s+\{ // (\d+) to (\d+)", "the frequency switch")
    if SW != ("0", "2"):
        abort("the poll-frequency switch is not 0..2: %r" % (SW,))
    if "java/lang/IllegalStateException" not in SUB:
        abort("subscribe no longer throws on an unknown poll frequency")
    ADDS = re.findall(r"Field (fast|norm|slow):Ljavax/baja/driver/util/"
                      r"BPollScheduler\$Bucket;", SUB)
    if ADDS != ["fast", "norm", "slow"]:
        abort("subscribe does not fan out to fast/norm/slow in order: %r"
              % ADDS)

    PD = method(SCHED, "private void pollDibs()", "BPollScheduler")
    one(PD, r"Method java/util/Stack\.pop:", "the LIFO dibs pop")
    one(PD, r"Method poll:\(Ljavax/baja/driver/util/BIPollable;\)V\n"
            r"\s+\d+: goto\s+0", "the drain-until-empty loop")
    one(PD, r"Method java/util/Stack\.empty:\(\)Z\n\s+\d+: ifeq", "the empty test")

    # ---- pollQueues: strict fast, normal, slow order --------------------
    PQS = method(SCHED, "private void pollQueues()", "BPollScheduler")
    ORDER = re.findall(r"Field (fast|norm|slow):Ljavax/baja/driver/util/"
                       r"BPollScheduler\$Bucket;", PQS)
    if ORDER != ["fast", "norm", "slow"]:
        abort("pollQueues order is not fast, normal, slow: %r" % ORDER)

    # ---- checkBucketConfig: replaces the queues, keeps the cursor -------
    CBC = method(SCHED, "private void checkBucketConfig()", "BPollScheduler")
    RS = re.findall(r"Method reSort:", CBC)
    if len(RS) != 3:
        abort("checkBucketConfig does not reSort three buckets (%d)" % len(RS))
    PUTQ = re.findall(r"putfield\s+#\d+\s+// Field javax/baja/driver/util/"
                      r"BPollScheduler\$Bucket\.q:", CBC)
    if len(PUTQ) != 3:
        abort("checkBucketConfig does not replace three queues (%d)" % len(PUTQ))
    KEEPS_CURSOR = not re.search(
        r"putfield\s+#\d+\s+// Field javax/baja/driver/util/"
        r"BPollScheduler\$Bucket\.index:", CBC)

    # ---- how the statistics are formatted ------------------------------
    CNT = method(SCHED, "private java.lang.String count(int)", "BPollScheduler")
    KTHRESH = int(one(CNT, r"sipush\s+(\d+)\n\s+\d+: if_icmpge",
                      "the 'k' threshold"))
    KDIV = int(one(CNT, r"sipush\s+(\d+)\n\s+\d+: idiv", "the 'k' divisor"))
    DUR = method(SCHED, "private java.lang.String duration(long)",
                 "BPollScheduler")
    SECT = int(one(DUR, r"ldc2_w\s+#\d+\s+// long (\d+)l\n\s+\d+: lcmp\n"
                        r"\s+\d+: ifge", "the seconds threshold"))
    CYC = method(SCHED,
                 "private java.lang.String toCycle(javax.baja.driver.util."
                 "BPollScheduler$Bucket, long)", "BPollScheduler")
    one(CYC, r"// String (average =)", "the cycle-time prefix")
    one(CYC, r"ldiv", "the integer division in the cycle average")
    one(CYC, r"Field javax/baja/driver/util/BPollScheduler\$Bucket\.cycleTotal:I"
             r"\n\s+\d+: istore\s+4\n\s+\d+: iload\s+4\n\s+\d+: ifne\s+\d+\n"
             r"\s+\d+: ldc\s+#\d+\s+// String -", "the no-cycles-yet dash")

    # ---- pollEnabled lives on the service, not the scheduler -----------
    if "pollEnabled" not in SVC:
        abort("BAbstractPollService no longer declares pollEnabled")
    SVCACT = re.findall(r"public static final javax\.baja\.sys\.Action (\w+);",
                        SVC)
    if sorted(SVCACT) != ["disable", "enable"]:
        abort("BAbstractPollService actions are not enable/disable: %r" % SVCACT)

    # ---- report ---------------------------------------------------------
    W = 70
    jv = subprocess.run([JAVAP, "-version"], capture_output=True,
                        text=True).stdout.strip()
    out = []
    p = out.append
    p("The poll scheduler: what happens when the bus cannot keep up")
    p("=" * W)
    p("")
    p("read from %s" % HOME)
    p("javap:    %s" % jv)
    p("class:    javax.baja.driver.util.BPollScheduler (driver-rt.jar)")
    p("")
    p("The three rates, read out of the static initialiser, not the docs:")
    for prop, v in RATES:
        p("  %-11s %6d ms   (min facet 1 ms, showMilliseconds)" % (prop, v))
    p("  pollEnabled lives on BAbstractPollService, with actions")
    p("  enable and disable. While it is false the thread sleeps")
    p("  %d ms a turn and polls nothing." % DISABLED)
    p("")
    p("Step 1. The run loop, in order, per pass:")
    p("          pollDibs()      - drain the on-demand stack, in full")
    p("          pollQueues()    - fast, then normal, then slow")
    p("          computeSleep()  - and sleep only if it is positive")
    p("        Every %d ms it also calls checkBucketConfig() and" % DEADLINE)
    p("        updateStats(); both sit behind that one gate (ifle %s)." % GATED)
    p("")
    p("Step 2. pollQueue polls EXACTLY ONE pollable per pass - one")
    p("        ArrayList.get and one poll() call - walking the bucket")
    p("        round-robin on its own index cursor. A bucket is due when")
    p("        nextTicks <= now + %d ms." % GRACE)
    p("")
    p("        It then re-schedules itself, and this is the whole of it:")
    p("          next = (rate - size * elapsed) / size   i.e. rate/size")
    p("                                                  minus elapsed")
    p("          nextTicks = now + next")
    p("        where size is the bucket's point count and elapsed is how")
    p("        long that single poll actually took. An empty bucket")
    p("        re-schedules at %d ms." % EMPTY)
    p("")
    p("        There is no Math.max in the method. Nothing floors next.")
    p("")
    p("Step 3. So when a poll takes longer than its share of the rate,")
    p("        next falls to zero and then below it, nextTicks lands at")
    p("        or before now, and the bucket is due again at once. The")
    p("        sleep itself is behind an ifle, so zero is already enough.")
    p("        computeSleep() starts at %d ms and takes the min over all" % CAP)
    p("        three buckets' nextTicks")
    p("        minus now, so it returns that same number or less, and")
    p("        the sleep is skipped outright.")
    p("")
    p("        The poll thread stops sleeping. It does not log, it does")
    p("        not set a status, and it does not slow the bus down. It")
    p("        just stops idling, and the real cycle time stretches to")
    p("        size * elapsed instead of the rate that was asked for.")
    p("")
    p("        Derived from the constants above, not asserted - the")
    p("        largest fast bucket that still leaves a positive gap")
    p("        between polls, so the thread still sleeps at all:")
    for e in (2, 5, 10, 20, 50):
        n = breakeven(FAST, e)
        p("          %3d ms per poll -> %5d points, and %d points gives "
          "next = %d ms" % (e, n, n + 1, next_ticks(FAST, n + 1, e)))
    p("        On an RS-485 multidrop a request and its reply is tens of")
    p("        milliseconds, so those are the point counts that matter.")
    p("")
    p("Step 4. The on-demand path starves the periodic one. subscribe()")
    p("        pushes the pollable onto a java.util.Stack called dibs AND")
    p("        adds it to one of fast/norm/slow by poll frequency")
    p("        (tableswitch 0..2, anything else is an")
    p("        IllegalStateException). pollDibs() then pops - LIFO - and")
    p("        loops back to the top until the stack is empty, before")
    p("        pollQueues() gets a single turn. So a burst of")
    p("        subscriptions is served newest-first and holds off every")
    p("        periodic poll until it is done.")
    p("")
    p("Step 5. checkBucketConfig() re-sorts all three buckets and")
    p("        replaces each bucket's queue ArrayList (3 reSort calls, 3")
    p("        putfields). It does not reset the bucket's index cursor,")
    p("        so after a re-sort the cursor carries over into a")
    p("        different list.  [cursor preserved: %s]" % KEEPS_CURSOR)
    p("")
    p("Step 6. What evidence you get, which is %d properties, all String:"
      % len(STATS))
    for i in range(0, len(STATS), 3):
        p("          " + "  ".join("%-16s" % s for s in STATS[i:i + 3]).rstrip())
    p("        Every one is flags 3 - READONLY|TRANSIENT - and defaults")
    p("        to the string \"-\". statisticsStart defaults to")
    p("        BAbsTime.NULL, and resetStatistics is flags %s"
      % FL["CONFIRM_REQUIRED"])
    p("        (CONFIRM_REQUIRED).")
    p("")
    p("        Being Strings, they are already rounded and already")
    p("        formatted when you read them:")
    p("          cycle times  \"average = N ms\", integer division, and")
    p("                       \"-\" until the first cycle completes")
    p("          counts       plain below %d, then (n/%d) + \"k\"" % (KTHRESH, KDIV))
    p("          durations    \"Nms\" below %d ms, then (n/1000) + \"sec\""
      % SECT)
    p("        and they are recomputed only every %d ms." % DEADLINE)
    p("")
    p("        TRANSIENT means they are not saved, so a station restart")
    p("        clears them. String means you cannot put a history")
    p("        extension on them, cannot link them to a numeric and")
    p("        cannot set an alarm on them. The one number that says your")
    p("        bus is saturated is a piece of text, read by eye.")
    p("")
    p("Step 7. Where the errors go: run() catches InterruptedException")
    p("        and continues silently, and catches Throwable and calls")
    p("        printStackTrace() - %d site(s). The poll thread never" % STACKS)
    p("        dies, and nothing it survives reaches the station log.")
    p("")
    p("Three consequences worth designing for")
    p("-" * W)
    p("  1. Overrun is silent and self-inflicted. The scheduler reacts")
    p("     to a slow bus by not sleeping, never by reporting. If your")
    p("     driver needs an operator to know the cycle has stretched,")
    p("     the driver has to say so itself.")
    p("  2. Rate is a budget per bucket, not per point. Doubling the")
    p("     points in a bucket halves each point's share, so adding")
    p("     points to a working network can push it over with no")
    p("     configuration change anywhere.")
    p("  3. The statistics cannot be trended. If cycle time matters to")
    p("     your customers, expose it as a numeric of your own.")
    p("")
    p("Three checks one station settles in an afternoon")
    p("-" * W)
    p("  1. Put N points on one slow bus at the default %d ms fast rate" % FAST)
    p("     and read fastCycleTime: it will say \"average = ...\" well")
    p("     above %d once N * elapsed exceeds it." % FAST)
    p("  2. Watch the poll thread's CPU while that is true - the sleep")
    p("     is being skipped every pass.")
    p("  3. Open a graphic with many points on it and watch the periodic")
    p("     poll stall while the dibs stack drains.")
    p("")
    sys.stdout.write("\n".join(out) + "\n")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
