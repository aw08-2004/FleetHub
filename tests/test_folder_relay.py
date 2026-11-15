"""Unit test for folder_relay.py -- the in-memory window between two PCs in a live folder copy.

The silent failure this file exists to catch is **a copy that reports "done" with bytes
missing**: a chunk dropped, stored twice, or handed out of order, or a receiver allowed to
claim success before the sender finished. Any of those produces a folder on the target that
has every name and most of the bytes, with one file quietly wrong, and nothing in the console
would say so. The second is **a window that grows without bound** -- a sender that is never
told to wait would park a 40 GB profile in the hub's memory.

Driven with a fake clock, so the idle timeout and the long-poll deadlines are asserted
without sleeping. The one place a real wait matters (a sender blocked on a full window being
released by the receiver) uses a thread and a short real timeout.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import folder_relay as fr

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def main():
    print("\n== Opening ==")
    clock = Clock()
    relay = fr.FolderRelay(clock=clock)
    try:
        relay.open("PC-1", "PC-1", ["C:\\a"], "C:\\in", "x")
        check("a copy onto the same PC is refused", False)
    except fr.RelayRefused:
        check("a copy onto the same PC is refused", True)
    tid = relay.open("PC-1", "PC-2", ["C:\\a"], "D:\\in", "op@x.com")
    st = relay.status(tid)
    check("a new transfer is waiting", st["state"] == fr.WAITING)
    check("status names both ends", st["source"] == "PC-1" and st["target"] == "PC-2")
    check("a third machine cannot see it", relay.status(tid, machine="PC-3") is None)
    check("either end can see it", relay.status(tid, machine="PC-2") is not None)

    print("\n== Only the right machine may play each part ==")
    check("the target cannot put chunks",
          relay.put_chunk(tid, "PC-2", 0, b"x", wait=0) == fr.FORBIDDEN)
    check("the source cannot take chunks",
          relay.take_chunk(tid, "PC-1", 0, wait=0)[0] == fr.FORBIDDEN)
    check("a stranger cannot finish the send",
          relay.finish_send(tid, "PC-3") == fr.FORBIDDEN)

    print("\n== Order is enforced, retries are idempotent ==")
    check("chunk 1 before chunk 0 is refused",
          relay.put_chunk(tid, "PC-1", 1, b"b", wait=0) == fr.OUT_OF_ORDER)
    check("chunk 0 is stored", relay.put_chunk(tid, "PC-1", 0, b"aa", wait=0) == fr.STORED)
    check("a retried chunk 0 is a duplicate, not a second copy",
          relay.put_chunk(tid, "PC-1", 0, b"aa", wait=0) == fr.DUPLICATE)
    check("chunk 1 is stored", relay.put_chunk(tid, "PC-1", 1, b"bbb", wait=0) == fr.STORED)
    check("the state moves to copying", relay.status(tid)["state"] == fr.COPYING)
    check("bytes sent count each chunk once", relay.status(tid)["bytes_sent"] == 5)

    print("\n== Taking: asking for N+1 acknowledges N ==")
    state, data = relay.take_chunk(tid, "PC-2", 0, wait=0)
    check("chunk 0 comes out as sent", state == fr.CHUNK and data == b"aa")
    state, data = relay.take_chunk(tid, "PC-2", 0, wait=0)
    check("asking for 0 again (a lost response) still answers", state == fr.CHUNK and data == b"aa")
    state, data = relay.take_chunk(tid, "PC-2", 1, wait=0)
    check("chunk 1 comes out as sent", state == fr.CHUNK and data == b"bbb")
    check("asking for 1 freed chunk 0", relay.status(tid)["bytes_received"] == 2)
    check("going backwards after an ack is refused",
          relay.take_chunk(tid, "PC-2", 0, wait=0)[0] == fr.OUT_OF_ORDER)
    check("nothing yet is pending, not end",
          relay.take_chunk(tid, "PC-2", 2, wait=0)[0] == fr.PENDING)

    print("\n== A receiver cannot claim success early ==")
    early = relay.open("PC-1", "PC-2", ["C:\\b"], "D:\\in", "op")
    relay.put_chunk(early, "PC-1", 0, b"zz", wait=0)
    relay.finish_receive(early, "PC-2", summary="all good")
    check("success before the sender finished is recorded as failed",
          relay.status(early)["state"] == fr.FAILED)

    print("\n== The end ==")
    check("finish_send is accepted", relay.finish_send(tid, "PC-1") == fr.STORED)
    check("a chunk after the end is refused",
          relay.put_chunk(tid, "PC-1", 2, b"late", wait=0) == fr.OUT_OF_ORDER)
    state, _ = relay.take_chunk(tid, "PC-2", 2, wait=0)
    check("after the last chunk the receiver is told end", state == fr.END)
    check("every byte was acknowledged", relay.status(tid)["bytes_received"] == 5)
    relay.finish_receive(tid, "PC-2", summary="Wrote 2 files")
    st = relay.status(tid)
    check("the copy is done", st["state"] == fr.DONE and st["error"] is None)
    check("the receiver's summary is kept", st["summary"] == "Wrote 2 files")
    check("after the end every call is gone",
          relay.take_chunk(tid, "PC-2", 3, wait=0)[0] == fr.GONE)

    print("\n== A failure on either side ends it for both ==")
    t2 = relay.open("PC-1", "PC-2", ["C:\\c"], "D:\\in", "op")
    relay.finish_send(t2, "PC-1", error="disk on fire")
    st = relay.status(t2)
    check("a sender error fails the copy", st["state"] == fr.FAILED)
    check("the error names the machine", "PC-1" in st["error"] and "disk on fire" in st["error"])
    check("the receiver then sees gone", relay.take_chunk(t2, "PC-2", 0, wait=0)[0] == fr.GONE)

    print("\n== Cancel frees the window ==")
    t3 = relay.open("PC-1", "PC-2", ["C:\\d"], "D:\\in", "op")
    relay.put_chunk(t3, "PC-1", 0, b"x" * 1000, wait=0)
    check("cancel answers true", relay.cancel(t3, "stop") is True)
    check("cancelled is its own state", relay.status(t3)["state"] == fr.CANCELLED)
    check("the sender sees gone", relay.put_chunk(t3, "PC-1", 1, b"y", wait=0) == fr.GONE)
    check("a second cancel answers false", relay.cancel(t3) is False)

    print("\n== Backpressure: the window is bounded ==")
    t4 = relay.open("PC-1", "PC-2", ["C:\\e"], "D:\\in", "op")
    big = b"\0" * fr.MAX_CHUNK_BYTES
    seq = 0
    while relay.put_chunk(t4, "PC-1", seq, big, wait=0) == fr.STORED:
        seq += 1
    held = relay.status(t4)["bytes_sent"]
    check("the window stops taking chunks at its byte limit", held <= fr.MAX_BUFFER_BYTES)
    check("a full window answers full, not stored",
          relay.put_chunk(t4, "PC-1", seq, big, wait=0) == fr.FULL)
    try:
        relay.put_chunk(t4, "PC-1", seq, b"\0" * (fr.MAX_CHUNK_BYTES + 1), wait=0)
        check("an oversized chunk is refused", False)
    except ValueError:
        check("an oversized chunk is refused", True)

    # A sender blocked on the full window is released the moment the receiver takes one.
    real = fr.FolderRelay()
    t5 = real.open("PC-1", "PC-2", ["C:\\f"], "D:\\in", "op")
    n = 0
    while real.put_chunk(t5, "PC-1", n, big, wait=0) == fr.STORED:
        n += 1
    outcome = {}

    def blocked_sender():
        outcome["state"] = real.put_chunk(t5, "PC-1", n, big, wait=5)
    worker = threading.Thread(target=blocked_sender)
    worker.start()
    time.sleep(0.2)
    real.take_chunk(t5, "PC-2", 0, wait=0)
    real.take_chunk(t5, "PC-2", 1, wait=0)    # acknowledges chunk 0
    worker.join(5)
    check("a waiting sender is let in when the receiver acknowledges",
          outcome.get("state") == fr.STORED)

    print("\n== Idle transfers time out, finished ones are forgotten ==")
    t6 = relay.open("PC-1", "PC-2", ["C:\\g"], "D:\\in", "op")
    clock.now += fr.IDLE_SECONDS + 1
    st = relay.status(t6)
    check("no progress for IDLE_SECONDS fails the copy", st["state"] == fr.FAILED)
    clock.now += fr.KEEP_FINISHED_SECONDS + 1
    check("a long-finished copy is forgotten", relay.status(t6) is None)

    print("\n== At most MAX_ACTIVE live copies ==")
    capped = fr.FolderRelay(clock=Clock())
    for _ in range(fr.MAX_ACTIVE):
        capped.open("PC-1", "PC-2", ["C:\\h"], "D:\\in", "op")
    try:
        capped.open("PC-1", "PC-2", ["C:\\h"], "D:\\in", "op")
        check("one more than MAX_ACTIVE is refused", False)
    except fr.RelayRefused:
        check("one more than MAX_ACTIVE is refused", True)

    print("\n== Agent version gate ==")
    check("the introducing minor passes", fr.agent_supports_stream("3.51.0"))
    check("a later version passes", fr.agent_supports_stream("3.60.2"))
    check("an older minor fails", not fr.agent_supports_stream("3.50.9"))
    check("unknown fails", not fr.agent_supports_stream(None))
    check("garbage fails", not fr.agent_supports_stream("beta"))

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
