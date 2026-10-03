import concurrent.futures
import contextvars
import threading
import time

import pytest

from serena.task_executor import TaskExecutor


@pytest.fixture
def executor():
    """
    Fixture for a basic SerenaAgent without a project
    """
    return TaskExecutor("TestExecutor")


class Task:
    def __init__(self, delay: float, exception: bool = False):
        self.delay = delay
        self.exception = exception
        self.did_run = False

    def run(self):
        self.did_run = True
        time.sleep(self.delay)
        if self.exception:
            raise ValueError("Task failed")
        return True


def test_task_executor_sequence(executor):
    """
    Tests that a sequence of tasks is executed correctly
    """
    future1 = executor.issue_task(Task(1).run, name="task1")
    future2 = executor.issue_task(Task(1).run, name="task2")
    assert future1.result() is True
    assert future2.result() is True


def test_task_executor_exception(executor):
    """
    Tests that tasks that raise exceptions are handled correctly, i.e. that
      * the exception is propagated,
      * subsequent tasks are still executed.
    """
    future1 = executor.issue_task(Task(1, exception=True).run, name="task1")
    future2 = executor.issue_task(Task(1).run, name="task2")
    have_exception = False
    try:
        assert future1.result()
    except Exception as e:
        assert isinstance(e, ValueError)
        have_exception = True
    assert have_exception
    assert future2.result() is True


def test_task_executor_cancel_current(executor):
    """
    Tests that tasks that are cancelled are handled correctly, i.e. that
      * subsequent tasks are executed as soon as cancellation ensues.
      * the cancelled task raises CancelledError when result() is called.
    """
    start_time = time.time()
    future1 = executor.issue_task(Task(10).run, name="task1")
    future2 = executor.issue_task(Task(1).run, name="task2")
    time.sleep(1)
    future1.cancel()
    assert future2.result() is True
    end_time = time.time()
    assert (end_time - start_time) < 9, "Cancelled task did not stop in time"
    have_cancelled_error = False
    try:
        future1.result()
    except Exception as e:
        assert e.__class__.__name__ == "CancelledError"
        have_cancelled_error = True
    assert have_cancelled_error


def test_task_executor_cancel_future(executor):
    """
    Tests that when a future task is cancelled, it is never run at all
    """
    task1 = Task(10)
    task2 = Task(1)
    future1 = executor.issue_task(task1.run, name="task1")
    future2 = executor.issue_task(task2.run, name="task2")
    time.sleep(1)
    future2.cancel()
    future1.cancel()
    try:
        future2.result()
    except:
        pass
    assert task1.did_run
    assert not task2.did_run


def test_task_executor_propagates_contextvars_from_issuer(executor):
    """
    Regression: a task scheduled via :meth:`TaskExecutor.issue_task` must observe the
    :mod:`contextvars` bindings that were active in the issuing thread at the time the task was
    issued. ``threading.Thread`` does not inherit ContextVars, so without an explicit context
    snapshot the task body would see the module-level defaults instead of the issuer's bindings.

    The motivating case is :meth:`SerenaAgent._activate_project`: it runs inside a
    ``Tool.apply_ex`` task closure that has bound ``_SESSION_KEY_VAR`` and ``_ACTIVE_PROJECT_VAR``
    for the active MCP session, then schedules ``init_language_server_manager`` via
    ``issue_task``. If the second task does not inherit those bindings, every reader of the active
    project inside it falls through to the legacy single slot and ``get_active_project_or_raise``
    raises ``No active project``, leaving the language-server manager unbuilt and every subsequent
    tool call failing with "language server manager could not be constructed at all".
    """
    var: contextvars.ContextVar[str] = contextvars.ContextVar("test_propagation", default="default")
    observed: list[str] = []

    def task_body() -> str:
        observed.append(var.get())
        return var.get()

    # bind the contextvar in the issuing thread, then schedule the task; the task body must see
    # the bound value, not the default
    var.set("issuer-bound")
    future = executor.issue_task(task_body, name="ctxvar-propagation")

    assert future.result(timeout=5) == "issuer-bound"
    assert observed == ["issuer-bound"]


def test_task_executor_isolates_contextvar_mutations_per_task(executor):
    """
    Two tasks issued under different ContextVar bindings must observe their own bindings, not each
    other's. Each ``Task`` snapshots its issuing context at construction time and runs inside that
    snapshot, so subsequent mutations in the issuing thread (or in sibling tasks) cannot bleed in.
    """
    var: contextvars.ContextVar[str] = contextvars.ContextVar("test_isolation", default="default")

    def task_body() -> str:
        return var.get()

    # task A is issued under the binding "alpha"
    var.set("alpha")
    future_a = executor.issue_task(task_body, name="ctxvar-iso-a")
    # mutate the caller's context; task B is issued under "beta"
    var.set("beta")
    future_b = executor.issue_task(task_body, name="ctxvar-iso-b")
    # mutate again after both tasks are queued; neither task may observe this value
    var.set("gamma")

    assert future_a.result(timeout=5) == "alpha"
    assert future_b.result(timeout=5) == "beta"


def test_task_executor_cancellation_via_task_info(executor):
    start_time = time.time()
    executor.issue_task(Task(10).run, "task1")
    executor.issue_task(Task(10).run, "task2")
    task_infos = executor.get_current_tasks()
    task_infos2 = executor.get_current_tasks()

    # test expected tasks
    assert len(task_infos) == 2
    assert "task1" in task_infos[0].name
    assert "task2" in task_infos[1].name

    # test task identifiers being stable
    assert task_infos2[0].task_id == task_infos[0].task_id

    # test cancellation
    task_infos[0].cancel()
    time.sleep(0.5)
    task_infos3 = executor.get_current_tasks()
    assert len(task_infos3) == 1  # Cancelled task is gone from the queue
    task_infos3[0].cancel()
    try:
        task_infos3[0].future.result()
    except:
        pass
    end_time = time.time()
    assert (end_time - start_time) < 9, "Cancelled task did not stop in time"


def test_keepalive_idle_worker_relinquishes_and_revives() -> None:
    """A per-session executor's worker exits after its idle window; the next task revives it."""
    ex = TaskExecutor("KeepaliveIdle", idle_keepalive_seconds=0.2)
    assert ex._worker_alive is True
    # after the idle window with no work, the worker relinquishes its thread
    time.sleep(0.6)
    assert ex._worker_alive is False
    assert ex._task_executor_thread is not None and not ex._task_executor_thread.is_alive()
    # the next task revives a worker and runs to completion
    assert ex.execute_task(lambda: 42, name="after-idle") == 42
    assert ex._worker_alive is True
    ex.shutdown()


def test_keepalive_preserves_serial_ordering() -> None:
    """With a worker kept alive, tasks issued together run in FIFO order on the single worker."""
    ex = TaskExecutor("KeepaliveOrder", idle_keepalive_seconds=5.0)
    order: list[int] = []
    tasks = [ex.issue_task((lambda i=i: order.append(i)), name=f"t{i}") for i in range(10)]
    for t in tasks:
        t.result()
    assert order == list(range(10))
    ex.shutdown()


def test_keepalive_ordering_preserved_across_revival() -> None:
    """After an idle relinquish-and-revive cycle, subsequently issued tasks still run in order."""
    ex = TaskExecutor("KeepaliveRevive", idle_keepalive_seconds=0.2)
    assert ex.execute_task(lambda: "a", name="a") == "a"
    time.sleep(0.6)
    assert ex._worker_alive is False
    order: list[int] = []
    tasks = [ex.issue_task((lambda i=i: order.append(i)), name=f"r{i}") for i in range(5)]
    for t in tasks:
        t.result()
    assert order == [0, 1, 2, 3, 4]
    ex.shutdown()


class _TaskThreadDied(BaseException):
    """A BaseException that is not an Exception -- the class pyo3's PanicException belongs to."""


def test_task_executor_base_exception_completes_future_and_next_task_runs(executor) -> None:
    """
    A task whose function raises a BaseException that is not an Exception completes its future with
    that exception, and the task queued behind it then runs to completion.
    Ordering is fixed by gates, never by the clock: the dying task holds on ``release`` until the second
    task is queued behind it, and its thread is joined before its future is read, so a future the dead
    thread never completed is observed as not done rather than waited on.
    """
    died = _TaskThreadDied("task thread died")
    assert not isinstance(died, Exception)
    started = threading.Event()
    release = threading.Event()
    second_ran = threading.Event()
    dying_threads: list[threading.Thread] = []

    def dying_task() -> None:
        dying_threads.append(threading.current_thread())
        started.set()
        release.wait()
        raise died

    def second_task() -> str:
        second_ran.set()
        return "second"

    first = executor.issue_task(dying_task, name="dying")
    started.wait()
    second = executor.issue_task(second_task, name="second")
    # the executor is occupied by the in-flight dying task, so the second task is queued, not run
    assert not second_ran.is_set()
    release.set()
    dying_threads[0].join()

    assert first.is_done(), "the dying task's thread has exited without completing its future"
    assert first.future.exception() is died
    with pytest.raises(_TaskThreadDied):
        first.result()
    assert second.result() == "second"
    assert second_ran.is_set()


@pytest.mark.parametrize("outcome", ["returns", "raises"])
def test_task_executor_cancelled_running_task_stays_cancelled_and_next_task_runs(executor, monkeypatch, outcome: str) -> None:
    """
    A task cancelled while its function runs stays cancelled whether the function then returns or raises a
    BaseException that is not an Exception: the result or exception is discarded, nothing escapes the task's
    thread unhandled, and the task queued behind it runs to completion.
    Ordering is fixed by gates, never by the clock: the task is cancelled while its function holds on
    ``release``, and its thread is joined before anything is read.
    """
    escaped: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_value))
    started = threading.Event()
    release = threading.Event()
    running_threads: list[threading.Thread] = []

    def cancelled_task() -> str:
        running_threads.append(threading.current_thread())
        started.set()
        release.wait()
        if outcome == "raises":
            raise _TaskThreadDied("raised after the task was cancelled")
        return "discarded"

    first = executor.issue_task(cancelled_task, name="cancelled-while-running")
    started.wait()
    second = executor.issue_task(lambda: "second", name="second")
    first.cancel()
    release.set()
    running_threads[0].join()

    assert first.future.cancelled()
    assert escaped == [], "the task's function raised out of its thread unhandled"
    with pytest.raises(concurrent.futures.CancelledError):
        first.result()
    assert second.result() == "second"


def test_queue_worker_goes_on_after_a_future_completed_with_a_base_exception(executor, monkeypatch) -> None:
    """
    The queue worker survives a task whose future was completed with a BaseException that is not an Exception:
    nothing escapes the worker's thread, and the worker starts the task queued behind it.
    The first task's start is replaced so that it completes that task's future with the exception on the worker's
    own thread without running the task's function, which isolates the worker's wait from run_task.
    Ordering is fixed by gates, never by the clock: the test waits on an event that the second task's function sets
    and that an exception escaping any thread also sets, so a worker that died is observed rather than waited on.
    """
    died = _TaskThreadDied("future completed with a BaseException")
    escaped: list[BaseException | None] = []
    outcome = threading.Event()

    def record_escape(args: threading.ExceptHookArgs) -> None:
        escaped.append(args.exc_value)
        outcome.set()

    monkeypatch.setattr(threading, "excepthook", record_escape)
    real_start = TaskExecutor.Task.start

    def start(task: TaskExecutor.Task) -> None:
        if task.name.endswith(":dying"):
            task.future.set_exception(died)
        else:
            real_start(task)

    monkeypatch.setattr(TaskExecutor.Task, "start", start)

    def second_task() -> str:
        outcome.set()
        return "second"

    first = executor.issue_task(lambda: None, name="dying")
    second = executor.issue_task(second_task, name="second")
    outcome.wait()

    assert escaped == [], "the BaseException escaped the queue worker's thread"
    assert first.future.exception() is died
    assert second.result() == "second"


def test_queue_worker_runs_an_unlogged_task_without_recording_it(executor) -> None:
    """
    A task issued with ``logged=False`` is started by the queue worker like any other, and is not recorded as the
    last executed task: the record still names the logged task that ran before it.
    Ordering is fixed by the queue, never by the clock: the record is read by a third task, which the worker starts
    only after it has finished with the unlogged task.
    """
    assert executor.issue_task(lambda: "logged", name="logged").result() == "logged"
    assert executor.issue_task(lambda: "unlogged", name="unlogged", logged=False).result() == "unlogged"
    recorded = executor.issue_task(executor.get_last_executed_task, name="reader").result()

    assert recorded is not None
    assert recorded.name.endswith(":logged")
    assert recorded.logged


def test_queue_worker_exits_once_the_executor_is_shut_down() -> None:
    """
    Shutting the executor down ends its queue worker's loop: the worker, idle inside a keepalive window that cannot
    elapse during the test, exits once shutdown is requested.
    Ordering is fixed by joining the worker's thread, never by the clock.
    """
    ex = TaskExecutor("ShutdownExit", idle_keepalive_seconds=3600)
    worker = ex._task_executor_thread
    assert worker is not None and worker.is_alive()
    ex.shutdown()
    worker.join()
    assert not worker.is_alive()
