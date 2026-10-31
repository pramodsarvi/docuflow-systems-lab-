"""RabbitMQ work tokens: what KEDA scales on, and what wakes the workers.

Postgres stays the source of truth for every job (state, retries, lease, workload rules). RabbitMQ carries one *token*
per runnable job on a durable queue per lane (`docuflow.io`, `docuflow.vlm`, `docuflow.embed`):

  * enqueue / requeue / retry-dead publish tokens (after the job row is committed)
  * a worker takes a token, then claims the best eligible job in Postgres (priority, fair share, pause, query yield)
  * a failed job's retry token goes to `docuflow.<lane>.delay` with a per-message TTL and dead-letters back to the lane
    queue when the backoff expires
  * KEDA's rabbitmq scaler reads the lane queue length, so replicas follow the backlog
  * `reconcile()` (run by workers under an advisory lock) tops the queue up if tokens were lost, e.g. a publish that
    failed after a commit, or a broker restart without persistence

With RABBITMQ_URL unset everything is a no-op and workers poll Postgres directly (local dev, unit tests)."""
import threading
import time

from . import config, db

_local = threading.local()
LANES = ("io", "vlm", "embed")


def enabled():
    return bool(config.RABBITMQ_URL)


def qname(lane):
    return f"docuflow.{lane}"


def _channel():
    import pika
    ch = getattr(_local, "ch", None)
    if ch is not None and ch.is_open and ch.connection.is_open:
        return ch
    params = pika.URLParameters(config.RABBITMQ_URL)
    params.heartbeat = 600                 # jobs can block the thread for minutes between broker calls
    params.blocked_connection_timeout = 30
    conn = pika.BlockingConnection(params)
    ch = conn.channel()
    ch.confirm_delivery()
    for lane in LANES:
        ch.queue_declare(qname(lane), durable=True)
        ch.queue_declare(qname(lane) + ".delay", durable=True, arguments={
            "x-dead-letter-exchange": "", "x-dead-letter-routing-key": qname(lane)})
    _local.ch = ch
    return ch


def _reset():
    ch = getattr(_local, "ch", None)
    _local.ch = None
    try:
        if ch is not None:
            ch.connection.close()
    except Exception:
        pass


def _with_retry(fn):
    for attempt in range(3):
        try:
            return fn(_channel())
        except Exception:
            _reset()
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))


def notify(lane, n=1, delay_seconds=0):
    """Publish n tokens for a lane (optionally delayed). Never raises: the reconciler repairs anything lost."""
    if not enabled() or n <= 0:
        return 0
    import pika
    props = pika.BasicProperties(delivery_mode=2, expiration=str(int(delay_seconds * 1000)) if delay_seconds > 0 else None)
    target = qname(lane) + (".delay" if delay_seconds > 0 else "")

    def pub(ch):
        for _ in range(n):
            ch.basic_publish("", target, b"1", props)
        return n
    try:
        return _with_retry(pub)
    except Exception as e:
        print(f"[broker] publish to {target} failed ({e}); reconciler will repair", flush=True)
        return 0


def take(lane):
    """Take one token. Returns a delivery tag (ack with `done`, return with `give_back`) or None if the queue is empty."""
    def get(ch):
        method, _, _ = ch.basic_get(qname(lane), auto_ack=False)
        return method.delivery_tag if method else None
    return _with_retry(get)


def done(tag):
    try:
        _channel().basic_ack(tag)
    except Exception:
        _reset()      # the broker requeues unacked tokens when the connection drops: harmless surplus


def give_back(tag):
    try:
        _channel().basic_nack(tag, requeue=True)
    except Exception:
        _reset()


def depth(lane):
    def q(ch):
        return ch.queue_declare(qname(lane), durable=True, passive=True).method.message_count
    return _with_retry(q)


def runnable(lane):
    """Jobs a worker could start now (queued, backoff elapsed, lane and dataset not paused)."""
    with db.pool().connection() as c:
        return c.execute("""SELECT count(*) n FROM jobs j WHERE j.lane=%s AND j.status='queued' AND j.run_after <= now()
                            AND NOT EXISTS (SELECT 1 FROM control c WHERE c.key='paused'
                              AND (c.value->'lanes' @> to_jsonb(%s::text) OR c.value->'datasets' @> to_jsonb(j.dataset)))""",
                         (lane, lane)).fetchone()["n"]


def reconcile(lane):
    """Make the queue hold at least one token per runnable job. Single-flight across workers via an advisory lock."""
    if not enabled():
        return 0
    with db.pool().connection() as c:
        if not c.execute("SELECT pg_try_advisory_xact_lock(hashtext('reconcile:' || %s)) ok", (lane,)).fetchone()["ok"]:
            return 0
        missing = runnable(lane) - depth(lane)
        return notify(lane, missing) if missing > 0 else 0
