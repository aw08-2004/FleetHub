"""Live folder copy from one PC to another, relayed through the hub (Files tab, roadmap #13).

**Why this exists.** The Files tab could already pull a folder off a machine as a zip and push
one file onto a machine, but each of those parks the whole payload in the hub's spool, which
is why both stop at files.MAX_TRANSFER_BYTES (2 GiB) and why neither can hand a folder to a
SECOND machine. "Copy these three folders from the old PC to the new one" is a profile
migration, and profile folders are routinely tens of gigabytes. So this path never holds the
payload: the source agent POSTs a stream in numbered chunks, the target agent long-polls them
back out as they arrive, and the hub keeps only a small window of chunks in between.

**What the silent failure would be, and what stops it.** A relay that loses or reorders one
chunk produces a folder that LOOKS copied -- the right names, most of the bytes -- with one
file quietly corrupt. Three things close that:

  * **Sequence numbers, enforced here.** A chunk is accepted only as the next one expected,
    a retried chunk the hub already has is acknowledged as a duplicate rather than stored
    twice, and the receiver asks for chunks by number. Nothing is ever inferred from order of
    arrival.
  * **A chunk leaves the window only when the receiver asks for the NEXT one.** Asking for
    N+1 is the receiver's acknowledgement of N, so a response lost on the way to the target
    is simply asked for again and is still there.
  * **The end is explicit, on both layers.** The sender closes the stream with `finish_send`;
    a stream that stops without it never reads as finished. The agents' own framing carries a
    final record as well, so a receiver that saw the hub say "end" without that record knows
    the stream was cut (agent/.../Files/FolderStream.cs).

**Backpressure, not buffering.** The window is bounded by bytes (MAX_BUFFER_BYTES). A sender
that gets ahead of its receiver waits on `put_chunk` for room, up to WAIT_SECONDS, and is then
told `full` and retries. So the hub's memory per transfer is the window, whatever the folder's
size, and the copy runs at the speed of the slower of the two links.

**State is in memory, deliberately, like remote_relay.py.** A transfer is a few megabytes of
chunks in flight; writing them through the single `db_writer` would put a disk write per chunk
on the path every sensor report also uses. It relies on the hub being one process, which it is.
A hub restart mid-copy fails the transfer, visibly: both agents get `gone` and say so, and the
console's status poll gets a 404 it renders as "the hub lost track of this copy".

**Copy only -- nothing is deleted on the source.** A move would be a copy plus a delete, and a
delete that runs after a copy the target only *thinks* it finished is the one outcome here that
cannot be undone. The owner asked for copy only (2026-11-14).

Kept free of Flask so it can be unit-tested in isolation; folder_relay_web.py wires the routes
and owns every authorization decision -- nothing here knows who is asking, only which MACHINE
is, and each call checks that machine is the end of the transfer it claims to be.

Rejected alternatives:
  * **Spool the whole stream on the hub, then hand it over.** Needs the folder's full size free
    on the hub's disk, twice the wall-clock time, and reintroduces exactly the cap this exists
    to remove.
  * **Agent to agent directly.** Agents only ever dial out to the hub; two PCs on different
    sites, behind different NATs, have no address for each other. The hub is the one party both
    can already reach over HTTPS.
  * **One long streaming response to the receiver.** The hub sits behind a TLS terminator that
    may buffer a response it has not seen the end of -- remote_relay.py found the same -- and a
    buffered stream is a copy that hangs with no error anywhere. A long-poll per chunk cannot.
  * **Zip on the source, unzip on the target.** A zip's directory is at its END, so the target
    could not extract anything until the last byte arrived; the agents' framing is per file and
    each file is in place the moment its bytes are.
"""
import threading
import time
import uuid

#: Largest single chunk an agent may POST. The agents send 1 MiB; this is the ceiling, so a
#: misbehaving client cannot use one request to fill the window.
MAX_CHUNK_BYTES = 4 * 1024 * 1024

#: Bytes held between sender and receiver, per transfer. Large enough that a sender on a fast
#: LAN is not stalled by one slow long-poll round trip, small enough that eight concurrent
#: copies cost the hub well under a quarter of a gigabyte.
MAX_BUFFER_BYTES = 24 * 1024 * 1024

#: Concurrent live transfers. Bounds the memory above; a ninth copy is refused with a sentence
#: rather than quietly slowing all eight.
MAX_ACTIVE = 8

#: How long one put_chunk / take_chunk call may wait for room or for data before answering.
#: Under the agents' 60-second HTTP timeout with room to spare.
WAIT_SECONDS = 15

#: A transfer with no progress -- no chunk stored and none taken -- for this long is failed.
#: Long enough for an offline target to come back from a reboot, short enough that a PC that is
#: gone for the day does not hold a slot until somebody notices.
IDLE_SECONDS = 15 * 60

#: How long a FINISHED transfer is kept, so the console can still show what happened.
KEEP_FINISHED_SECONDS = 60 * 60

# Transfer states, as the console sees them.
WAITING = "waiting"      # opened, no bytes yet
COPYING = "copying"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
ENDED = frozenset({DONE, FAILED, CANCELLED})

#: The agent minor that introduced stream_send / stream_receive. **Both ends must have it**:
#: an older target fails its command as an unknown type, and the sender would then fill the
#: window and wait out IDLE_SECONDS before anybody was told. Mirrored as MIN_STREAM_AGENT in
#: static/js/files.js, which greys the menu item out; this copy is the one that refuses.
MIN_AGENT_VERSION = (3, 51, 0)


def agent_supports_stream(version):
    """True when an agent version string is at least MIN_AGENT_VERSION. Unknown is False:
    a machine that never reported a version is not one to start a fifteen-minute wait on."""
    import re
    match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", str(version or ""))
    if not match:
        return False
    return tuple(int(p) for p in match.groups()) >= MIN_AGENT_VERSION

# Answers from put_chunk / take_chunk. Strings rather than exceptions: every one of these is
# an expected state the web layer maps to a status code, not an error.
STORED = "stored"
DUPLICATE = "duplicate"
FULL = "full"
OUT_OF_ORDER = "out_of_order"
CHUNK = "chunk"
PENDING = "pending"
END = "end"
GONE = "gone"
FORBIDDEN = "forbidden"


class RelayRefused(ValueError):
    """A transfer could not be opened. The message is a sentence for the operator."""


class _Transfer:
    __slots__ = ("id", "source", "target", "paths", "destination", "issued_by",
                 "created", "touched", "ended_at", "chunks", "buffered", "next_in",
                 "next_out", "send_done", "state", "error", "bytes_in", "bytes_out",
                 "total_bytes", "total_files", "summary", "send_command", "receive_command")

    def __init__(self, transfer_id, source, target, paths, destination, issued_by, now):
        self.id = transfer_id
        self.source = source
        self.target = target
        self.paths = list(paths)
        self.destination = destination
        self.issued_by = issued_by
        self.created = now
        self.touched = now
        self.ended_at = None
        self.chunks = {}          # seq -> bytes, only the unacknowledged window
        self.buffered = 0
        self.next_in = 0          # the seq the sender must send next
        self.next_out = 0         # the lowest seq the receiver has not acknowledged
        self.send_done = False
        self.state = WAITING
        self.error = None
        self.bytes_in = 0
        self.bytes_out = 0
        self.total_bytes = None
        self.total_files = None
        self.summary = None
        self.send_command = None
        self.receive_command = None


class FolderRelay:
    """Every live folder transfer this hub is carrying. One instance per hub process."""

    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._transfers = {}

    # ---------------------------------------------------------------- console side
    def open(self, source, target, paths, destination, issued_by):
        """Start a transfer and answer with its id. Refuses past MAX_ACTIVE."""
        if source == target:
            raise RelayRefused("Pick a different computer to send to.")
        with self._lock:
            self._prune_locked()
            active = sum(1 for t in self._transfers.values() if t.state not in ENDED)
            if active >= MAX_ACTIVE:
                raise RelayRefused(
                    "The hub is already carrying as many live copies as it allows. "
                    "Try again when one finishes.")
            transfer_id = uuid.uuid4().hex
            self._transfers[transfer_id] = _Transfer(
                transfer_id, source, target, paths, destination, issued_by, self._clock())
            return transfer_id

    def attach_commands(self, transfer_id, send_command, receive_command):
        """Remember the two fleet commands, so the status can point at their output."""
        with self._lock:
            t = self._transfers.get(transfer_id)
            if t is not None:
                t.send_command = send_command
                t.receive_command = receive_command

    def status(self, transfer_id, machine=None):
        """What the console polls. None for an unknown id, or for a `machine` that is neither
        end of the transfer -- the same answer for both, so one PC's scope cannot be used to
        learn about a copy between two others."""
        with self._lock:
            self._prune_locked()
            t = self._transfers.get(transfer_id)
            if t is None or (machine is not None and machine not in (t.source, t.target)):
                return None
            return {
                "id": t.id, "source": t.source, "target": t.target,
                "paths": list(t.paths), "destination": t.destination,
                "issued_by": t.issued_by, "state": t.state, "error": t.error,
                "bytes_sent": t.bytes_in, "bytes_received": t.bytes_out,
                "total_bytes": t.total_bytes, "total_files": t.total_files,
                "summary": t.summary,
                "send_command": t.send_command, "receive_command": t.receive_command,
            }

    def cancel(self, transfer_id, reason="Cancelled from the console."):
        """Stop a transfer. Both agents see `gone` on their next call and stop."""
        with self._lock:
            t = self._transfers.get(transfer_id)
            if t is None or t.state in ENDED:
                return False
            self._end_locked(t, CANCELLED, reason)
            return True

    # ---------------------------------------------------------------- sender side
    def describe(self, transfer_id, machine, total_bytes, total_files):
        """The sender's up-front measurement, for a progress bar. Optional: a transfer that
        never sends one still copies, it just shows bytes without a percentage."""
        with self._lock:
            t = self._transfers.get(transfer_id)
            if t is None or t.state in ENDED:
                return GONE
            if machine != t.source:
                return FORBIDDEN
            t.total_bytes = max(0, int(total_bytes or 0))
            t.total_files = max(0, int(total_files or 0))
            return STORED

    def put_chunk(self, transfer_id, machine, seq, data, wait=WAIT_SECONDS):
        """Store chunk `seq` from the source. Waits up to `wait` seconds for room."""
        if len(data) > MAX_CHUNK_BYTES:
            raise ValueError("chunk is larger than the relay accepts")
        deadline = self._clock() + max(0.0, wait)
        with self._lock:
            while True:
                t = self._transfers.get(transfer_id)
                if t is None or t.state in ENDED:
                    return GONE
                if machine != t.source:
                    return FORBIDDEN
                if t.send_done:
                    # A chunk after the sender's own end is a broken client, and storing it
                    # would hand the receiver bytes past a point it was told was final.
                    return OUT_OF_ORDER
                if seq < t.next_in:
                    return DUPLICATE
                if seq > t.next_in:
                    return OUT_OF_ORDER
                # An empty window always takes one chunk, so a chunk bigger than the spare
                # room cannot wedge the transfer forever.
                if t.buffered == 0 or t.buffered + len(data) <= MAX_BUFFER_BYTES:
                    t.chunks[seq] = bytes(data)
                    t.buffered += len(data)
                    t.next_in += 1
                    t.bytes_in += len(data)
                    t.touched = self._clock()
                    if t.state == WAITING:
                        t.state = COPYING
                    self._changed.notify_all()
                    return STORED
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return FULL
                self._changed.wait(remaining)

    def finish_send(self, transfer_id, machine, error=None):
        """The source has sent everything (or has failed, with `error`)."""
        with self._lock:
            t = self._transfers.get(transfer_id)
            if t is None or t.state in ENDED:
                return GONE
            if machine != t.source:
                return FORBIDDEN
            if error:
                self._end_locked(t, FAILED, f"{t.source}: {error}")
                return STORED
            t.send_done = True
            t.touched = self._clock()
            self._changed.notify_all()
            return STORED

    # ---------------------------------------------------------------- receiver side
    def take_chunk(self, transfer_id, machine, seq, wait=WAIT_SECONDS):
        """Hand chunk `seq` to the target, waiting up to `wait` seconds for it to arrive.

        Answers (state, bytes). Asking for `seq` acknowledges everything before it, which is
        what frees the window. END means the sender finished and every chunk has been taken.
        """
        deadline = self._clock() + max(0.0, wait)
        with self._lock:
            while True:
                t = self._transfers.get(transfer_id)
                if t is None or t.state in ENDED:
                    return GONE, b""
                if machine != t.target:
                    return FORBIDDEN, b""
                if seq < t.next_out:
                    # Already acknowledged and dropped: the receiver went backwards, which
                    # only a broken client does. Not silently answered with other bytes.
                    return OUT_OF_ORDER, b""
                self._ack_locked(t, seq)
                if seq in t.chunks:
                    return CHUNK, t.chunks[seq]
                if t.send_done and seq >= t.next_in:
                    return END, b""
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return PENDING, b""
                self._changed.wait(remaining)

    def finish_receive(self, transfer_id, machine, error=None, summary=None):
        """The target's verdict. Success is only accepted once the sender has finished and
        every chunk has been acknowledged -- a receiver claiming success early is wrong, and
        the copy is reported as failed rather than as done."""
        with self._lock:
            t = self._transfers.get(transfer_id)
            if t is None or t.state in ENDED:
                return GONE
            if machine != t.target:
                return FORBIDDEN
            if summary:
                t.summary = str(summary)[:2000]
            if error:
                self._end_locked(t, FAILED, f"{t.target}: {error}")
            elif not t.send_done or t.next_out < t.next_in:
                self._end_locked(t, FAILED,
                                 f"{t.target} stopped before the whole folder had arrived.")
            else:
                self._end_locked(t, DONE, None)
            return STORED

    # ---------------------------------------------------------------- internals
    def _ack_locked(self, t, seq):
        for old in [s for s in t.chunks if s < seq]:
            data = t.chunks.pop(old)
            t.buffered -= len(data)
            t.bytes_out += len(data)
        if seq > t.next_out:
            t.next_out = seq
            t.touched = self._clock()
            self._changed.notify_all()

    def _end_locked(self, t, state, error):
        t.state = state
        t.error = error
        t.ended_at = self._clock()
        # The window goes the moment the copy is over, finished or not: the bytes are the
        # one thing here that is large.
        t.chunks.clear()
        t.buffered = 0
        self._changed.notify_all()

    def _prune_locked(self):
        now = self._clock()
        for transfer_id, t in list(self._transfers.items()):
            if t.state not in ENDED and now - t.touched > IDLE_SECONDS:
                self._end_locked(t, FAILED, "Nothing moved for 15 minutes -- one of the two "
                                            "computers is offline or stopped answering.")
            if t.state in ENDED and now - t.ended_at > KEEP_FINISHED_SECONDS:
                del self._transfers[transfer_id]

    def prune(self):
        """Fail idle transfers and forget old finished ones. Called on every open/status;
        exposed so a test (or a timer, if one is ever wanted) can drive it."""
        with self._lock:
            self._prune_locked()
