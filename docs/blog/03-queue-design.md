# Part 3. The queue: RabbitMQ tokens, Postgres truth

## The starting point

The first version of docuflow used Postgres as the job queue: workers claimed rows with
`SELECT ... FOR UPDATE SKIP LOCKED`, and KEDA's `postgresql` scaler ran a `count(*)` on the jobs table. That works
and is a perfectly good design. The move to RabbitMQ came from two concerns:

- **Polling load.** Every idle worker polls the database twice a second. Fifty idle workers add up to 100 queries per
  second that do nothing.
- **A standard scaling signal.** Queue length on a broker is what KEDA, dashboards and operators expect to see, and
  it's cheap for KEDA to read.

## Why not move the jobs into RabbitMQ entirely

The jobs table holds more than a FIFO can express. At claim time, the app applies workload rules:

- **priority**, with downstream stages first, so a page that's already been read gets embedded before a new page is
  read, and becomes queryable sooner
- **fair share** across datasets: a 5,000-page dataset can't starve a 50-page one
- **pause and resume** a lane or a dataset
- **leases and retries:** a job whose worker died is requeued when its lease expires; a job that keeps failing is
  parked as `dead` for inspection

Moving all of that into broker features (priority queues, per-dataset queues, consumer-side limits) means re-building
it in a place that's harder to query and test. So the design keeps **Postgres as the source of truth** and makes
**RabbitMQ carry work tokens**.

## Tokens

There's one durable queue per lane: `docuflow.io`, `docuflow.vlm`, `docuflow.embed`. Each runnable job is
represented by one message, the *token*, with no payload.

```
enqueue job  ──► INSERT INTO jobs ... COMMIT ──► publish 1 token to docuflow.<lane>
worker loop  ──► basic_get token ──► claim best eligible job in Postgres (rules applied here)
                  ├─ got a job        ─► ack token, run the job (lease + retries tracked in Postgres)
                  ├─ nothing runnable ─► ack token (surplus: duplicate, or the dataset is paused)
                  └─ runnable but held back (lane limit, query back-off) ─► nack + requeue, sleep 1 s
```

The worker side, from `docuflow/worker.py`:

```python
tag = broker.take(lane)                 # basic_get, manual ack
if tag is None:
    time.sleep(0.5); return None
job = jobs.claim(lane, name)            # FOR UPDATE SKIP LOCKED + priority, fair share, pause, limits
if job:
    broker.done(tag)                    # the job row now tracks the work
    return job
if broker.runnable(lane) == 0:
    broker.done(tag)                    # surplus token: drop it
else:
    broker.give_back(tag); time.sleep(1)
```

Three details keep this correct:

1. **Publish after commit.** A token published inside the transaction can reach a worker before the row is
   visible; the worker would then find nothing to claim and drop the token. Callers that enqueue inside a
   transaction call `jobs.notify_after(stage, n)` once it has committed.
2. **Ack early.** The token is acknowledged as soon as the job is claimed, not when the job finishes. A page read can
   take 25 s and an embedding call milliseconds; holding unacked messages across long jobs ties tokens to broker
   connections and heartbeats. Once claimed, the Postgres lease covers the job, and lease expiry republishes a
   token if the worker dies.
3. **Paused work releases pods.** A token for a paused dataset is dropped (nothing is *runnable*), so the queue
   shrinks and KEDA scales the workers down. On resume the reconciler (below) restores the tokens.

## Retries with backoff: delay queues

A failed job goes back to `queued` in Postgres with `run_after = now() + backoff` (5 s, 10 s, 20 s… up to 300 s).
Its token goes to `docuflow.<lane>.delay`, a queue with no consumers whose dead-letter target is the lane queue:

```python
ch.queue_declare("docuflow.vlm.delay", durable=True, arguments={
    "x-dead-letter-exchange": "", "x-dead-letter-routing-key": "docuflow.vlm"})
ch.basic_publish("", "docuflow.vlm.delay", b"1", pika.BasicProperties(delivery_mode=2, expiration="20000"))
```

When the per-message TTL expires, RabbitMQ moves the token to `docuflow.vlm`, exactly when the job becomes runnable
again. KEDA reads only the lane queue, so backing-off jobs don't keep pods alive.

## Reconciliation

Tokens can be lost: a publish can fail after a commit, or the broker can restart without persistence. Every worker
runs a reconciler every 30 s, single-flight across all workers through a Postgres advisory lock:

```python
def reconcile(lane):
    with db.pool().connection() as c:
        if not c.execute("SELECT pg_try_advisory_xact_lock(hashtext('reconcile:' || %s)) ok", (lane,)).fetchone()["ok"]:
            return 0
        missing = runnable(lane) - depth(lane)          # runnable jobs in Postgres minus tokens in the queue
        return notify(lane, missing) if missing > 0 else 0
```

Over-publishing is harmless (surplus tokens are dropped at claim time), so the reconciler only ever adds tokens.

## The scaling signal this produces

For KEDA, the lane queue length is now **"runnable jobs not yet claimed"**. It excludes jobs in backoff, jobs that
are paused, and jobs already running. That's exactly the backlog a new pod could start on. Running jobs are covered
by the second trigger (Part 4). With `RABBITMQ_URL` unset, workers fall back to polling Postgres, which keeps unit
tests and single-machine development simple.

## Tested against a real broker

`tests/test_broker.py` runs against a real RabbitMQ and checks four behaviours:
- one ingest publishes exactly one token per fetch job, after commit
- a worker consumes exactly one token per claimed job
- surplus and paused tokens are dropped, and the reconciler restores them on resume
- a failed job's token reappears on the lane queue only after its backoff
