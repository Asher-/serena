import contextvars
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
