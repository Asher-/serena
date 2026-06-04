import concurrent.futures
import contextvars
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Thread
from typing import Generic, TypeVar

from sensai.util import logging
from sensai.util.logging import LogTime
from sensai.util.string import ToStringMixin

log = logging.getLogger(__name__)
T = TypeVar("T")


class TaskExecutor:
    # idle window after which a per-session worker thread relinquishes itself; the next
    # issue_task revives it. This bounds a per-session worker to its active life instead of
    # the daemon's lifetime, without any separate sweeper. Overridable per instance for tests.
    IDLE_KEEPALIVE_SECONDS: float = 300.0

    def __init__(self, name: str, idle_keepalive_seconds: float | None = None):
        self._name = name
        self._idle_keepalive_seconds = self.IDLE_KEEPALIVE_SECONDS if idle_keepalive_seconds is None else idle_keepalive_seconds
        self._task_executor_lock = threading.Lock()
        self._task_executor_queue: list[TaskExecutor.Task] = []
        # set by :meth:`shutdown` to stop the worker permanently (distinct from idle relinquishment).
        self._shutdown_requested = threading.Event()
        # whether a worker thread is currently servicing the queue. A worker idle past
        # _idle_keepalive_seconds clears this and exits, so per-session threads do not
        # accumulate one-per-ever-connected-session; issue_task revives one under the same
        # lock, so the empty-queue exit and the enqueue can never strand a task.
        self._worker_alive = False
        self._task_executor_thread: Thread | None = None
        self._task_executor_task_index = 1
        self._task_executor_current_task: TaskExecutor.Task | None = None
        self._task_executor_last_executed_task_info: TaskExecutor.TaskInfo | None = None
        self._start_worker()

    def _start_worker(self) -> None:
        """Start a fresh daemon worker for the queue and mark a worker live. Caller holds the lock (or is __init__)."""
        self._worker_alive = True
        self._task_executor_thread = Thread(target=self._process_task_queue, name=self._name, daemon=True)
        self._task_executor_thread.start()

    class Task(ToStringMixin, Generic[T]):
        def __init__(self, function: Callable[[], T], name: str, logged: bool = True, timeout: float | None = None):
            """
            :param function: the function representing the task to execute
            :param name: the name of the task
            :param logged: whether to log management of the task; if False, only errors will be logged
            :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
            """
            self.name = name
            self.future: concurrent.futures.Future = concurrent.futures.Future()
            self.logged = logged
            self.timeout = timeout
            self._function = function
            # capture the caller's ContextVar context at task-issue time; the worker thread
            # spawned by ``start`` does not inherit ContextVars by default, so without an
            # explicit copy any per-session state set by the caller (e.g. ``_SESSION_KEY_VAR`` /
            # ``_ACTIVE_PROJECT_VAR`` bindings established by ``Tool.apply_ex`` or by
            # ``SerenaAgent.active_project_context``) would be invisible to the task body and
            # every reader of the active project would silently fall through to the legacy
            # single slot, breaking the per-session activation contract for any task that
            # ultimately resolves the active project (the language-server-manager init task
            # scheduled from ``_activate_project`` is the case that surfaces this).
            self._context: contextvars.Context = contextvars.copy_context()

        def _tostring_includes(self) -> list[str]:
            return ["name"]

        def start(self) -> None:
            """
            Executes the task in a separate thread, setting the result or exception on the future.
            """

            def run_task() -> None:
                try:
                    if self.future.done():
                        if self.logged:
                            log.info(f"Task {self.name} was already completed/cancelled; skipping execution")
                        return
                    with LogTime(self.name, logger=log, enabled=self.logged):
                        result = self._function()
                        if not self.future.done():
                            self.future.set_result(result)
                except Exception as e:
                    if not self.future.done():
                        log.error(f"Error during execution of {self.name}: {e}", exc_info=e)
                        self.future.set_exception(e)

            # run the task body inside the issuing context so per-session ContextVars and
            # any other context bindings established by the caller are visible to the task
            thread = Thread(target=lambda: self._context.run(run_task), name=self.name)
            thread.start()

        def is_done(self) -> bool:
            """
            :return: whether the task has completed (either successfully, with failure, or via cancellation)
            """
            return self.future.done()

        def result(self, timeout: float | None = None) -> T:
            """
            Blocks until the task is done or the timeout is reached, and returns the result.
            If an exception occurred during task execution, it is raised here.
            If the timeout is reached, a TimeoutError is raised (but the task is not cancelled).
            If the task is cancelled, a CancelledError is raised.

            :param timeout: the maximum time to wait in seconds; if None, use the task's own timeout
                (which may be None to wait indefinitely)
            :return: the result of the task
            """
            return self.future.result(timeout=timeout)

        def cancel(self) -> None:
            """
            Cancels the task. If it has not yet started, it will not be executed.
            If it has already started, its future will be marked as cancelled and will raise a CancelledError
            when its result is requested.
            """
            self.future.cancel()

        def wait_until_done(self, timeout: float | None = None) -> None:
            """
            Waits until the task is done or the timeout is reached.
            The task is done if it either completed successfully, failed with an exception, or was cancelled.

            :param timeout: the maximum time to wait in seconds; if None, use the task's own timeout
                (which may be None to wait indefinitely)
            """
            try:
                self.future.result(timeout=timeout)
            except:
                pass

    def _process_task_queue(self) -> None:
        last_active = time.monotonic()
        while not self._shutdown_requested.is_set():
            # obtain task from the queue
            task: TaskExecutor.Task | None = None
            with self._task_executor_lock:
                if len(self._task_executor_queue) > 0:
                    task = self._task_executor_queue.pop(0)
                elif (time.monotonic() - last_active) > self._idle_keepalive_seconds:
                    # idle past the keepalive window: relinquish this worker thread. The empty-queue
                    # check and this exit are atomic under the lock, so a concurrent issue_task either
                    # appends before we look (we take the task) or revives a fresh worker after we
                    # clear the flag -- no task is ever stranded on a dead thread.
                    self._worker_alive = False
                    return
            if task is None:
                time.sleep(0.1)
                continue
            last_active = time.monotonic()

            # start task execution asynchronously
            with self._task_executor_lock:
                self._task_executor_current_task = task
            if task.logged:
                log.info("Starting execution of %s", task.name)
            task.start()

            # wait for task completion
            task.wait_until_done(timeout=task.timeout)
            with self._task_executor_lock:
                self._task_executor_current_task = None
                if task.logged:
                    self._task_executor_last_executed_task_info = self.TaskInfo.from_task(task, is_running=False)

    @dataclass
    class TaskInfo:
        name: str
        is_running: bool
        future: Future
        """
        future for accessing the task's result
        """
        task_id: int
        """
        unique identifier of the task
        """
        logged: bool

        def finished_successfully(self) -> bool:
            return self.future.done() and not self.future.cancelled() and self.future.exception() is None

        @staticmethod
        def from_task(task: "TaskExecutor.Task", is_running: bool) -> "TaskExecutor.TaskInfo":
            return TaskExecutor.TaskInfo(name=task.name, is_running=is_running, future=task.future, task_id=id(task), logged=task.logged)

        def cancel(self) -> None:
            self.future.cancel()

    def get_current_tasks(self) -> list[TaskInfo]:
        """
        Gets the list of tasks currently running or queued for execution.
        The function returns a list of thread-safe TaskInfo objects (specifically created for the caller).

        :return: the list of tasks in the execution order (running task first)
        """
        tasks = []
        with self._task_executor_lock:
            if self._task_executor_current_task is not None:
                tasks.append(self.TaskInfo.from_task(self._task_executor_current_task, True))
            for task in self._task_executor_queue:
                if not task.is_done():
                    tasks.append(self.TaskInfo.from_task(task, False))
        return tasks

    def issue_task(self, task: Callable[[], T], name: str | None = None, logged: bool = True, timeout: float | None = None) -> Task[T]:
        """
        Issue a task to the executor for asynchronous execution.
        It is ensured that tasks are executed in the order they are issued, one after another.

        :param task: the task to execute
        :param name: the name of the task for logging purposes; if None, use the task function's name
        :param logged: whether to log management of the task; if False, only errors will be logged
        :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
        :return: the task object, through which the task's future result can be accessed
        """
        with self._task_executor_lock:
            if logged:
                task_prefix_name = f"Task-{self._task_executor_task_index}"
                self._task_executor_task_index += 1
            else:
                task_prefix_name = "BackgroundTask"
            task_name = f"{task_prefix_name}:{name or task.__name__}"
            if logged:
                log.info(f"Scheduling {task_name}")
            task_obj = self.Task(function=task, name=task_name, logged=logged, timeout=timeout)
            self._task_executor_queue.append(task_obj)
            # revive a worker that relinquished itself after going idle; under the same lock as the
            # worker's empty-queue exit, so enqueue and exit can never race a task onto a dead thread.
            if not self._worker_alive and not self._shutdown_requested.is_set():
                self._start_worker()
            return task_obj

    def execute_task(self, task: Callable[[], T], name: str | None = None, logged: bool = True, timeout: float | None = None) -> T:
        """
        Executes the given task synchronously via the agent's task executor.
        This is useful for tasks that need to be executed immediately and whose results are needed right away.

        :param task: the task to execute
        :param name: the name of the task for logging purposes; if None, use the task function's name
        :param logged: whether to log management of the task; if False, only errors will be logged
        :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
        :return: the result of the task execution
        """
        task_obj = self.issue_task(task, name=name, logged=logged, timeout=timeout)
        return task_obj.result()

    def get_last_executed_task(self) -> TaskInfo | None:
        """
        Gets information about the last executed task.

        :return: TaskInfo of the last executed task, or None if no task has been executed yet.
        """
        with self._task_executor_lock:
            return self._task_executor_last_executed_task_info

    def shutdown(self) -> None:
        """
        Signal the dispatcher loop to exit and cancel any tasks still queued.

        Idempotent. The currently running task is allowed to finish; pending tasks have
        their futures cancelled so callers blocked on :meth:`Task.result` raise
        :class:`concurrent.futures.CancelledError` instead of hanging.

        Per-session :class:`TaskExecutor` instances are shut down explicitly when their
        owning session is evicted (so the daemon thread does not idle in
        ``time.sleep(0.1)`` for the rest of the process lifetime). The daemon-wide
        executor on :class:`SerenaAgent` does not normally need to be shut down — its
        worker is a daemon thread and dies on process exit.
        """
        self._shutdown_requested.set()
        with self._task_executor_lock:
            for task in self._task_executor_queue:
                task.cancel()
            self._task_executor_queue.clear()
